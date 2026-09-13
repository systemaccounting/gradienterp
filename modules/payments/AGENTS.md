# payments module


depends on accounting. the webhook ingestion layer — translates payment processor events into journal entries.

## current features

- **sales tax on a saved-card charge.** Stripe computes, the ledger records: a tax engine is rates
  per jurisdiction, taxability per product class per state, thresholds and filing — a service, not
  a rule instance (`multiply_item_value` is a café's local sales tax, not a nationwide seller's).
  `charge_saved_method` asks Stripe Tax first
  (`POST /v1/tax/calculations`: the invoice total, the customer — Stripe reads the address off the
  customer, the tax code is the account's preset). Where gradienterp holds no registration the
  answer is zero and the intent is what it always was. When tax is owed the intent is for
  `amount_total` and carries the calculation (`hooks[inputs][tax][calculation]`), so Stripe records
  the tax transaction on success and the reversal on a refund; the cents ride `metadata[tax_amount]`
  and the webhook hands them to `record_invoice_paid` as `tax`. An account without Stripe Tax
  set up answers the calculation with an account-level error and the charge goes out untaxed;
  `customer_tax_location_invalid` fails the charge closed like a decline. The idempotency key
  carries the tax, so a different answer is a different charge.
- **the setup session collects the billing address** (`billing_address_collection: required`,
  `customer_update[address]: auto`) and keeps it on the Stripe customer — the one place a buyer's
  address lives — and the buyer's VAT id when they have one (`tax_id_collection[enabled]`), so
  Stripe Tax applies the reverse charge on its own. VAT on a charge to an EU buyer needs the
  operator's EU registration in Stripe Tax (a dashboard action); until it exists Stripe answers
  zero tax, as it does for any unregistered jurisdiction.
- **four declines mean the card has to be saved again** (`PAYER_BACK` in the Stripe adapter):
  `authentication_required` (a challenge nobody is here to answer), and an India-issued card's
  e-mandate cancelled, paused or never registered (`transaction_not_approved`,
  `india_recurring_payment_mandate_canceled`, `payment_intent_mandate_invalid`). Each is a 409
  naming the link, not a retry.
- **an Indian business saves its card on a SetupIntent, which registers the RBI e-mandate**
  (Checkout has no card `mandate_options`): `payment_links {kind: setup, mandate: "india"}` sets the
  Stripe customer's name and address from the legal business profile — or, for an account card,
  the person's own (`profile`) — with the country as its two-letter code (Stripe refuses "India";
  Stripe Tax reads the Indian state, name or code) and its GSTIN as an `in_gst` tax id (read first, added once), then creates the SetupIntent
  (`usage: off_session`, card, a monthly maximum in the billing currency, reference
  `gerp-<contact>-<epoch>` — unique per mandate — `supported_types: india`) with Checkout's metadata, and returns its client
  secret. The gerp-cloud BFF picks it by the payer's country and answers the card page's url;
  the page confirms it with Stripe's Payment Element; `save_payment_method` reads the SetupIntent.
  The key needs **Setup Intents: write** beside Checkout Sessions and Customers.
- **the charge reads the card before it charges**: an `IN` card is charged naming the mandate its
  succeeded SetupIntent registered (`mandate` on the PaymentIntent); an `IN` card with none (saved
  through Checkout) is refused before any PaymentIntent as `india_card_without_mandate`, one of
  `PAYER_BACK`, so the invoice is marked unpaid and the link goes out. An India charge comes back
  `processing` for the bank's 26-hour pre-debit notice (above ₹15,000 or the mandate's maximum the
  cardholder authenticates it there); its `completes_at` rides the collection watch as
  `hold_until`, and `check_collection` re-queues without a strike until it passes.

- `payment_links {kind: setup}` — a Checkout Session in `setup` mode; returns the hosted URL a buyer
  saves a card on. Charges nothing, stores nothing, and the card never reaches us. `setup` needs an
  explicit `currency` (no line items to infer one from) or Stripe 400s. With `mandate: "india"`, a
  SetupIntent and its client secret instead (above).
