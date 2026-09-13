# reviewing an automation before it runs

This is the REVIEW playbook. A separate one covers writing an automation — if you are authoring
rather than reviewing, use that; this one is addressed to a reader who did not write the code.

Another turn wrote this script. You did not, and you have no memory of writing it — that is
deliberate. Read it the way you would read a stranger's, because a turn reviewing its own work
agrees with itself and the review is then worth nothing.

If you find you DID author this script in this session, stop and say so rather than approving it.

## which kind is this

Ask first, because it decides what you are looking for.

A **modules-kind** script is a PYTHON file reaching the firm's own ERP surface. Its entrypoint is
`run(ctx, **params)` and it has five things available:

    ctx.call("<tool>", {…})    invoke any tool this firm's gateway exposes
    ctx.rules("<key>", obj)    run the rule instances on that key and get back what they returned
    import <shared module>     the runner's own bundle — `automation_rules`, `general_rules`, and
                               the shared modules they pull in
    import boto3               ONLY where no tool covers the effect — see below. The runner's ROLE
                               is what bounds it, and that role is scoped in terraform
    plain Python

The two ways into the rule libraries are not interchangeable. `ctx.rules` runs a rule with the
firm's stored params, so the owner retunes a row. An import calls a shared function that has no
params to store; `modules/rules/AGENTS.md` states the split. What is importable is whatever
`automate` itself imports, so a shared module reaches scripts only once the runner pulls it in.
A script importing `general_rules` to hard-code what an instance is meant to hold is the thing to
send back: the import is available, the bypass is the finding.

**A tool where a tool exists; the SDK where one does not.** Sending is `ctx.call("send_email", …)`
and reading an invoice is `ctx.call("manage_invoice (op: get)", …)` — reaching for `smtplib` or writing DynamoDB
directly when a tool covers it is out of kind, because the tool is the reviewed, logged, scoped way
and hand-rolling around it discards all three.

But some effects have no tool and are not going to get one: acting on a credential the firm holds
for a system this platform does not run. `import boto3`, read the credential, use it. `automate`'s
ROLE is the boundary — a script reaches exactly what that role reaches, which is scoped in terraform
and reviewable there rather than negotiated per script. The rule is not "no SDK", it is "no reaching
around a tool that exists", and the two are easy to tell apart: name the tool it should have used,
or there isn't one.

A credential itself is never in a script, in its params, or hard-coded. It is read from the firm's
own parameter path at the moment it is used, and the name is the only thing that appears in the
source. Hold the name, never the value — the same rule the agent follows.

Where a recipient comes FROM is still worth checking: the firm's own records are fine (the customer
on an invoice is exactly who a dunning notice goes to); an address lifted out of untrusted content
the firm received — a processed document, an inbound message, a form submission — is not. A bare
unchecked address param is weak, but not wrong if the script says where the caller is expected to
have got it. And a script that sends a BATCH should read `failed_count` rather than assuming a 200
means every message landed. Everything else
it does is **glue**, and those effects belong to tools written and reviewed once by the platform. You are not checking whether `email`
works. You are checking whether **this script emails the people it says it will**.

An **external-kind** script is a SHELL script, run with `sh -e` — no entrypoint, no `ctx`, no
`params`, just commands top to bottom, and its inputs are the owner's env vars. Do not send one back
for lacking `run(ctx, **params)`; that contract is the modules kind's. It reaches the internet, the
firm's cabinet and its own env secrets, and cannot touch a module tool at all — that is enforced by its role, not by your reading, so you do not
have to hunt for a disguised ledger write. What you are checking instead is **what it sends and
where**. It can read the firm's documents and reach any endpoint, so exfiltration is the failure that
matters, careless as readily as deliberate. An address or endpoint you cannot account for is a
finding.

The third kind is not a script at all. **It is a Step Functions state machine** — a definition in
Amazon States Language, `.asl.json`, filed under `approved/machines/` — handed to Step Functions rather than executed by any runner of
ours. There is no entrypoint, no `ctx`, and no Python or shell anywhere in it. Do not send one back
for lacking `run(ctx, **params)`; that contract belongs to the modules kind.

Two properties make this the easiest kind to review, and they are properties of the FORMAT rather
than of anything the author did:

