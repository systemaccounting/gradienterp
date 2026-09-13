# tests/accounting — open work

`local/` coverage is complete — every lambda's `IS_LAMBDA=false` path is exercised, plus `test_audit_invariants.py` (cross-statement invariants) and `test_transform.py` (provider-webhook transforms). `add_classification` has no jsonl path (IS_LAMBDA-only), so `test_add_classification.py` loads it in lambda mode and swaps the ddb + lambda clients for fakes. Layout + conventions live in `tests/AGENTS.md`. What's left:

## integ/ — the `IS_LAMBDA=true` path

Same cases per lambda, against real infra instead of jsonl stores. Needs either:
- a dedicated test customer account (DDB ledger + classifications + pending + balances, S3 reports, SES), or
- moto / localstack for offline mocking.

Not a priority until deployment is wired beyond local mode.

## out of scope here

- Ingest pipeline above `post_journal_entry` (authorizer, API GW, SQS) — exercised via `tests/helpers/replay.py` + `tests/server/per_customer/`, not pytest.
- Vendor reconciliation.