- `save_payment_method` — the landing half: reads the finished session (or the card page's
  `setup_intent_id`), follows it to the SetupIntent for the method it saved, and writes `stripe_customer_id` + `stripe_payment_method_id`
  onto the contact — creating the contact first if this is the buyer's first appearance. NOT an
  agent tool (no `schema.json`): it is a redirect-landing endpoint.
- both are invoked cross-account by the gerp-cloud BFF (`billing_invoker_role_arn`), because gerp
  creation runs in the operator account and the card is saved before the buyer's gerp exists. They
  are Stripe-only and carry NO `provider` argument: `PROVIDER#` rows are written going forward by
  `configure_webhook`, so requiring one would break card-saving for every firm set up before it
  existed.

- `ingest_stripe` / `ingest_square` / `ingest_paypal` lambdas — receive a provider webhook, verify its signature (no stored verification value → 401, nothing written; a failed or erroring verification → 400), dedup by provider event id, dispatch to accounting's `transform_<provider>_<event>` → `post_journal_entry`.
  Stripe verifies against EITHER of two secrets. A signing secret belongs to an endpoint and
  appears only in the response that created it, so replacing an endpoint rotates it and both are
  briefly live; `_helpers.webhook_secrets` keeps the previous one beside the current with a 60s
  cache TTL, and without that a swap 400s events into a deleted endpoint's dropped retries.
- `configure_webhook` — agent tool, ONE for every processor: create the provider's webhook
  subscription and store what verifies deliveries. Stripe's default is the firm path: after
  `manage_mcp install` with Write on webhook endpoints at Stripe's approval screen, the lambda
  makes `PostWebhookEndpoints` through the vendor gateway as the firm (modules/mcp
  `firm_gateway.py`), so the signing secret comes back here and no key is pasted; the account is
  the one whose mode matches `stripe_billing`'s, and a read-only approval is a 403 naming the
  write. A write Stripe wants a human to approve first comes back 409 with `approval_url`, and
  the same call with `approval_token` goes through. Stripe's server has no webhook delete, so
  the sweep disables older endpoints at the url (`PostWebhookEndpointsWebhookEndpoint`, by `id`).
  `secret_name` is the key path, kept for a firm that prefers a key and for Square and PayPal.
  `payment_links {kind: test}` has no Stripe adapter any more: the agent makes a test payment
  through Stripe's own tools. `provider` is
  a parameter, because the agent should not have to know which processor a firm uses in order to
  name a tool — and setup is the one moment it has just been told. Stripe endpoints are created
  pinned to `modules/payments/stripe_api.py`'s `VERSION`, the same constant every outbound call
  sends as `Stripe-Version` — one string decides the shape of both directions, and an endpoint left
  unpinned delivers whatever the dashboard's account default says.
- `payment_links {kind: test}` — one tool for every processor: fire a REAL test-mode payment so the
  processor delivers a signed event to our endpoint on its own. The integration test. It never
  claims the webhook arrived, only that delivery follows — assuming it is how a broken seam reads as
  a working one.
- `payment_links {kind: payment}` — a link the payer clicks to settle one invoice, created FRESH each time
  it is asked for. Stripe sessions expire within 24 hours, so one made at issue is dead by a day-3
  dunning notice; creating it at send time also re-reads what is owed. Stripe only so far; another
  provider gets a 501 naming that rather than something that looks like a link and is not. The payer
  returns to `return_url` when the caller names one, else to `PAYER_LANDING_URL`
  (`https://gradienterp.cloud/paid`, which says paid or cancelled) — never the firm's portal, whose
  url carries its slug.
