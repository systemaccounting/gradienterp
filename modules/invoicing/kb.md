# invoice statuses and tags — what the ledger calls things, and what a firm calls them

An invoice's **status** is `draft → issued → paid`, with `unpaid` between the last two when a
charge was attempted and did not land. Not configurable. Those are money positions, not vocabulary:
not billed, billed, tried and failed, collected. `issue_invoice` debits the only accounts-receivable
there is and `record_invoice_paid` credits it, so a status the modules cannot name is an invoice
outside the ledger.

`unpaid` posts nothing — the receivable was debited at issue and is still owed. It says someone tried
to take the money and could not, which is the fact a chase attaches to, and `unpaid → paid` is
permitted so a retry that later wins settles it the ordinary way.

A sale at a branch passes `location` (the ordinal from `manage_locations`) on `create` or
`from_template`: the invoice id leads with it and issue and paid post to that location. Omitted,
the sale is at #1, main.

## automation may use either

A firm's own rules and scripts reference statuses freely. A row on `INVOICE_STATUS#unpaid`, a script
that checks `status == "issued"` before sending — that is ordinary, and the whole unpaid collection
sequence is written that way. Statuses are shared vocabulary, not something the modules keep to
themselves.

**Reviewing one, a script naming a status is not a finding.** What would be a finding is a script
inventing a status the platform does not have, or writing one directly instead of going through the
transition tools.

Reach for a tag when no status names what you mean. `checked-out`, `disputed`, `needs-po` are facts
about a firm's own process, not about where the money is, so they get to be the firm's — and unlike
a status they mean nothing in anyone else's gerp.

When an owner asks for a status of their own — a hotel wanting `checked-out` — **tell them to use
`issued`**, because that is what it means: the folio is final and the money is owed. Do not apologize
for it. Then give them a tag for the part that is theirs.

## what a tag is

A label they invent, meaning whatever they decide. It carries no accounting meaning, which is what
makes it safe to hang their automation off.

    manage_invoice op=tag  invoice_id=… tag=disputed
                        op=remove invoice_id=… tag=disputed
                        op=list   invoice_id=…
                        op=find   tag=disputed          → every invoice carrying it

**They are a SET.** Many at once, unordered, independent. Applying `checked-out` does not remove
`checked-in`. If an owner wants that, remove the old one yourself in the same turn — do not go
looking for a setting, there is none, and that is on purpose.

## declare before applying

A tag has to exist in the `invoice_tags` registry first (`extend_schema`, registry `invoice_tags`,
bucket `common`, `class: operational`). Apply refuses an undeclared one and names it, so if you hit
that, declare it and apply again in the same turn.

Nothing ships canonical, so a new firm starts empty. **Before declaring, look at what is already
declared** and reuse the exact spelling — `disputed` and `in-dispute` are two tags to every query and
nothing warns you. This is the same failure job tags have (`modules/settings/kb.md`).

## a tag is where their automation goes

Applying one runs whatever rule instances are attached to `INVOICE_TAG#<tag>`; removing runs
`INVOICE_TAG#<tag>#removed`. So "when I mark it ready-to-bill, issue it" is a rule on a tag, not code.
Attach with `add_rule` on that key.

The rule can be attached before the tag is declared. It just never runs, because the tag cannot be
applied — and if something unattended tries, that lands as an incident on the owner's list rather
than failing silently.

## what tags are NOT for

- **Tracking work.** That is what the invoice's ITEMS are for, and each one already walks its own
  states with no setup at all — `manage_invoice` (op: transition) takes any state you give it, and one that matches
  no money rule is pure annotation. A hotel's `check-in` is an item state, not an invoice tag and not
  a status.
- **Anything that should move money.** A tag posts nothing. If the owner describes something that
  books, that is a status transition or an item transition, and those are the tools for it.
