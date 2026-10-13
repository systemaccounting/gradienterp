# gradienterp — the operator's own gerp

gradienterp runs on gradienterp. This directory holds what the operator authors for its own
instance, the way any firm authors things for theirs: scripts staged, reviewed and approved into
gradienterp's cabinet through the tools every firm has. Nothing here is platform code, and nothing
under `modules/` or `prod/per_customer` knows this directory exists.

`automations/`

- `upsert_customer_contact.py` — the person behind a gradienterp.cloud account, as a customer
  contact keyed by the account id. Published as the hook `customers/upsert` for the web app
  (`prod/gradienterp_cloud`), which posts the account record on every save.
- `daily_list.py` — the operator's daily list: the product record by name from modules/metrics, the
  open invoices beside it, as a page under `pages/reports/daily-list/`. Every morning at seven; mondays
  the same lines go to the owner by mail (`to` in the schedule's params).
- `alerts.py` — the golive watch: a mail to report@gradienterp.cloud when an account signs up and
  when a gerp is created, naming the person behind the account. Runs on two `run_automation` rows
  at `METRIC#account.signed_up` and `METRIC#subscription.started`; deleting the rows turns it off.
- `collections/` — the unpaid-invoice chase: audit, notice, cancel. The closure it ends in is `closure/`.
- `closure/` — the close, for either reason: begin (export and destroy), notice, close (the AWS account).

The local stack seeds `upsert_customer_contact.py` and publishes its hook at startup
(`tests/server/_hooks_local.py`); the rest are approved by hand.