- **two Stripe keys, different lifetimes.** `configure_webhook` takes a key scoped to
  **Webhook Endpoints: Write**, uses it once and it can be deleted. Everything that COLLECTS shares a
  standing key stored as `stripe_billing` — `charge_saved_method`, `payment_links` (kinds payment and setup)
  and `save_payment_method` all read it — and it needs **Payment Intents: Write**
  (`POST /v1/payment_intents`), **Checkout Sessions: Write** (`payment_links`, kinds setup and
  payment), **Customers: Write** (kind setup creates the payer),
  **Payment Methods: Read** and **Setup Intents: Read** (`save_payment_method` follows a finished
  session to the card). A key can charge or it can be one-shot, not both, so the collecting one is a
  standing per-customer secret to rotate and revoke. A missing grant surfaces as a 403 saying the key
  *"does not have the required permissions for this endpoint"*; editing permissions does not rotate
  the token, so SSM needs no update. `payment_links {kind: test}` is separate — it takes an explicit
  `secret_name` for a test-mode key.

- `charge_saved_method` — an off-session charge against a card the payer saved earlier. Refuses where
  none was saved and says to send a link instead. Idempotent on the invoice AND amount, with a key
  DERIVED rather than generated, so a redelivery replays the first response instead of taking the
  money twice. Books nothing: the charge fires the same webhook a human clicking a link fires.
- `manage_saved_cards` — op `list` / `select` / `delete` / `forget` over a payer's saved methods.
  The SET is Stripe's and the SELECTION is ours: a card is a `(customer, method)` pair, `list` reads
  Stripe each time it is shown (brand, last4 and expiry are never stored), and the contact holds the
  pair it charges as `stripe_customer_id` + `stripe_payment_method_id`. `list` returns every
  customer the payer may charge against (its own, plus `from_customers` — the account's, for a
  gerp) and the stored selection on its own, so a selection on another customer, or one Stripe no
  longer holds, is still reported. `select` verifies the method sits on a customer the contact may
  charge and is 404 otherwise. `delete` detaches, refusing with a 409 that names the contact still
  billed to the card — the caller passes the scope as `used_by` (which gerps an account owns is
  known there and nowhere else) — and deleting the subject's own selected card clears the
  selection, so a last card can go. Stripe's hosted customer portal was declined: it manages one
  customer, and a gerp picks across two.
  `select` on a payer nobody has met creates the contact (`manage_contacts` op put, named and
  addressed by the caller's `name` / `email`) before storing the pair — a gerp created against a
  card its account already holds never passes through `save_payment_method`, where the contact used
  to be born. `CONTACTS_PUT_FN` is derived from `CONTACTS_GET_FN`'s name. A contact born here —
  through `select`, or `save_payment_method` where `name` and `legal_name` ride the session
  metadata — carries `legal_name` when the caller gave one: the party the invoice bills, since a
  gerp is not a legal entity. It also carries the business's legal profile when the caller gave
  one (`legal` in the `select` payload; on the session as two JSON values, `legal` for name/email/
  phone and `legal_address` for the address, since Stripe caps a metadata value at 500 characters):
  the contact's `email`, `phone` and a `business` address, the one street line split into
  `street_number` and `street_name`. `forget` deletes the
  payer's OWN Stripe customer (card, billing address, every attached method) and clears the three
  ids off the contact — the gerp-cloud BFF's step for the seller's copy of a deleted account.
  `delete` and `forget` are absent from the gateway schema; only the BFF calls them. The BFF's
  cross-account grant (`billing_invoker_role_arn`) covers `payment_links`, `save_payment_method`,
  `manage_saved_cards` and `charge_saved_method` — the last for Pay now on a missed hosting
  charge. `list`,
  `select` and `save_payment_method` carry each card's `fingerprint` (Stripe's, one value per card
  number across customers), which the BFF keeps in gerp-priors when an account is deleted and
  reads before a card vends a gerp.
