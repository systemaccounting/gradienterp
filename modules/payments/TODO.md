# payments — open work

What's live is in [`AGENTS.md`](AGENTS.md) (`## current features`). Open work below. The
processors below reuse the shape the live ones already follow (`ingest_<p>` +
`configure_<p>_webhook` + `create_<p>_test_*` + `POST /webhooks/<p>`).

**Interim**: the intended end state is the agent reaching Stripe's whole outbound toolkit via
one MCP gateway target, but that's blocked (AgentCore can't sync `mcp.stripe.com` —
`resources/templates/list` `-32601`; fix filed in `stripe_bug.md`). The hand-built gateway
lambdas stay (rather than an in-process MCP client) to keep the tools on the Cedar path. When
the MCP target unblocks, supersede + delete them.

## a card that lapses after provisioning

- [ ] **nothing watches a payer's card once the gerp exists.** Having one is enforced to CREATE a
      gerp and never again. An expiry is the ordinary case and the next charge catches it; a
      deliberate removal looks identical from here, so both surface as a decline rather than as
      anything anyone saw coming.

- [ ] **a notice must not guess which fix it needs.** After a lapse the payer needs
      `create_setup_link`; after a single decline on a live card they need a payment link. Different
      failures, different links, and a notice carrying the wrong one sends someone to a page that
      cannot help them. The distinction is readable — `manage_saved_cards list` says whether any
      chargeable method survives.

## connect-time hygiene

- [ ] **two stale TEST-mode webhook endpoints** on gradienterp point at the live ingest url. They
      never fire for a live charge, so this is untidiness rather than risk — but it now needs the
      DASHBOARD: `stripe_setup` holds the live key and the test-mode key it replaced is gone, so
      nothing in the stack can authenticate against test mode.

- [ ] **whether a firm should hold keys in two modes at all.** `configure_webhook` now refuses a
      mismatch, so this is no longer a hole. But it was only expressible because a firm keeps a
      setup key and a collecting key independently; one key, or a mode recorded per firm, would make
      the class unrepresentable rather than caught.

## collection — Stripe only

`charge_saved_method` is `ADAPTERS = {a.NAME: a for a in (provider_stripe,)}` and
`create_payment_link` ships only `provider_stripe.py`. So a firm on Square or PayPal can have its
books posted from webhooks and cannot be ASKED to pay: no saved-method charge, no payment link.
Collection is a Stripe-only capability wearing a multi-provider shape.

- [ ] **`charge_saved_method` for Square and PayPal.** Each needs the provider's own stored-payment
      primitive (Square cards-on-file, PayPal vaulted payment tokens) and a `save_payment_method`
      path to create one — the Stripe adapter's `(cus_…, pm_…)` pair has no direct analogue in
      either. The idempotency key and the fallback loop are already provider-agnostic in `main.py`.

- [ ] **`create_payment_link` for Square and PayPal.** Square Checkout links and PayPal orders;
      both carry a reference field the way Stripe's `metadata[invoice_id]` does — Square
      `reference_id`, PayPal `invoice_id` / `custom_id`, which their transforms ALREADY read into
      the memo.