- **There is no `while`.** Loops are `Choice` edges and `Map` over an input array. A `Map` is bounded
  by its array; a `Choice` that goes backward is a cycle in a graph you can see. "Is this loop
  bounded" stops being a judgement about someone's code and becomes something you read off the
  document.
- **There is no escape hatch.** No import, no `exec`, no shell. Every effect is a `Task` with a
  declared `Resource` and a declared `Parameters` block, so the complete list of what it touches is
  readable — which is the thing a script can never offer you.

What it may REACH is its execution role, decided in terraform, not by your reading: this firm's own
module lambdas and `automate`, and not SSM, IAM or `states:*`. So a `Task` naming
`arn:aws:states:::aws-sdk:ssm:getParameter` is one the account refuses at run time. Say so as a
finding — an owner should not discover it as a failed execution — but you are not the thing stopping
it.

Everything below applies to all three unless it says otherwise.

## what belongs here at all

Automations sequence things the platform already does: query, loop, branch, call a tool. That is the
whole intended range.

It is not a size limit. A long, ugly, repetitive script that spells out something a loop cannot
express is fine — verbosity is not complexity.

What is out of range is a script that IMPLEMENTS instead of sequencing. The tells:

- it recomputes something a tool would have told it — a balance, a total, a tax
- it keeps state between runs in a shape it invented, a stored blob doing the job of a table
- it carries domain logic: proration, a tax, a scheduling algorithm. That is a rule.
- it defines classes or helpers meant to be reused. That is a library, and a library is a module.
- it works around a tool — three calls and a merge where one argument would have done it

When you see these, the finding is not "fix the script". The platform is missing something, and the
right answer is a feature request naming what: a tool, a rule, or an argument that does not exist
yet. Approving a script that implements what a module should own is how a firm ends up quietly
maintaining software it never meant to own.

## what to check

1. **The set it acts on.** This is the failure that actually happens. A script queries one set and
   then acts on another — fetches unpaid invoices and loops over every contact, filters by date and
   then ignores the filtered list. Trace the variable from the query to the call, by name. If the
   thing being iterated is not the thing that was queried, that is your finding.

2. **The loop is bounded by something real.** A loop over a query result is bounded by the query. A
   `while` is bounded by whatever you can prove about its condition, which is usually nothing. That
   is the loop inside one run. The loop ACROSS runs is a separate section below, and it does not
   show up in the source at all.

3. **A script that retries a payment says how many times.** Trying one card, then another, then
   another against the same invoice is exactly what someone working through stolen cards looks like,
   and a processor's fraud systems act on the pattern rather than the intent — elevated declines,
   review, a frozen account. Three attempts is unremarkable. Twenty is not. If the script loops over
   a payer's saved methods with no cap, or with a cap the owner did not choose, say so. The number
   is theirs to set — `retry_order`'s `limit` is a row they edit — but a script that decides it
   silently has taken the choice.

4. **Every `ctx.call` is one the stated purpose needs.** List the tools it calls and say in plain
   words what each is for. A reminder script that closes something is not a reminder script. If you
   cannot explain a call in terms of what the owner asked for, that is the finding — you do not have
   to prove it is harmful.

   **There is no allowlist to check against.** A script reaches whatever this firm's gateway
   exposes, by the same route the agent uses, so "that tool is not permitted" is never the finding.
   THIS is what bounds a script. Judge each call on whether the firm should be doing it.

5. **The arguments.** The gateway validates them against the tool's own schema and rejects a wrong
   shape with a 400 — so a bad call fails rather than doing something surprising. But it fails
   PARTWAY THROUGH: a script that dies on its third call has already made its first two, and those
   are not undone. Check each call against the tool's schema as you have it — field names, types,
   which fields are actually required — because the point is not letting the run start at all.

   Spend your attention on the arguments that are not literals. A literal was right when it was
   typed. A value that came out of a query or a rule was written against one sample row and will
   meet every row — an empty string where a number belongs, a missing field on the one record that
   has no address, a list where the tool wants one item.

6. **The branch nobody will test.** The dangerous call is usually in an `else` or after an early
   return. Read every branch. A script that is safe on the happy path and destructive on the
   fifteenth day is the shape to expect here.

7. **Nothing hardcoded that should be config.** Wording, intervals, thresholds, recipients and
   addresses belong in a rule instance or the schedule payload, not in the source. Say so when they
   are literals — not because it is unsafe, but because every future tweak then comes back through
   this review, and the owner will stop making them.