- `collection_rules.charge_saved_card` — the rule a firm ATTACHES to an invoice status
  (`INVOICE#issued`, or whichever status they choose). It lives here because collection is this
  module's domain, and it is bundled into invoicing's `issue_invoice` by the import graph. It
  announces `collection.requested` on the firm's own bus rather than invoking anything: the callsite
  has already answered its caller, so a failed direct call would be seen by nobody.
- **collection is event-driven** (`infra/collection.tf`) — a rule on the firm's own bus invokes
  `charge_saved_method`. Two things this demands of any handler reached this way: EventBridge invokes
  ASYNCHRONOUSLY, so a returned error dict looks like success and retires the message — a retryable
  failure has to **raise**; and the rule's own DLQ catches delivery failures only, so the durable
  half is `aws_lambda_function_event_invoke_config` with an on-failure destination. Both land in
  `-undelivered`, one queue per gerp. A decline returns (retrying declines it again); a processor
  5xx, a failed invoice read, or missing wiring all raise.
- routes: `POST /webhooks/stripe`, `POST /webhooks/square`, `POST /webhooks/paypal` on the customer API gateway.
- storage: `-webhook-log` table (idempotency — one row per `<provider>#<event_id>`), and the dead letter as TWO tables keyed alike — `-dlq` holds the FACT (`provider`, `event_type`, `reason`, `received_at`) and `-dlq-bodies` holds the provider payload. "How many provider events failed to map, and why" is real operational information and belongs in the open; the body that failed to map is a whole webhook — names, addresses, emails, card last4 — arriving on exactly the path a human then goes and inspects. One table made the first unservable to protect the second; split, the servable half is safe by construction with no projection to get right, and debugging joins them on `pk`.
- tracing one delivery: every ingest logs the event id on each way out — `<provider> event posted`
  (`event`, `event_id`, `entry_id`), `stripe event collected an invoice` (`event_id`, `invoice_id`,
  `journal_entry_id`; the invoice row keeps the same id as `payment_entry_id`), `no transform …;
  dead-lettered <event_id>`, or the dead-letter exception line — so an event id finds its log
  line, its `-webhook-log` row, and the entry it posted. Lines before 2026-09-13 are plain text.
- outputs: `webhook_log_table`, `dlq_table`, `dlq_bodies_table`, `lambda_functions`, `lambda_arns`.


### a payer's cards live under more than one Stripe customer

A contact carries TWO customer ids and they are not interchangeable:

    stripe_own_customer_id   the customer this contact's own cards hang off
    stripe_customer_id       whichever customer the SELECTED method belongs to

They diverge because a gerp can be billed to a card its ACCOUNT OWNER saved, and a payment method
charges only against the customer it hangs off — Stripe detach is terminal and methods cannot move
between customers. So `select` writes the second and must not touch the first: the setup page
reads `stripe_own_customer_id` to decide where a NEW card attaches, and reading the selected one
there attaches this payer's next card to somebody else.

`manage_saved_cards` op list returns the union across both, each row carrying its origin, because a
payer needs to see every card they can actually be charged on. Contacts written before the split
fall back to `stripe_customer_id`.

### collecting an invoice

`charge_saved_method` and a payment link both stamp `metadata[invoice_id]` on the way out
(their `provider_stripe.py`). `ingest_stripe` reads it off `charge.succeeded` and calls
`record_invoice_paid` with `cash_account="CASH_IN_TRANSIT_STRIPE"`; a charge with no invoice
behind it falls through to `transform_sale`, which is right — a payment link paid by someone with no
receivable, or a POS tap, is a plain sale.

**It hands off rather than transforming.** Clearing the receivable also has to release what
`issue_invoice` parked in `REVENUE_PENDING`, PER LINE, into each item's own revenue account — which
needs the invoice, and a transform never sees it. `record_invoice_paid` already owns that
transition and is already idempotent (status guard + deterministic `entryId`).

