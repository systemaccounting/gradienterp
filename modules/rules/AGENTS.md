# rules module

**This is controlled extension of module behavior.** A firm shapes what a posting contains — a tax,
a fee, a gratuity, a payout — by writing ROWS, and the platform deploys no code for any of it. That
is the point of the module, and every decision below serves it.

**Rules are functions. Rule instances configure them**, so one function serves every firm and
whatever varies is a param on the row.

A rule has no trigger — it runs because a callsite reached it. Automation
(`modules/automation`) does have one: a schedule, an event, a tool call. Those scripts call rules
too.

Most rules return a list and the caller uses it. `charge_saved_card`
(`modules/payments/collection_rules.py`) sends an event on the firm's bus instead, which routes to
`charge_saved_method`.

**When a function belongs here: a ton of instances are waiting to use it.** `multiply_item_value`
takes five params and gets a row per taxable item per firm; `us_federal` takes eight and gets one per
worker. That fan-out is what makes writing the function once worth it. The other two grounds are
gerps duplicating it in their own scripting, and compliance or reporting needing it standardized.
Short of any of those an agent writes the forty lines in one turn and moves on — a rule one firm runs
once is a script.

A rule with **no params** is at the bottom of that test: its instances differ only in where they
hang, so it is a marker rather than a computation. `charge_saved_card` and `wage_accrual` are the two.
That is not disqualifying — the attachment IS the dispatch — but it is the shape to notice before
adding a third.

**A key is `<CALLSITE>#<subject>`, declared in `callsites.py`.** A rule instance is a row on a key; a
callsite is the query for that key plus the libraries resolvable there. Both halves used to be
written inline at each of the thirteen sites and collected nowhere, so nothing could answer *what can I
attach here* — and the key named the OBJECT, so an item's three moments (a line being priced, stock
moving on a sale, the reorder loop) shared one `ITEM#` key and each caller hand-filtered what came
back.
Naming the callsite splits them and deletes the filters.

Declared in one shared file rather than at each site, because the sites are lambda handlers a tool
cannot import — the same reason `modules/schemas/_registries.py` is one file. Libraries are NAMED
there, not imported, so a lambda still bundles only what its own callsite needs. It is not on the
rule: a rule does not know its key (`multiply_item_value` has no idea invoicing looks it up with
`instances.at(INVOICE_LINE, …)`), so labelling the function would be a second source of truth that drifts — the invariant
`spec()` exists to hold.

What it buys: `rule_params op=get catalog=true` takes a `callsite` and returns that site's menu, and
`manage_rules` op=add can tell that a row will never run — no callsite reads its key kind, or none that do
resolve its library. That check LOGS rather than refuses today
(`rule_attached_where_it_cannot_run`); flip it once the log is quiet across the fleet.

**The catalog is a contract, twice over.** To the firm: sales tax works, FICA works, wage accrual
works, and nobody rewrites them — so a rule a firm has attached is an API whose name and params
cannot shift underneath them. To the platform: `pay_run` imports `payroll_rules`, `manage_stock (op: move)`
imports `stock_rules`, and those modules attach no instances — they call the library directly. The
standardized logic is written ONCE and serves both, so what the platform does by default and what a
firm can configure are the same code. The catalog is small because it is earned: adding to it means
promising it to every firm forever.

There is now a third caller: an **automation script** reaching the catalog through
`ctx.rules(key, obj)`, which is `run_instances` under a different name. It changes nothing about how
a rule is written — the script is just a callsite that happens to be firm code rather than platform
code. What it changes is who the catalog is for. A firm automating a notice should not have to
reinvent how one is composed, and the parts of an automation that live in an instance are the parts
its owner can retune without sending the script back through code review.