- [ ] **the collection branch in `ingest_paypal` / `ingest_square`.** `ingest_stripe` reads
      `metadata.invoice_id` off a charge and calls `record_invoice_paid` with
      `cash_account=CASH_IN_TRANSIT_STRIPE`, which clears AR and releases the parked revenue per
      line. The other two still call `transform_sale`, and **that is correct today** — with no
      collection path, every payment they report was taken some other way (a terminal tap, a PayPal
      invoice sent from PayPal's own app) and has no gerp receivable behind it. This item comes free
      with either adapter above and is dead code before them: it is listed so the pairing is not
      re-derived, not as a gap to close on its own.

## more processors

- [ ] `ingest_adp` — payroll settlement; debit WAGES_PAYABLE / credit CASH; `source` = shift's entry_id for accrual→settlement linkage. needs `transform_adp_*` + the labor module
- [ ] `ingest_clio` — legal billing; AR/cash flows
- [ ] **direct bank connectors** — `modules/payments/{wf,bofa,chase,…}`, one dir per bank
      (the stripe/square/paypal shape; explicit cases, no generic bank machinery). Each buys
      BOTH halves: the feed (transactions/statements direct — plaid stays the catch-all for
      the long tail) and ORIGINATION (ACH/RTP/wire — the spend-rails AP api with no bill-pay
      middleman). Gate = enrollment: [WF Gateway](https://developer.wellsfargo.com/), Chase
      partner onboarding, BofA CashPro are application-gated with contracts — plaid-review-class
      lead time, start at real-merchant onboarding. Cred shapes vary per bank (OAuth / mTLS);
      vendor quirks decode in each bank's transform, canonical event kinds stay bank-agnostic.

## operations

- [ ] DLQ review flow — unmapped event types land in the DLQ table; the operator agent surfaces them and routes to either ingest-transform authoring or a platform bug
- [ ] **launch gate** — a real merchant connects live Stripe per the live-first playbook
      (rewritten 2026-08-02: `rk_live_` webhook-only key, delete-after): `configure_webhook`
      creates the endpoint + stores the signing secret → the first real charge lands in pending
      with the signature **enforced**. Note: the entry stays in pending until its accounts are
      classified against the chart-of-accounts registry. Until a secret is stored, `ingest_stripe`
      accepts unverified (logged). gradienterp's own sandbox-era SSM values swap the same way at
      real-merchant onboarding.


- [ ] **delete the two stale test-mode webhook endpoints.** Left from when `configure_webhook`
      registered with `stripe_setup` while charges used `stripe_billing` — test mode, so nothing
      live routes through them and the sweep never sees them (it only looks at the mode its key
      belongs to). Dashboard-only cleanup.
- [ ] **revisit `ENABLED_EVENTS`.** Four types: `charge.succeeded`, `refund.created`,
      `invoice.paid`, `payout.paid`. The collection path now keys off `charge.succeeded` carrying
      `metadata.invoice_id`, and refunds and payouts each have more specific types than the ones
      subscribed — a failed payout does not arrive at all today. Changing the list means recreating
      the endpoint, which rotates the signing secret, which is now safe.

## withdrawing the balance

- [ ] **`withdraw_to_bank`** — gradientERP's Stripe account is on a `manual` payout schedule, so
      collected money sits in the Stripe balance until someone clicks *Pay out funds*. The receiving
      half already exists (`transform_stripe_payout_paid`, `payout.paid` subscribed, landing in
      `CASH_PENDING`); only the initiating call is missing.

      **Only Stripe can do it, and "payout" means three different things.** Stripe's is
      balance→own bank (`POST /v1/payouts`). Square's Payouts API is READ-ONLY — Instant Deposit
      fires from the Dashboard or POS, never an endpoint. PayPal's Payouts API sends money to THIRD
      PARTIES, up to 15,000 recipients per call; moving a PayPal balance to your own bank is a
      withdrawal with no public API. So the tool takes the `configure_webhook` adapter shape with
      one adapter implemented and the other two refusing by naming the provider's dashboard —
      naming it `payout` and folding all three in would make PayPal pay strangers.

      Needs `payouts:write` added to `stripe_billing`. That grant cannot add or change an external
      account, so the worst case is the firm's own money reaching its own verified bank early.
      Blocked on the bank feed: `CASH_PENDING` is drained by `reconcile` against Plaid, which is
      built and offline-proven but NOT applied, so withdrawals would pile up in `CASH_PENDING`.

## spend rails (money OUT — the agent pays)

Posture: the owner hands the agent their real payment account and sets their own approval
policy, including none — capability ships plain, risk posture is the owner's (the
continuation-budget doctrine applied to money). Virtual cards are an optional instrument,
never a gate. The platform never custodies funds — gerp↔provider directly; that line keeps
the platform out of money-transmitter licensing.

- [ ] **card in the vault** — the owner's card via the `collect_secret` form → SSM (chat never
      carries the number); `browse_*` fills checkout/portal pages from the vault. The rail works
      today (IRS Direct Pay proved portal-driving) — open work = the connect playbook
      (`payments/spend/kb.md`) + a browse fill-from-vault convention so the number
      never enters model context.
- [ ] **approval loop = zero new tools** — a pending spend is a TASK on the books (any session
      sees it); the agent nudges by outbound email (existing tool); the owner approves in chat —
      chat IS the reply channel. Policy starts as stated instruction; graduates to a rule
      instance (`modules/rules`) when an owner wants "ask above $X" enforced in code.
- [ ] **AP/ACH api rail** — the direct bank connectors' origination half (see more processors
      above) is the endgame; plaid transfer or a bill-pay provider api bridges banks not yet
      connected. Same cred posture: connect-time, owner's account.