**`cash_account` is why that works.** It used to hardcode `DR CASH`. Money a processor collected is
in the processor's balance, not the bank, so a provider-driven payment names its in-transit account
and the later `payout.paid` moves it on; debiting `CASH` at collection would book the same dollars
again at payout. Default is still `CASH` — money handed over.

    at issue     DR ACCOUNTS_RECEIVABLE      CR REVENUE_PENDING
    at charge    DR CASH_IN_TRANSIT_STRIPE   CR ACCOUNTS_RECEIVABLE
                 DR REVENUE_PENDING          CR <the item's own revenue account>

PayPal and Square carry the same reference on their payloads and still call `transform_sale`, so a
collected invoice double-counts there. Neither has a collection path to trigger it — see TODO.

### connecting a processor

**`configure_webhook` refuses a mode mismatch.** A Stripe key carries its mode inside it and both
modes share one host, so a `rk_test_` setup key alongside a live collecting key connects cleanly,
stores a signing secret and reports success — while every live charge delivers to an endpoint that
exists only in test mode. The Stripe adapter compares the setup key's mode against `stripe_billing`
and refuses before creating anything. PayPal and Square cannot express this: their sandboxes are
different HOSTNAMES, so a mismatch fails at connect time on its own.

**Setup is idempotent, by create-then-sweep.** POST has no upsert and the signing secret appears
only in the CREATE response, so re-running has to create rather than reuse — then it deletes any
other endpoint at the same url. Create first deliberately: sweeping first leaves a window where an
event is simply lost, while an overlap only delivers twice and the event-id dedupe absorbs that. The
sweep needs `webhook_read`; without it the create still succeeds and duplicates remain, because
setup working matters more than a listing permission.

### the collection watch

`charge_saved_method` returns when the processor accepts the card; the books move only when
`charge.succeeded` arrives and `ingest_stripe` settles the invoice. A delivery that never comes
fails nowhere — the money is at the processor, the invoice stays `issued`, and nothing knows. That
happened: `ingest_stripe` went from 2026-06-04 to 2026-08-26 without an invocation because its
endpoint was registered in test mode while charges ran live.

- a successful charge sends ONE message to `<prefix>-collection-watch` with `DelaySeconds=300`
  (`_helpers.watch_collection`, never raises — a charge that went through must not report failure
  because a queue was unreachable).
- `check_collection` consumes it and reads the invoice. `paid` → emit `ok` and drop. Still open →
  strike, and re-queue once.
- **the message IS the pending record.** Nothing is written when a charge succeeds, there is nothing
  for the webhook to delete, and nothing to scan. A normal collection means the delayed copy arrives
  to find the invoice already paid.

**Why not Step Functions.** A `Wait` expresses the same thing and is what `modules/automation` uses
for a firm's own sequences. It is the better fit for a LONG wait — days — and would need a
platform-owned state machine deployed by terraform. For five minutes it is more moving parts than a
delay queue: a definition, an execution role, a log group, and a start call, versus a queue and a
lambda. If a payout watcher lands — days rather than minutes — that trade flips.

**Why not a table.** DynamoDB Streams fire on write immediately and TTL — its only delayed removal —
is documented as "typically within 48 hours", a cleanup mechanism rather than a timer. A table would
still need a timer beside it, leaving a poll or a scheduler entry per charge.