A script reaches the library the same two ways a platform caller does. `ctx.rules` is the instance
path, for the parts whose params the firm owns. `import` is the direct path, for the parts it does
not: `automate` imports `automation_rules` and `general_rules`, which puts them in its zip, and
`exec` hands the script `__builtins__` — so `import general_rules` inside a script resolves, and the
role is what bounds it (`modules/automation/TODO.md`). `manage_stock (op: move)` already picks between the two
in one file: `run_instances(ctx, stock_rules.adjustment_instances(), …)` where the firm's rows
decide, `stock_rules.produces(effects)` where nothing is configurable.

Which half a function belongs in is the plain/`@rule` split the libraries already draw. `produce()`
builds a dict and `produces()` filters a list, so both are plain. `produce_on_sale` takes the firm's
`applies_to`, so it is a rule. `automation_rules` has no plain functions because both its entries are
decisions a script should not hold: a directly-callable `retry_order` would be the hard-coded
attempt count the library exists to move out of scripts.

Read the catalog first and attach an instance where one fits. Anything else is not a rule.

## a rule can answer what may happen, not only what does

`next_possible_values` (`state_rules.py`) takes a value and returns the values it may become, read at
the `next_values` callsite on `NEXT_VALUES#<what>#<current>`. Invoicing's statuses use it, a firm's
tag sequences use it, and a PO or a shipment would use the same function — nothing about "which moves
are legal" is per-module.

**It projects rather than judges**, and that is what lets it live here at all. `run_instances` has no
veto: two instances on one key both run and neither sees the other, so a rule answering "may I?"
could be overruled by the next row saying yes. Answering "where can I go?" makes two rows a union,
which is what stacking means everywhere in this catalog, and makes no rows mean nothing is permitted
— so refused and unconfigured are the same safe answer.

**Whose rows answer is the platform/firm line.** Invoicing keeps its status moves CANONICAL, the way
`transition_rules` keeps its postings canonical: the table is not read for them, so there is nowhere
to put a different opinion about a money position. A firm's own tags read the table, which is how it
builds a sequence with no deploy — and how it gets to be wrong and have its agent fix it.

**The other fold, if a callsite ever needs one.** A projection unions: more rows mean more permitted
answers. A REQUIREMENT is the opposite — "every attached instance must produce a row, or the move
does not happen" — and it needs nothing new either, because `run_instances` stamps `rule_key` on
everything returned, so a callsite can compare the instances it ran against the keys that came back
and treat a silent instance as a withheld permission. A firm that attached nothing folds over
nothing, which is vacuously satisfied, so the requirement simply does not exist for them.

Both folds keep the same property: no rule refuses on its own. Each says only what it permits, and
the callsite decides what a missing permission means.

## the model, in four lines

Everything else in this file is detail on these. If something you are about to build does not fit
here, it is not a rule.

1. **A rule is a function, stored in code.** `multiply_item_value` is a rule. Written once, for
   everyone; it never knows a tenant, a jurisdiction or an industry.

2. **Its params come from exactly two places** — and this is the literal call:

       fn(ctx, **instance["param"])

   `ctx` is the runtime value: the object being read during execution, a transaction, a pay context.
   `param` is the stored args, from the instance row. Nothing else reaches a rule.

3. **A rule instance is stored args**, so one function serves many uses:

       {"rule": "multiply_item_value", "name": "sales_tax", "param": {"factor": .0725, …}}

   A tax, a district tax, a gratuity, a fee and a royalty are all `multiply_item_value`. The row is
   what makes them different, and writing one is the whole act of configuring a firm.

4. **A rule's return value has no vocabulary.** The caller called it, so the caller knows what it
   asked for. Adding a tax object to a transaction is one KIND of rule — a kind you never declare,
   because you control when and how you are calling it. Want a rule that sends events? Write one and
   call it where you want events sent.

**Rule "kind" is never necessary.** If a future change starts introducing a tag on what a rule
returns, a registry of return types, or a layer that inspects results to work out what they were —
that is this model being lost, and the fix is to delete the layer, not to formalize it.

