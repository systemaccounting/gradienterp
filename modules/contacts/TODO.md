# contacts — open work

What's live is in [`AGENTS.md`](AGENTS.md) (`## current features`). Open work below.

- [ ] **stream → EB fan-out** — table has streams enabled but no pipe. Wire `contacts.added/updated/deleted` events when a downstream consumer (purchasing, public archive) needs them
- [ ] **Cedar policy layer** — e.g. "don't delete a contact still referenced by an open PO" (queries purchasing at policy-eval time). Phase 5
- [ ] **public_user type** — public-user is an explicit post-signup capability action; reuse the contacts table as the platform-wide public-user directory (`public_user` rows resolved at read time via api.openlyoperated.biz). Schema + resolution path when the public-user capability lands

## generalization / template reuse

The contacts shape — `modules/schemas/data/<module>_fields.json` canonical schema + DDB table + 5 thin pass-through lambdas + Gateway targets — is the template for any CRUD-with-a-schema module. Candidates as they land: `calendar`, parts of `labor` and `treasury`. Escape hatch from the shape is the module-test threshold: a module that enforces an invariant or produces a side effect an LLM can't synthesize earns real lambdas (e.g. accounting's conservation check + pair-row decomposition).


- [ ] **`manage_contacts` is the one tool schema over the byte cap.** 36 top-level properties —
      person, vendor, customer and employee fields side by side — are 2,673 bytes minified with
      every description emptied; the cap is 2,560 and the schema sits at 3,747 with descriptions
      trimmed to the bone (`scripts/lint_schemas.py` carries the allowance). The fix is structural:
      the role-specific fields nested as one object each (`vendor: {...}`, `customer: {...}`,
      `employee: {...}`), which also reads as what a contact IS. It changes the write shape every
      caller posts — payments' contact writes, the customer-contact hook script, the seeds — so it
      is its own pass, not a trim.