**Why two attempts.** `create_inc_from_log` opens a first strike silently on purpose ("a one-off
timeout should not mail anyone"), so one check could only file a task nobody sees. The second strike
notifies. Retry-before-file using the existing policy, not a new one.

**Why it prints instead of invoking `create_incident`.** The line goes to `collection:<invoice_id>`,
the subject `charge_saved_method` and `issue_invoice` already share, so whichever half loses a
collection strikes one stream. Printing costs no grant on the incident path and leaves payments
working when the automation module is off.

**Its log group is created explicitly** (`collection_watch.tf`) rather than left to Lambda's lazy
creation, because automation's subscription filter attaches BY NAME and an absent group fails that
apply outright — `module.automation` depends on `module.payments` for the same reason.

**It does not generalize to payouts.** SQS caps `DelaySeconds` at 900s. A charge settles in seconds;
a payout takes days, so `withdraw_to_bank` needs a different timer (a Step Functions `Wait`) and
this queue should not grow a `kind`.

**A payment link has no watch.** Nobody initiated anything at a known moment, so nothing is owed at
a point in time and no message is sent. That is the collect path a real firm uses most.

## what it does

receives webhooks from stripe, square, paypal, etc. routes each event to the matching transform in `modules/accounting/lambdas/ingest/transform.py` (which produces canonical, balanced line items without `accountType`). calls `post_journal_entry`. line items arrive unclassified by design — classification happens on the way out of the pending queue against accounting's chart-of-accounts registry (see `modules/accounting/AGENTS.md`).

## flow

1. payment processor fires webhook (`charge.succeeded`, `payout.paid`, `refund.created`, etc.)
2. payments lambda receives it, validates signature, dedups by the provider's event id (idempotency)
3. dispatches to the matching `transform_<provider>_<event>` function in accounting's ingest library → balanced line items (no `accountType`)
4. calls `post_journal_entry` with original webhook timestamp → entry lands in the pending queue (202) since `accountType` is absent
5. on the next `classify_pending` sweep, the chart-of-accounts registry resolves each line item's `accountType` and promotes the entry into the ledger
6. event types without a transform get logged to a DLQ for operator review — payments never silently drops a webhook, but it also won't fabricate line items when the mapping is missing

## providers

**A lambda here is a unit of INVOCATION, not a unit of code**, and that is what decides whether
something is one lambda or three.

The AGENT calls a **capability** — "configure the webhook", "make a test payment", "give me a
payment link". **A tool name must not carry deployment state.** `create_stripe_payment_link` makes
the agent answer a question before it can act — which processor does this firm use? — and nothing in
the tool namespace can tell it, so one step becomes two and the first has no tool. Three further
costs follow: a per-provider tool's description has to explain WHEN to use it, and the answer is
"when the firm uses X", the thing the agent does not know; picking wrong returns "no stripe
credential" and the agent has to infer from that to try a different tool NAME; and every tool is
schema it carries, against ~95 on a gerp's gateway.

So each capability is ONE lambda with a `provider_<name>.py` adapter per processor and `provider` as an
optional parameter. Which one a call means comes from `PROVIDER#<name>` rows in the settings table
(`lambdas/_providers.py`), written when `configure_webhook` succeeds — the same shape `SENDER#`
uses for outbound mail.

A VENDOR calls an **endpoint** — Stripe POSTs to a Stripe URL with a Stripe signature. Three
vendors, three URLs, three lambdas: not because the code differs, but because there are three
callers. That is why `ingest_*` stays split while everything else folded.

The adapter interface is the same in every capability:

    NAME, API_BASE_ENV, API_BASE_DEFAULT
    credentials(body, read_secret) -> (creds, error_message)
    <verb>(creds, …, api_base)     -> what the processor made

Both differences that look like they should break it live inside adapters and never above the
dispatch: PayPal authenticates in TWO steps (client id + secret → access token), and PayPal reads a
PAIR of secrets at fixed names where Stripe and Square read one the caller names. Each capability's
test asserts the capability has no `if provider ==` branch, because a fold that special-cases a
processor is three lambdas in a trenchcoat.

Adapters import by NAME (`import provider_stripe`), not by a computed lookup — `scripts/deploy.py`
walks the import graph to build the zip, and a dict keyed on a string it cannot follow would ship a
bundle missing two of them.

One thin ingest lambda per provider (`lambdas/ingest_<provider>/`), each wired to
`POST /webhooks/<provider>` and bundling accounting's `transform.py`. They share
`lambdas/_helpers.py` (dedup, DLQ, signature verification, the post_journal_entry
cross-invoke).

- **stripe** — `Stripe-Signature` HMAC over `t.body`.
- **square** — HMAC over `notification_url + body`, header `x-square-hmacsha256-signature`.
- **paypal** — verification is an **API callback**, not an offline HMAC: `POST /v1/notifications/verify-webhook-signature` with the request's transmission headers + the stored `webhook_id`. The **raw request body** must reach that call verbatim — PayPal CRC32s the bytes as-sent, so a re-serialized parsed event fails verification.
- a new provider is a webhook route + an ingest lambda + its `transform_<provider>_*` functions. (adp / clio / wells fargo tracked in TODO.)

## connecting a processor (agent tools)

Two agent tools, not two PER processor: `configure_webhook` creates the webhook subscription and stores what verifies deliveries; `payment_links {kind: test}` fires a real test payment so the processor delivers a signed event on its own. Both take `provider` and a **secret name**, never the secret itself — the owner submits credentials via the agent's in-chat `collect_secret` form (`modules/secrets`) under the fixed name the tool expects, and the tools read from SSM by name (the credential never enters the agent's context). The agent reaches each provider's setup guide (`<provider>/kb.md`) from the setup-guides KB via `search_guides` (`modules/playbooks`).