The one thing the engine adds beyond calling the function: it stamps `rule_key` and `rule_exec_id`
onto what a rule caused, so rule-made objects are detectable and traceable back to the row. That is
the only reason `run_instances` exists rather than modules calling functions directly.

## three things, and nothing else

- **a rule** is a general function, written once for everyone.
- **a rule instance** is a named use of a rule, storing its parameters. One row.
- **modules call rules.** Invoicing, building a line for a hotel room, looks up the instances for
  that room's inventory key and runs each one.

The engine is pure functions, no infra. The rule *definitions* live in the domain that owns them
(`modules/labor/payroll_rules.py`, `modules/invoicing/transition_rules.py`, …); the engine plus the
relevant libraries are bundled into the lambdas that call them.

## current features

- `rules.py` — the engine. One kind of rule (`@rule`, a bare marker), `run_instances`, the
  return-shape helpers (`debit`/`credit`, `catalog_item`/`rule_added_item`, `default`), `defaults`
  (a fold, not a collector), `value` (value rules — below), and the arithmetic ops rules compose
  (`mul`, `clamp`, `_bracket_tax`)
- `instances.py` — the instance store (`add`, `for_key`, `at`): a row is a rule
  name, its params, the key it runs on, and an `n` that orders it against other rows on the same key
- `params.py` — the PLATFORM store (`GENERAL` rows): the legal requirements — what the law sets, not the firm — bracket
  tables, wage bases. Seeded weekly from canonical; layered *under* an instance's params at run time
- `general_rules.py` — the rules owned by nobody: `multiply_item_value` (a sales tax, a district tax,
  a gratuity, a fee and a royalty are all *it*) and `rate_posting` (every payroll tax is *it*)
- `agreement_rules.py` — what a firm answers without a turn: `accept_within` and `accept_in_stock`
  on `PROPOSAL#<kind>` (a counterparty's proposal landing — permit an accept or a counter, never
  refuse), `auto_order` on `REORDER#<item>` (a gap on the shelf — permit a PO to a named vendor at a
  named price). Projections like `next_values`; the callsite acts on what was permitted.
- `dispatch_rules.py` — `run_automation`: a callsite handing its moment to the firm's own code.
  Attach it, name the script, and that row is the whole configuration. Listed at `invoice_status`
  and `invoice_tag` only — not at the four callsites that fold returns into postings (a dispatch row
  is a leg with no account or amount), and not at `automation`, which would let a script dispatch at
  its own subject
- **`retry_order`'s limit is not capped in code, deliberately.** Looping a payer's cards is what a
  processor's fraud systems read as someone working through stolen cards, and it is still the firm's
  number to set. Enforcement had no right home: the add op is generic and its first per-rule branch
  is how a tool stops being generic; a `spec()` bound would be a concept invented for one rule. Both
  also guard a door nobody dangerous walks through — a script can loop without touching `retry_order`
  and never call `manage_rules` at all. It lives instead as advice in `modules/automation/kb.md`'s review
  checklist, the one place every script passes before it can run.
- `automation_rules.py` — what a RUNNING script asks through `ctx.rules`: `retry_order` (which
  candidates to attempt, and how many) and `retry_decision` (whether a failure is worth another).
  The judgment a script would otherwise hard-code, put where an owner retunes it without sending the
  script back through review
- `manage_rules` — agent tool, op-routed. `add`: write a rule instance (`matches`, `name`,
  `rule`, `param`, `n`) — the whole act of configuring a firm's business. `list`: the attached
  automations — every instance row (or one key's, `INVOICE_LINE#<key>` including its
  `INVOICE_LINE#*` catch-alls), each with its rule's param spec; canonical defaults appear marked
  `canonical: true` wherever no firm row overrides. `delete`: the OFF switch — remove a row by
  (`matches`, `name`); `n` is looked up when omitted, and asked for only when two rows on the key
  share a name. On a canonical key the built-in default resumes (the response says so)