8. **No credentials, keys or tokens in the source.** Ever. Send it back. A script never holds a
   secret value — it passes a `secret_name` to a tool and the tool decrypts, the same way you do.
   A script trying to read SSM itself is not a style problem, it is a script trying to leave its
   boundary.

9. **What a partial failure leaves behind.** If the script makes three calls and the second one
   fails, what state is the firm's data in? You may not be able to fix it, but say it in the
   findings, because that is the thing the owner needs to know before this runs unattended.

## reviewing a machine

The numbered checks above are written for code. Most carry over — the set it acts on, arguments that
are not literals, the branch nobody will test, nothing hardcoded that should be config — but four
things are only askable of a graph, and they are why this kind is quick.

**List every `Task`'s `Resource` and `Parameters`.** That list IS what the automation does; there is
nothing else in the file. Judge each one the way check 4 judges a `ctx.call`: does the stated purpose
need it. A `Task` the execution role will refuse is worth saying out loud rather than letting the
owner meet it as a failed execution.

**Trace the STATE through the graph, not each state on its own.** Every state's output becomes the
next one's input, and the ways that goes wrong are invisible when you read states one at a time. The
one that actually happens: a `Pass` or a `Task` with `Result`/`ResultPath` unset REPLACES the input
rather than adding to it, so a field a later state reads is simply gone — the execution dies with
`States.Runtime … does not reference an input value`, naming the state that READ it rather than the
one that dropped it. Follow each field the definition depends on from where it enters to where it is
used. This is check 1 for graphs, and it is the check a reviewer reading state-by-state will pass
while the machine cannot run.

**Walk the edges.** A `Next` or a `Choice` pointing backward is a loop. Ask what its exit condition
reads and whether that value can change — a `Choice` on a field nothing in the graph writes is a
loop with no exit. This is the check that is a judgement call in Python and a reading here.

**Add up the `Wait`s.** A sequence that waits three days five times holds an execution open for
fifteen. Say so. An owner should know an automation is still running two weeks after it started, and
`delete` stopping it mid-way is a real thing that leaves work half-done.

**Wait durations must come from the payload, never literals.** Check 7 wants intervals out of the
source for its own reason; here it is what makes the kind reviewable at all. A machine cannot be
partially executed, and a sequence with three-day waits does not finish inside a review — so the only
way to satisfy "test it before approving" is to start one execution with short waits against real
data and watch it walk. A definition with `"Seconds": 259200` written in cannot be tested at all.

**A step that computes belongs in `automate`, not in ASL.** JSONPath and `States.Format` are bad at
composing a notice or resolving a recipient, and a machine that tries becomes unreadable exactly
where you need to read it. An EFFECT is a `Task` on the thing that performs it; WORK is a `Task` on
`automate` running an approved script — which has its own review, and is where that scrutiny
belongs. A definition doing arithmetic in intrinsics is a feature request in disguise.

**A failed step must fail the machine.** `automate` answers `{statusCode, body}` like every tool
here, and a returned dict is a SUCCESSFUL Lambda invocation as far as Step Functions is concerned —
so `Retry` and `Catch` never fire and the sequence keeps walking after a step failed. A `Task` on
`automate` therefore passes `raise_on_error` in its payload, or checks `$.Payload.statusCode` in a
`Choice` immediately after. Neither present is a finding: the machine will send day six after day
three failed and finish `Succeeded`.

## the ring you cannot see in the source

A script's writes are triggers. Applying a tag, moving an invoice to a status, selling stock — each
of those is a moment something can be attached at, and if something is, the script did not end when
it returned. Nothing in the file says so.

Two shapes, and only the first is readable:

**It re-enters its own callsite.** A script attached at `INVOICE_TAG#urgent` that applies the
`urgent` tag runs forever. The trigger and the write are both in front of you — compare them, and
send it back.

**Something else closes the ring.** This script applies `disputed`; a rule instance on
`INVOICE_TAG#disputed` applies `urgent`; this script is attached at `INVOICE_TAG#urgent`. No two of
those live in the same place, and reading this file will never show it.

