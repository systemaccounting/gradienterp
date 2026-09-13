# modules — the remaining classics

the classic-ERP sweep (2026-07-18) against the module set. job costing, assets,
shipping/receiving, budgeting, and onboarding data import have come off this list;
these remain, in rough value order. each names
its shape so it can be picked up cold; per the events doctrine, each states its cross-firm row
when built.

- [ ] **period close / lock** — after the owner closes july, nothing backdates into it without
      an explicit reopen: a `closed_through` config row checked by `post_journal_entry`. thin,
      real, expected by any accountant.
- [ ] **AR aging + collections cadence** — the 30/60/90 aging read (composable from
      `manage_invoice (op: get)` due dates today) + a standing reminder cadence (calendar + email, playbook-
      shaped). "chase AR" exists as behavior; the classic is making it a discipline.
- [ ] **multi-entity consolidation** — an owner with two gerps has no consolidated view;
      a dashboard-level read across their own gerps. post-launch.
- [ ] **lot / serial tracking** — vertical-gated (food/bev): a `lot` attribute on inventory
      movements (no schema surgery — the movement log is schemaless), so "which lot went into
      tuesday's batch" is a query. build when a food producer asks. cross-firm: the
      `lot.recalled` / `coa.presented` matching rows presuppose it.
- [ ] **employee expense reimbursement** — medium-thin: receipt capture exists; the missing
      flow ends in "owe Dana $47" — an AP-to-worker posting the next pay run picks up.
      possibly a playbook + one classification convention, not a module.
- [ ] **order management depth** — a B2C order lifecycle (quote → order → fulfill → invoice)
      beyond POS-immediate and invoice-direct. today it rides POS webhooks + invoicing +
      shipping; a real gap only if a merchant asks for open-order tracking.

deferred consciously (revisit on a real ask, not before):

- **multi-currency** — US-only fleet; the `fx_exposure` matching rows are network-era work.
- **landed-cost absorption** — parked with the other absorption questions (candle-maker
  labor/overhead, freight-in).
- **CRM** — excluded by the personal-namespace stance; `contacts` is the boundary.
- **POS / e-commerce storefronts** — external by design; the platform ingests their webhooks.