- `rule_params` — agent tool, op-routed. `get`: the current PARAMS rows, a rule's param-spec, or
  (`catalog=true`) every offerable rule and where it attaches; `set`: write a platform /
  employer-wide / per-worker param row
- DDB `<prefix>-instances` — pk = the key the rule runs on, sk = `<n>#<name>`
- DDB `<prefix>-params` — pk = `GENERAL` | a contact_id, sk = `<rule>#<effective_from>`
- rule libraries bundled here for reflection: labor's `payroll_rules`, treasury's `treasury_rules`, inventory's `stock_rules` (`produce_on_sale` — the made-to-order backflush; `value_adjustment` — the count-variance posting; the add op offers them too)

## a rule instance

```
pk = INVOICE_LINE#beans   sk = 0300#ca_sales_tax
  { rule: "multiply_item_value",
    param: { factor: 0.0725, creditor: "SALES_TAX_PAYABLE", name: "CA sales tax" } }

pk = INVOICE_LINE#beans   sk = 0310#sf_district_tax     ← a second one. BOTH run, in n order.
pk = INVOICE_LINE#*       sk = 0320#processing_fee      ← runs on every line
```

**Dispatch delivers candidates; the rule's parameters refine.** The instance's key is the dispatch
(a point query by what the object in hand carries), and nothing is bound to the inventory item —
the `beans` catalog row doesn't know a tax exists. A rule MAY carry a `pattern`-typed param
(`applies_to`, regex-fullmatched against the item key inside the rule body) to refine the
candidates its key delivered: an `INVOICE_LINE#*` instance with `applies_to: ^\d+#beans$` taxes beans at
every location including future ones; `^2#beans$` scopes one branch. The condition is the rule's
own parameterized code — the add op validates the pattern compiles at authoring. Code building an invoice
**queries this table by the key the line already carries** and runs whatever comes back. Beans are
taxed because a row matches `beans`; an hour of consulting isn't because none does. A firm that owes
no sales tax has **no rows** — so there is no "taxable" flag, no rate-of-zero meaning off, and no `if`
anywhere in the codebase that knows what a tax is.

- `pk` **is the match value**, promoted to the partition key so matching is a point query rather than
  a scan over every rule asking "is this mine?". One substrate, several uses:
  `INVOICE_LINE#<inventory key>` (+ `#*` for every line) — a tax, a fee, a tip
  `STOCK_SOLD#<inventory key>` — a backflush, when stock moves on a sale
  `REORDER#<inventory key>` — a par level the reorder loop reads
  `PAY_RUN#<contact_id>` / `CLOSE_SHIFT#<contact_id>` — a payroll tax, a withholding, a wage accrual
  `INVOICE_STATUS#<status>` / `INVOICE_TAG#<tag>` — what happens when an invoice moves or is tagged
  `ITEM_TRANSITION#<accountType>#<state>` — what an object's money does when it enters a state
  `STOCK_ADJUSTED#*` — a count adjustment landing
  `INVOICE_TEMPLATE#<name>` — the objects a template expands into
  `ITEM_CREATED#*` — the defaults stamped on an item being created
  `DISTRIBUTION#<id>` — a distribution's terms
  `AUTOMATION#<name>` — what a script's step composes, named by the script or its schedule payload
- `sk = <n>#<name>` — `n` orders them when several match; a lexical sk sort IS that order. Several
  matching is the normal case: a state tax, a city tax and a catch-all fee all run on one line.
- **current config, not a version history** — an instance is the config NOW. Writing the same
  `(matches, n, name)` REPLACES the row; deleting it turns the rule off. There is no dating, because a
  run period is already frozen in the ledger (a pay run dedups on `(pk, sk)`, so re-running June
  no-ops and the W-2 sums those frozen entries). The one thing that DOES need both years to coexist —
  a platform tax table — lives in the dated `params.py` store, read as-of the period and layered
  under this row's params.

## where a record came from