Which credentials each processor needs is the ADAPTER's business, not the caller's — Stripe and Square take one the caller names, PayPal reads a pair at fixed names. Owner-submitted, flat in the vault:
- **stripe** — one restricted key `stripe_setup` (Webhook Endpoints: write + Payment Intents: write).
- **square** — one access token `square_setup` (must match `square_api_base`'s environment). Every Square call names `square_api.VERSION` (`modules/payments/square_api.py`), as the Stripe calls name `stripe_api.VERSION`.
- **paypal** — two secrets `paypal_client_id` + `paypal_secret` (OAuth2 client-credentials).

The verification value `configure_webhook` derives is stored **nested** under `/secrets/<provider>/` (Stripe/Square `signing_secret`, PayPal `webhook_id`) — separate from the flat owner-submitted intake namespace the `ingest_*` lambda reads.

`payment_links {kind: test}` is the integration test. For Stripe it reads the named setup key, confirms a PaymentIntent with Stripe's test card (`pm_card_visa`), and Stripe fires + delivers `charge.succeeded` to the registered webhook on its own — exercising the processor→us seam (reachability, real signing-secret round-trip, real payload). Delivery is async, so the agent confirms via `list_pending_entries`, not inline.

These are interim hand-built gateway tools; a single `mcp.stripe.com` gateway target would supersede the Stripe adapters, blocked on Gateway sync — see TODO.

A contact has **one Stripe customer, holding every card its payer saved**. The setup page reads
`stripe_customer_id` off the contact and creates a customer only when it finds none; a failed read
raises rather than falling through to create. A second customer for the same payer would strand the
first card — a payment method cannot be moved between customers and detaching one is terminal, so
nothing could list, select or charge it again. `metadata[contact_id]` on the customer is for reading
Stripe's dashboard; it does not key anything, since `POST /v1/customers` creates and Stripe has no
upsert on metadata.

The contact learns its customer from `save_payment_method`, so an ABANDONED checkout leaves a
customer with no method attached and the next attempt creates another. Those are empty and strand
nothing. The anchor lands on the first completed save, and every card after it joins that customer.

`payment_links {kind: setup}` and `save_payment_method` are the card-on-file pair and are **Stripe-only, with no `provider` argument**. `PROVIDER#` rows are written going forward by `configure_webhook`, so requiring one would break card-saving for every firm set up before it existed. The argument earns its place when a second processor gets a card-on-file flow.

## storage

dynamodb. one table for the webhook event log (idempotency — dedup by provider event id) + a DLQ table for events whose provider/type has no transform yet. no account-mapping data lives in payments: canonical line-item shapes are code in `modules/accounting/lambdas/ingest/transform.py`, and account → account_type classification is a row in accounting's chart-of-accounts registry.
