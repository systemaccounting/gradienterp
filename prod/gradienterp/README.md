# gradienterp — the operator's own gerp

gradienterp runs on gradienterp. This directory holds what the operator authors for its own
instance, the way any firm authors things for theirs: scripts staged, reviewed and approved into
gradienterp's cabinet through the tools every firm has. Nothing here is platform code, and nothing
under `modules/` or `prod/per_customer` knows this directory exists.

`automations/`

- `upsert_customer_contact.py` — the person behind a gradienterp.cloud account, as a customer
  contact keyed by the account id. Published as the hook `customers/upsert` for the web app
  (`prod/gradienterp_cloud`), which posts the account record on every save.
- `collections/` — the unpaid-invoice chase: audit, notice, cancel. The closure it ends in is `closure/`.
- `closure/` — the close, for either reason: begin (export and destroy), notice, close (the AWS account).

The local stack seeds `upsert_customer_contact.py` and publishes its hook at startup
(`tests/server/_hooks_local.py`); the rest are approved by hand.