Everything a rule creates says which rule instance made it; everything a rule reads says it was read.

| field | on | means |
|---|---|---|
| `rule_key` | every object, posting and default a rule **creates** | the instance that created it — `INVOICE_LINE#beans\|0300#ca_sales_tax`. Absent ⇒ no rule created it (a hand-typed invoice line) |
| `rule_exec_id` | a **list**, on both what a rule reads and what it creates | one id created per rule invocation, appended to the object READ and to everything CREATED |

So a hotel room that triggered a tax carries the exec id (it was read) and no `rule_key` (no rule made
it), while the tax carries both — and the exec id is what ties the two together afterwards. A room a
*template* created carries that template's `rule_key` too.

`run_instances` stamps both, so no rule can forget to, and a new rule says where its output came from for free.

**In the ledger it rides per SIDE.** `post_journal_entry` collapses an entry's legs into
`(debit, credit)` pair rows, pairing greedily — one pay-run entry holds legs from eight rules, so a
row's debit and its credit routinely come from different ones. A single row-level key would be a lie,
so a ledger row carries `debit_rule_key`, `credit_rule_key`, and `rule_exec_id` (the union of both
legs').

## no tax on tax, and why it needs no flag

An object a rule COMPUTED (`rule_added_item` — a tax, a tip, a fee) has **no catalog key**. Nothing
bought it. So `at(INVOICE_LINE, None)` returns `[]`: no instance matches it, and `INVOICE_LINE#*` doesn't reach it
either, because the catch-all means *every item you sell* and a tax is not one.

An object drawn from the catalog (`catalog_item` — a template's room-night) KEEPS its key, stays
matchable, and so stays taxable.

That is the whole of it: a property of what the object is, not a flag anyone has to remember to set.

## what a rule returns

Whatever the callsite needs. There is no vocabulary, no tag, and nothing to collect.

`debit()` / `credit()` build a journal-entry leg. `catalog_item()` / `rule_added_item()` build a
transaction object. `default()` builds a `{field, value}` pair. Each is a convenience for the shape
its callsite wants, and the callsite uses the returned list directly:

    "lineItems": rules.run_instances(ctx, insts, modules=RULE_LIBS)

`run_instances` writes `rule_key` and `rule_exec_id` onto each returned dict on the way out, which is
the one thing it adds beyond calling the functions in order.

`defaults(rows)` survives as the single exception, and it is not a collector — nothing is filtered.
It folds `[{field, value}, …]` into `{field: value}` because a mapping is the shape `manage_stock (op: create_item)`
wants and a list of pairs is not.

## value rules — a rule that produces a NUMBER, and yields it

Some rules answer *what is the value* rather than building something — inventory's `required_count`
(the level an item should be held at) and `order_required` (how much to order). Read with
`rules.value(instances, modules, role, ctx)` rather than `run_instances`: nothing is created, so
nothing is stamped.

**A value rule yields.** A value is a value *over time*, so the rule is a generator and the caller
decides how much to pull — `value(...)` returns the head (the value now) by default, `count=N` pulls N
successive values. A constant yields once and stops, which costs it one `yield` instead of a `return`
and one `next()` at the read. That is the whole reason to do it now: a rule that becomes a live
signal — a forecast, a setpoint with change points — needs no new interface and no caller migration.
The same applies outward: an agent tool over a value rule takes its loop shape (a count, an interval)
as ordinary params, so streaming is a bigger argument rather than a different tool. Effect rules may
`yield` too — `run_instances` materializes the call either way.

**The loop bound is the function's timeout, and the annotation says so.** Nothing caps a pull: ask for
too much and the invocation runs out, which Lambda itself reports to the caller as an `Unhandled`
function error carrying `Task timed out after N seconds` — the platform already delivers that, so no
invented ceiling stands in for it. A loop param's description tells the agent the trade rather than
forbidding it: **a big pull may be cut off at the invocation timeout — that's fine, read again from
where it stopped.** Resuming needs no cursor, because the read is pure — a value rule is `ctx → value`,
so continuing is just calling again with the ctx moved forward (a later `ts`).

**Composition, by role.** `role` matches an instance's `name` or its `rule`, and the instance's own
rule is what runs — so `order_required` composes `next(ctx["ev"]("required_count", …))` and gets
whichever function the firm bound to that role (a constant, a seasonal, a forecast) without knowing
which. Swapping the par strategy is a row, as ever.

## a rule or a script: does the next decision need the previous result

`run_instances` is a fold with no feedback. Every instance on a key runs against the SAME `ctx`, and
none of them sees what another returned — `n` decides who goes first and that is all the sequencing
there is.

So the question that settles where something belongs has one answer:

| | |
|---|---|
| the decision does not depend on an earlier result | a rule — which order to try, how many, whether a 409 is worth retrying |
| it does | a script (`modules/automation`) — that attempt failed, so try the next one |

A script is the only caller that can run something, read the answer, and decide what happens next.
Which is also why `ctx.rules` points one way only: a script may consult rules, and a rule can never
consult a script, because a rule has nowhere to receive a result.

**`n` is presentation order, not precedence over a value.** No rule reads another's return, and no
rule writes to the object it was handed — the engine's own `rule_exec_id` append is the only write to
`ctx`, and it is provenance rather than data. So changing an instance's `n` changes the order rows
come back in and nothing else that is computed.

Which means an instance can be inserted at ANY position without perturbing the others. A firm can
drop a `run_automation` row in the middle of a key and the taxes on either side of it compute exactly
what they computed before. The cost is runtime: the callsite runs every matched instance inline
before it returns, so a row in the middle is time added to the write it is attached to.

**The one exception is positional.** A callsite that needs one particular instance's return orders it
last with `n` and reads the tail. That buys ordering, not a condition — "this runs last" is
expressible, "this runs only if that came back empty" is not.

No callsite does this today: the four that consume returns (`pay_run`, `close_shift`,
`invoice_line`, `distribution`) take all of them. So `n` is inert everywhere it is currently used,
and someone attaching a second instance can rely on that. A callsite that starts reading the tail
ends that guarantee for its key, which is why it owes a comment where it reads the result — the row
author has no other way to learn that their `n` became load-bearing.

## the signature IS the param spec

`@rule` takes no arguments. It marks the function — the fence: an instance names its rule as a string,
and a name only ever resolves to a function wearing the marker — and does nothing else. No trigger, no
order: WHEN a rule runs is a module calling it, and its order is the instance's `n`.

A rule's params are read off its **signature** (`spec()` derives `{name: {type, description, required,
default?, options?}}`), so what the agent is offered when it writes an instance is exactly what the
function consumes. A rule cannot advertise a param it never reads, nor read one the agent was never
told about.

```python
@rule
def multiply_item_value(
    item,
    factor:   Annotated[float, "rate",    "Multiplied by the item's value. 0.0725 = 7.25%."],
    creditor: Annotated[str,   "account", "The account it is payable to — SALES_TAX_PAYABLE."],
    name:     Annotated[str,   "string",  "What it's called on the invoice."] = "tax",
):
```

- **required is structural** — a param with no default is required.
- **a typo is loud** — params are passed as keyword args, so an instance carrying `factr` raises a
  TypeError naming the instance, the rule and the param. The alternative is a rule silently computing
  on a default nobody chose.
- **platform values slot in underneath** — `us_federal` declares `schedules` (its bracket tables) as
  a required param with no default, and `params.layered()` supplies it from the `GENERAL` rows. A
  missing tax year therefore raises instead of withholding on a stale table, and there is no baked
  copy to fall back to.

## canonical instances

Some rows ship rather than waiting for a firm to write them, because they aren't business config —
they're what "correct" means:

- **the money rules** (`modules/invoicing/transition_rules.py` `CANONICAL`) — double-entry's own
  defaults. Cash collected before delivery is a liability; a collected tax is never revenue.
- **the invoice status moves** (`modules/invoicing/status_rules.py` `CANONICAL_STATUS`) — where an
  invoice may go from `draft`, `issued`, `unpaid`. `issue_invoice` debits the only
  ACCOUNTS_RECEIVABLE there is, so `paid` without passing `issued` is money that never entered the
  ledger.
- **the catalog default** (`modules/inventory/catalog_rules.py` `CANONICAL`) — every item needs a
  revenue account, and the goods/services split is the one every chart already has.
- **the count-variance valuation** (`modules/inventory/stock_rules.py` `CANONICAL_ADJUSTMENT`, on
  `STOCK_ADJUSTED#*`) — an ADJUSTED movement posts its value (variance → COGS) so INVENTORY dollars
  track the physical count.