- [ ] **issuing virtual cards (optional instrument)** — per-card caps + merchant locks +
      real-time auth webhooks, for owners who want a bounded instrument on a delegated task.
- [ ] **nothing reads `-undelivered`.** A charge that never RAN lands there with
      `condition: RetriesExhausted`, the original payload and the error, and nothing looks at it. A
      charge that ran and FAILED is covered — its log line files an incident and mails the owner
      (`AGENTS.md` § collection is event-driven), and so does `issue_invoice` when the announcement
      itself could not be sent. This is the remaining half: an SQS trigger on the queue that files
      the same way, against the same `collection:<invoice_id>` subject so it strikes one stream.

- [ ] **a payer who saved a card for one purchase and does not want it charged for everything.**
      Saving IS the consent: `stripe_payment_method_id` lands on a contact only because
      the payer completed a hosted setup page deliberately. So saved card → charge and otherwise →
      link needs no configuration row, and a `collect_by(method, min_amount, applies_to)` rule was
      written and deleted for computing nothing — it returned its own `method` param behind two
      filters, an identity function wearing a rule.

      What that gives up is a per-payer override, which is a real case and is the SECOND one. It
      becomes a contact field or a second rule instance when a firm asks for it, on evidence.

- [ ] **saving a payment method, by hosted checkout.** Nothing today saves a payer's card, so the
      charge lambdas below have nothing to charge. `POST /api/gerps` takes `business_name` and
      `openly_operated` and provisions — while spinning up a gerp is supposed to REQUIRE payment
      info, so the two disagree today.

      `create_setup_link` — a Checkout Session in `setup` mode, returning its URL. The SPA
      redirects there, Stripe takes the card, the browser comes back. No Stripe.js in the app, no
      card fields to style, no card number in our DOM or our logs. PayPal and Square each have a
      hosted equivalent, so the shape generalizes when they are needed; only Stripe is needed now.

      The session yields a `customer` and, through its `setup_intent`, a `payment_method`. Both land
      on the payer's contact — `stripe_customer_id` and `preferred_payment_method` are already
      canonical fields in `contact_fields.json`, so there is no new store and the fee invoice is an
      ordinary invoice against an ordinary contact.

      **The page Stripe returns to stores them, not the webhook.** `ingest_stripe` reads an event
      type and looks for `transform_stripe_<that type>`, and every one of those functions exists to
      turn the event into debits and credits. Saving a card sends `checkout.session.completed`, where
      nothing was bought and no money moved — there is no entry to post, only two ids to store. Sent
      there it would dead-letter as "no transform". So the BFF route the browser lands on reads the
      session and writes the contact. A webhook backstop for a payer who closes the tab is worth
      adding later, and it needs a landing place that is not a transform.

      **Gerp creation becomes two steps** — the form creates the gerp, the redirect saves the card,
      provisioning starts when they land back. Today one submit does everything, so this reorders
      `POST /api/gerps`.

      **Nothing here is platform-specific.** gradienterp billing its customers is a gerp making
      sales through its own connected Stripe, the same rail any gerp uses — one pattern, not a
      platform account beside a customer one. Each lambda runs in its own gerp's AWS account with
      that gerp's credential, so there is no branch to get wrong.

      What IS particular: gerp creation runs in the cloud BFF, in the management account, before the
      new customer's gerp exists. So creating the link and storing the contact means the BFF reaching
      into gradienterp's own gerp — the cross-invoke `POST /api/gerps` already does to reach tower's
      provisioner.

