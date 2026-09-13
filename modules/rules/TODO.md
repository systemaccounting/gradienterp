# rules — open work

- [ ] **notice composition — the first catalog entry an automation script calls.** `modules/automation`
      scripts reach the catalog through `run_instances`, and the case that proves it is a merge:
      turn a row into `{to, subject, body}`. The script queries who, the rule composes what, the
      script calls the `email` tool. Wanted for the unpaid-invoice sequence.

      Four things settle its shape, and three of them are traps:

      - **No count params.** `email_address_count` beside `email_addresses` stores a fact and its own
        derivative, and the only thing a check can catch is the row disagreeing with itself. That is
        the `kind` tag again. `len()` is the count.
      - **Variable arity is not a problem.** `fn(ctx, **params)` passes keyword args, so a param
        whose VALUE is a list keeps arity at one — `Annotated[list[str], …]` is one param with
        variable contents, and the spec derives off the signature as usual.
      - **`string.Template` with `$named` vars.** `$` over `{}` is not cosmetic: `str.format` walks
        attributes, so `{x.__class__.__init__.__globals__}` is live in a template a firm can edit,
        while `Template` substitution is inert by design. Name the vars — `$amount_due` cannot be off
        by one, `$2` breaks silently when someone reorders and nobody can read the template to check.
      - **The check that earns its place is "every placeholder resolves"** (`Template.substitute`
        raises on a miss), validated when the instance is written — the same bar the add op already
        holds a `pattern` param to. That catches a typo'd `$amout_due`, which is a real error.

      Recipients are usually not config: for dunning they are the answer to a query over unpaid
      invoices, so they arrive as `ctx`. A genuinely fixed list is a stored list param. Either way
      `run_instances` already does `list(fn(ctx, **param))`, so one message per row is an existing
      shape rather than an extension.
- [ ] **the obligation domains need a rule that flags** — a min-wage floor, an expiring license.
      Licenses / compliance / credentials cannot do anything until one exists.

      This item used to read "effect kinds beyond postings … + the caller's dispatch on `kind`".
      There are no kinds and no dispatch: a rule returns what its callsite takes, so a rule that
      flags is a rule whose callsite takes flags, and the work is the rule plus the callsite that
      wants one. Nothing in the engine changes. See `AGENTS.md` § the model, in four lines.

## transaction-object support (the invoice redesign)

The general transaction object (`modules/invoicing/TODO.md`) leans on the rule engine here — a
template is a rule and its params; no new engine, no per-industry code.

- [ ] **templates as owner config** — a template is a per-tenant rule + params (the expansion
      ratios: rooms→cleans, nights→housekeeping), declared/param'd the way payroll rules are.
      the agent authors it at onboarding by reading the business ("hotel" → time-dependent rooms
      + servicing labor) and writing params via `rule_params` op=set. no per-industry rule code —
      industry variance is params, not code (the network-effect thesis: `../../README.md`).
- [ ] tax-table updates for already-seeded tenants — the canonical tables/rates now live in
      the data plane: `modules/schemas/data/rule_params.json` → canonical S3 → `seed_schema`
      seeds GENERAL rows into each tenant's rules-params table (the one canonical seed for all
      reference data). So a new tenant + the initial seed are data, not a per-tenant deploy;
      the code constants remain only as an unseeded-tenant fallback. Remaining: wire
      `rule_params` into the weekly canonical PULL so an *update* propagates to already-seeded
      tenants the way the schema registry's does (today an update is: edit the file, push
      canonical, re-seed).
- [ ] decouple `rule_params` op=get from the rule libs — it bundles `payroll_rules` to
      reflect each rule's param-spec; publishing the specs as data rows (alongside the GENERAL
      params) would drop the coupling. Defer until a second, non-bundling spec reader exists.

- [ ] **what a rule may see, when a script is the caller.** `run_instances` hands a rule the `ctx`
      its callsite built, and a lambda passes its own row — an invoice, a worker, an instrument.
      A script passes whatever dict it likes through `ctx.rules`, which is a wider door. `retry_order`
      reads `candidates` and `selected` and nothing enforces or documents that as the contract, so a
      script passing `methods` gets an empty list and no explanation. Either the rule states its
      shape and says so on a mismatch, or `spec()` grows a way to describe the ctx as well as the
      params.

- [ ] **`instead` is prose, not a key.** A callsite whose subjects are answered from code names where
      the firm's own version goes as a sentence (`callsites.py`). Good enough for an agent reading a
      refusal; not enough for one to follow the pointer without parsing English. If a second
      canonical callsite appears, make it the key the firm should write instead.