They carry a real `sk`, so a posting one produces lands in the ledger with
`debit_rule_key = ITEM_TRANSITION#REVENUE#paid|0100#collect` — the same shape a firm-written instance produces.

### two semantics, and which one a key has

    get_rules, stock_rules    canonical = [] if written else [...]   a firm row REPLACES it
    money_instances           canonical or instances.for_key(key)    canonical wins, table unread
    next_statuses             CANONICAL_STATUS only                  table never read at all

A row does not stack onto a canonical one — two instances stamping the same field would let the
higher `n` silently win. Where the firm's row replaces it, that is the extension point. Where
canonical wins, there is nowhere to put a different opinion, and the accounting stands because no
such place exists rather than because anything polices it.

### a key answered from code refuses a row

The second semantics used to be silent: a row on `NEXT_VALUES#invoice_status#issued` was accepted,
listed by the list op, and never read — on the surfaces where being wrong is worst. Which subjects
answer from code is declared with the callsite (`modules/rules/callsites.py`: `canonical`, and
`instead` naming where the firm's own version goes), because the modules holding those rows are
lambda handlers a tool cannot import. `manage_rules` op=add refuses; `rule_params op=get catalog=true` returns
`answered_from_code` beside the menu.

The refusal is narrow, and being narrow is the point. Statuses are money positions, so the whole
`invoice_status` namespace is fixed — but a firm's TAG sequences are its own rows on that same key
kind. The five money keys are fixed — but every other state reads the table, which is how a hotel
collecting at `settled` writes on `ITEM_TRANSITION#REVENUE#settled` what canonical holds for `paid`.
A test binds the declaration to the rows the modules actually hold, so declaring one that does not
exist would refuse a row that works.

Everything else is opt-in: **nothing is seeded.** We *offer* the rules (`rule_params op=get catalog=true`
lists each one and its spec); the agent reads the business at onboarding and writes the rows.

## reuse is not a requirement

Two general rules cover a startling amount, because most financial contracts are algorithmically
trivial. But when a rule's ALGORITHM genuinely differs — a progressive bracket worksheet is not a
multiply — write another general rule rather than contorting one. Just consider how it composes first.

What is NOT allowed is a *per-tenant* or *per-jurisdiction* function: the rates and tables are params,
always.

## a rule read on a cadence

Most rules run per-transaction: a sale, a tax. Some are read on a schedule instead — inventory's
`required_count` and `order_required` are value rules a reorder loop reads to decide what to hold and
how much to buy.

The RULE half stays here: a value rule computes the number, from params and whatever ctx it is given,
the same as any other. What reads it on a cadence and acts on the answer is
`modules/automation` — the fold, the trigger and the action are business process automation, not
rules. See `modules/automation/AGENTS.md`.

## not here

- **pay** — settling a payable with cash + the ACH is treasury's generic "pay what's owed", identical
  for a worker's net, a vendor invoice, or a tax remittance. Rules stop at the reclassification; they
  never move cash.
- **the accounts** the postings name live in accounting's chart-of-accounts registry.
