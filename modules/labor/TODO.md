# labor — open work

- [ ] more payroll rules — CA daily/weekly/7th-day overtime, meal/rest premiums, sick-leave accrual
      (the first non-posting `accrue` effect). Shift-grain, so they are `@general_rule`s in
      `payroll_rules.py` keyed on `CLOSE_SHIFT#<contact_id>` alongside `wage_accrual`, in `n` order —
      unless one turns out to be a capped multiply, in which case it is a `rate_posting` row and no
      code at all.
- [ ] form emitters — W-2 / 941 / 1099 as ledger sums ⋈ config → JSON into `worker-legal`
      (per role, no aggregation; the emitter injects the real SSN at filing).
- [ ] entry generators — salary (a calendar schedule punches a fixed entry); commission / piece
      (EventBridge on `invoicing` / `inventory`; **1099 → AP, not labor**).
- [ ] **retention-aware deletion** — `manage_labor`'s delete op hard-deletes a `worker-legal` row + its uploaded
      doc blobs (S3 object first, then the row) unconditionally; today the agent is the only guard (it
      searches current requirements and runs interference on a premature delete). Deferred edge cases:
      encode the actual retention windows (I-9: 3 yrs after hire / 1 yr after termination, whichever is
      later; W-4; state forms), block or warn on a delete inside the window, and/or soft-delete + purge
      on expiry rather than immediate hard-delete.

## onboarding

collapse the hiring paperwork to a prompt — "offer alice $20/hr for barista work" — and let the
agent run the rest:

- [ ] **offer → accept** — append-only request/accept, the `treasury-offers` pattern: a
      `labor-offers` table (hash `offer_id`) holds the offer (contact + role + rate + W-2/1099);
      DDB stream → EventBridge fires the lifecycle as spec events; accept is the agreement gate.
      nothing mutates — the offer and the accept are events.
- [ ] **provision on accept** — agreement writes the `worker` row (rate + classification + role
      from the offer) and links the worker's **`account_id`** (their own gradienterp.cloud login)
      onto the `is_employee` contact. no new identity type: role resolves through the existing path
      (chat's `resolveRole` → owner-sub from `gerp-customers`, else `contactRole` scans contacts by
      `account_id` → the relationship flag). the worker creates their gradienterp.cloud account
      themselves; accept is what writes their `account_id` onto the contact.
      prereq: `account_id` must be added to the `contact_fields` registry (it isn't yet, so the
      validator would reject it) — a `modules/schemas/data/contact_fields.json` edit + tower apply
      + per-customer reseed. the `account-index` GSI already exists; it stays empty until then.
- [ ] **self-onboard over chat** — on accept, send the worker a gerp web-chat link; the agent
      collects W-4 / DE-4 / bank / I-9 in conversation → `worker-legal` rows (masked) + the secure
      store (raw SSN / bank). Doc blobs (I-9 / ID scans) use the built file-capture path — see
      `AGENTS.md` (worker-legal → documents).

## availability + capacity → `modules/calendar`

Free/busy is cross-cutting and lives in `modules/calendar` (the time domain). Labor is a **consumer**: an employee
is a `scheduled` resource (its `ref` → the `worker` row), the wage `rate` stays here, the sell `bill_rate` on the
resource. Substrate build plan: [`modules/calendar/TODO.md`](../calendar/TODO.md). The labor-side pieces:

- [ ] **clock-in reconciliation (plan ↔ actual)** — the schedule (a `@period` movement on the worker's capacity
      item in `modules/inventory`, `source=shift/job`) is the plan; the `time-entry` is the pay truth
      (`close_handler` on `ended_at`). Keep them **two rows** — never consume the booking, since early-leave /
      late-stay / no-show are the normal states. On insert, intersect the worked `[started_at, ended_at]` against
      the worker's booked intervals (interval overlap — the same predicate `reserve` guards with) and ref the
      matched `source` — derived, non-blocking (a span matching nothing still pays, as unscheduled). Variance falls
      out: overlap = worked-as-scheduled, booked-with-no-overlap = left early, worked-beyond = overtime,
      booking-with-no-entry = no-show. Sub-fork: intersect at insert (store the ref; labor's write reads inventory)
      vs at report (recompute; labor stays decoupled).
- [ ] **billable labor → revenue** — route the reconciled entry by `source`: `shift:X` → WAGES only (coverage,
      `close_handler` today); `job:X` → WAGES (cost, worker `rate`) **and** a billable line `hours × bill_rate` →
      `SERVICE_REVENUE` on invoice X (`job:X` = an `invoicing` quote/invoice). Extend the close-handler seam to
      post the revenue leg when `source` is a job. Timing fork: clock-out / delivery / invoice-send.
