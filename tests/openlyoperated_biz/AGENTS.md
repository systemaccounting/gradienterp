# tests/openlyoperated_biz — the deployed oob platform surfaces

integration tests for the openlyoperated.biz backend (operator account):
- the **economic counter pipe** (bus rule → counter lambda → counters DDB).
- the **read api** (`api.openlyoperated.biz/v1`): the directory, the read-through into a gerp's own
  `/oob` reads, the economy's counters, the stream door's key check.

## layout (mirrors the other module test dirs)

- `local/` — offline unit tests of the lambda logic (no AWS). runs in the default `scripts/test.sh` loop.
- `integ/` — against **deployed** infra (operator account); needs creds. run: `bash scripts/test.sh --env
  integ` (or `pytest tests/openlyoperated_biz/integ`). these fire real events / call real endpoints and
  **tear down** what they create — a dedicated test key, never the real `revenue` counter.

## creds

`_helpers.py` uses `operator-org` (operator account — counters, bus, read lambdas), overridable via
`OOB_OPERATOR_PROFILE`; cross-account reads use `OOB_CUSTOMER_PROFILE` (default `gerp-gradienterp`).
integ tests **skip** when creds don't resolve. `bash scripts/awsacct.sh --all` writes both profiles; `operator-org`
is for operator-account CLI/SDK, not the self-assuming terraform stacks (root `AGENTS.md` § the accounts).

## here / pending

- `integ/test_econ_counter.py` — the counter pipe, verified live (formalizes the earlier ad-hoc bash).
- `integ/test_api.py` — the directory lists gradienterp with its sources; the read-through serves the
  catalog and the financials (`net = rev - exp`) and the metrics name the api's url; the counters answer in
  the metric shape; every answer allows any origin; the stream door is 403 without a key. no creds needed.
- the page itself: `tests/e2e/published.spec.mjs` posts a ledger fact and reads it back off openlyoperated.biz.