So do not read for it — look it up. `get_rules` with no argument returns every rule instance this
firm has attached, each keyed by the moment it fires on. That is a short list of the moments that
are LIVE here. Take the `ctx.call`s you already listed to answer "is each call needed", and ask of
each one: could this produce any moment on that list — including the moment this script is itself
attached at? One call, once, and the ring is either there or it is not.

A hand-off is not automatically a finding — a script that tags an invoice so another automation
picks it up is a shape the platform intends. What the authoring playbook asks for is that the script
SAY so: the docstring names the moment, what it expects to pick it up, and what ends the chain. A
script that lands on a live moment silently has not thought about it.

**Volume is the same question without a loop.** A per-event moment — `STOCK_SOLD#*` fires on every
movement — means a run per movement, forever, and nothing caps it. That is not a bug and may be
exactly what the owner wants, but it is theirs to know: say how often you expect this to fire.

**A script that creates automations is the multiplying case.** Staging, approving and scheduling are
tools like any other, so a script can do all three. Every new script still comes back for review,
which holds. A script that writes SCHEDULES in a loop makes work faster than anyone reads it — if it
schedules, say what bounds how many.

Assume there is no net under any of this. Nothing counts a script's descendants, no depth limit
exists, and AWS's own recursive-loop detection does not cover the path a dispatch takes here. A ring
that runs, runs until a person stops it, and the first sign is usually the bill.

## test it before approving

Do not approve on a read alone. Make a few real calls against real data and look at what came back —
not merely that nothing raised.

**For a machine that means one execution.** It cannot be partially run, so start it with short waits
against real data and watch it walk — which is why the durations have to be in the payload. Read the
execution's history, not just its final status: a machine that reached `Succeeded` by taking the
branch that does nothing has proven nothing.

Choose the branch that ACTS, not the one that returns early. A test that only exercises the no-op
path proves the script can do nothing, which was never in question.

## check a fact before you make it a finding

You are reading a script that reaches other modules — statuses, field names, tool arguments, what a
tool returns. You do not know those from memory and you are not expected to. **Look them up.**

**Look up the DEFINITION, not the data.** `search_guides` for that module's playbook, or read the
tool's own schema. What a value can be is a question about the platform; what values are in the books
right now is a question about this firm's week, and the second does not answer the first.

Calling a tool and finding no rows tells you nothing about whether a value is legal. A status added
last month has no instances yet. A field a firm has not filled in is still a field. An empty result
is the weakest evidence there is, and reporting it as "there is no such status" is how a correct
script gets sent back with a finding its author cannot act on.

A finding that turns out to be wrong is worse than no review. It blocks a working script, and the
author now argues with you about a fact rather than fixing anything — so the one thing you must not
do is assert what the platform does from memory or from a sample. Statuses gain values, tools gain
arguments, and a playbook you read six months ago is not the platform today.

If you looked at the definition and could not confirm it, that is `escalate`, not `send_back`. "I
could not find where invoice statuses are defined" is a useful sentence. "There is no such status"
when there is one is a blocked deployment and a lost afternoon.

## the findings

Four outcomes, and say which one plainly:

- **approve** — call `approve_automation`. It copies the script out of `automations/staged/` into
  `automations/approved/`, which is the only place the runtime reads from, so approving is the act
  of making it runnable. **A machine takes one more step**: approving puts the definition where
  `manage_machines create` can read it, and nothing runs until someone calls that. So say
  plainly that it is approved but not yet deployed — an owner who thinks it is live is worse off
  than one who knows it is not. Approve the bytes you read: if the script changed while you were reviewing
  it, start over.
- **send back** — say specifically what to change. "Looks risky" is not a finding; "line 14 loops
  over `contacts` but the query result is `unpaid`" is.
- **feature request** — the script is not wrong, it should not be a script. Name what is missing and
  what would replace it: this tool needs a `total` argument, this belongs in a rule, this needs a
  trigger that does not exist. Tell the owner plainly that the automation is on hold pending a
  platform change rather than leaving them waiting on a review that will never pass.
- **escalate** — you could not tell whether it is safe. This is not a failure and should not be
  hidden. Tell the owner what you could not resolve and offer to send it to the platform for a human
  code review; the findings and the test calls go with it.

## what you are not deciding

Whether the automation is a good idea. Whether the tools it calls are safe — that was settled when
they were built. Whether the numbers suit this business, which is the owner's call and not yours.

Your review answers one question: does this script do what it says it does, and nothing else.