- [ ] **the collection lambdas — one per provider, per half.** Six, following the module's own
      shape (`configure_*`, `create_*_test_payment`, `ingest_*` are each one per provider):

      ```
      charge_saved_method    BUILT — stripe adapter, reached by a firm's invoice-transition rule
        stripe    PaymentIntent, confirm + off_session
        paypal    order against a vaulted payment token
        square    CreatePayment, card on file + customer

      create_payment_link    BUILT — stripe adapter (Checkout Session)
        paypal    order + its approve link
        square    payment link
      ```

      **One lambda per capability, an adapter per provider.** An earlier draft of this said the
      opposite — that these are different APIs rather than one API in three dialects, so a single
      branching lambda would be pretending. The fold disproved it: `configure_webhook` and
      `create_test_payment` both absorbed three genuinely different APIs, including PayPal's
      two-step auth and its fixed-name credential PAIR, without a single `if provider ==` above the
      dispatch.

      The differences are real and they belong in `provider_<name>.py`. What made the earlier
      reasoning wrong was measuring how alike the CODE is; the question is how many things CALL it
      (`AGENTS.md` § providers). The agent calls one capability and should not have to know which
      processor a firm uses in order to name a tool.

      **They book nothing.** A successful charge fires the same webhook a human clicking a link
      fires, and the `ingest_*` lambda books it on the path it already has. Posting here too would
      double-count, and the one booking path means autopay cannot drift from manual pay.

      **Each stamps the invoice id in whatever field its provider carries**, which is what lets the
      webhook clear the right receivable (the item below). Stripe has `metadata`; PayPal and Square
      each have their own reference field — confirm against the live API rather than assuming, the
      way the refund payload had to be. Each decodes back in that provider's transform.

      **Each needs a retry to be free.** Stripe takes an `Idempotency-Key`; the others have their
      own mechanism or none. A timeout, a redelivery or a rerun of the billing job must return the
      original charge, not make a second one — and a double charge is the one failure here that
      costs a customer money.

      **The credential posture changes.** `configure_webhook` takes a restricted key scoped
      to Webhook Endpoints: write, uses it once and discards it. A key that can charge cannot be
      one-shot — it persists, so it is a standing per-customer secret to rotate and revoke, scoped
      to payments and nothing else.

      **An off-session charge can fail in a way a link cannot** — a declined card, or an issuer
      demanding authentication the payer is not present to give. Neither is an error to retry; both
      mean this invoice gets paid by link. That is the second reason the link is the default and not
      a fallback bolted on later.

- [ ] **an arriving payment does not find its invoice.** `ingest_stripe` runs
      `transform_stripe_charge_succeeded` → `transform_sale`: `DR CASH_IN_TRANSIT_STRIPE /
      CR SALES_REVENUE`. That is right for walk-in cash, where the sale and the money are one act.
      It is wrong for anything issued as a receivable first, and nothing calls
      `record_invoice_paid` from the ingest path.

      Pay an issued invoice today and the ledger says: the invoice is still `issued`, AR still
      carries it, `REVENUE_PENDING` still holds it, and the revenue landed in `SALES_REVENUE`
      instead of each line item's own account. The two stranded balances offset, so the statements
      still tie and nothing looks broken — which is what makes it worth writing down. What is
      visibly wrong is an AR aging that never clears and a multi-account invoice collapsed onto one
      revenue line.

      The payment has to carry WHICH invoice it pays. Both collection rules above are the natural
      place to stamp it — a link created for an invoice and an autopay charge against one both know
      the invoice id — so the reference rides the processor's own metadata and comes back on the
      webhook. Then it decodes in the provider transform, the way every vendor field does, and the
      canonical path branches on a reference, not on a vendor: with one → `record_invoice_paid`,
      without → `transform_sale` as today.

      This bites both collection paths equally and is not a rule — a firm does not get to opt out of
      its receivables clearing.

## sales tax on the hosting fee (Stripe Tax)

- [ ] **the remittance entry.** When Stripe files and pays a return from the balance, the payout
      ingest meets a balance-transaction type it has no transform for and dead-letters it — that is
      how the type gets known. One transform then: DR `SALES_TAX_PAYABLE` / CR the processor's
      balance account.
- [ ] **the first taxed charge validates `hooks[inputs][tax][calculation]`** on this account's API
      version (`2026-05-27.dahlia`). It is sent only when a calculation owes tax, which needs a
      registration, so nothing exercises it until one exists; if Stripe rejects the parameter the
      charge fails closed and the collection rule's incident says so.
- [ ] **a registration, the day threshold monitoring flags a state.** California does not tax
      electronically delivered software, so none today. Locations in the Tax dashboard is the
      read; every calculation answers zero until a registration is on file.

- [ ] **the charging tools as the firm** — `charge_saved_method`, `payment_links {kind: setup,
      payment}`, `save_payment_method` and `manage_saved_cards` read `stripe_billing`; the firm's
      Stripe approval (modules/mcp, `firm_gateway.py`) could carry them too, the way
      `configure_webhook` goes now, and the second key would go. A rule at night holds the
      firm's token as an owner's turn does. Open: Stripe's human-approval step on some writes
      (`approval_token`) has no one to click at night; which writes ask is unrecorded.
