# gradienterp.cloud — owner web app + BFF

Owner-facing web app for the platform (goal #3). **Operator-account singleton**: one front door for all owners (not per-customer — `modules/` is the per-customer tier). Serves the SPA + an `/api/*` backend-for-frontend. The BFF is the **authorization gate**: an account owns N gerp instances, and the BFF enforces that a request only touches a gerp the caller owns. Plain Python Lambda handler (`bff/main.py`), repo-standard shape — no Node/TS tier.

## why a BFF (not a static SPA hitting gateways directly)

Two reasons, both load-bearing with multi-gerp:
1. **Authorization.** The per-customer gateways' JWT authorizer proves *"a valid platform user,"* not *"the owner of THIS gerp"* — every account's token shares issuer/audience. So a static SPA hitting a gerp gateway directly leaves a cross-tenant hole (any owner's token passes any gerp's authorizer). The BFF closes it: `claims.sub` → owned gerps (`gerp-customers`) → the requested `gerp_id` must be owned, else 403.
2. **Topology + resolution.** An account owns many gerps, each with its own random APIGW URL. The BFF resolves `account → owned gerps → selected gerp's gateway` and hides the per-customer URLs; the browser only ever sees `/api/*` (single origin, no per-customer CORS).

## auth split

- **authn** — the BFF's APIGW JWT authorizer (operator Cognito pool, `issuer=pool`, `audience=gerp-cloud`) validates the token; claims arrive at `requestContext.authorizer.jwt.claims`. The handler never validates tokens itself. The static SPA + `/auth/callback` are public (`$default` route, no authorizer) so the page can load to log in.
- **authz** — the handler: `claims.sub` → `_member_gerps(sub)` (the `gerp-members` row, the one read of ownership) → `gerp_id ∈ member gerps`, else 403.

## routes

- `POST /api/mcp/complete {session_id}` — the vendor-consent landing (modules/mcp). The SPA's
  `/mcp/callback?session_id=…` posts it (kept across a sign-in when there is none); the handler
  asks each gerp the account OWNS (`complete_mcp_auth`, `_invoke_gerp` with `module="mcp"`) and
  the one holding that pending session completes it; none is a 404. The BFF role's invoke grant
  is the export pattern: `gerp-mcp-*-complete_mcp_auth` in any account, the callee admits the role.
| route | auth | does |
|---|---|---|
| `GET /` (+ unknown) | public | serves the SPA shell + `web/` assets (`app.js`, `vendor/lit-html.js`); unknown non-`/api` paths fall back to `index.html` |
| `GET /api/gerps` | JWT | the account's **member** gerps — `_member_gerps` queries `gerp-members` by `account_id` (operator account↔gerp spine; ownership is `role=owner`) and joins `gerp-customers` for the instance details. Returns id + label + `role` + `chat_url` (gateway URLs never leave the BFF) — the switcher |
| `POST /api/gerps` | JWT | **account-level** (no ownership check — no gerp yet): `{business_name, openly_operated, terms_version, legal, public}` → `_new_gerp_id`: the label slugged to `[a-z0-9-]`, cut so the id with its `-` and six random hex characters is at most `GERP_ID_MAX` (28: every resource in the gerp's account is `gerp-<module>-<gerp_id>-<thing>`, Lambda caps a function name at 64, and the longest function by convention leaves room for 28; `tests/tower/local/test_resource_names.py` holds every function, bucket and queue name against it); a suffix collision on the conditional put draws again, three times → write the `gerp-customers` row (status `awaiting_payment`) + the owner `gerp-members` row (`role=owner`). `legal` is the business's legal profile — `name`, `email`, `phone`, `street`, `city`, `state`, `zip`, `country` required, `unit` optional — refused with a 400 naming `missing` when short; `public` is its public profile — `name`, the address fields, `email`, `phone`, `links`, `lat`/`lng` — optional as a whole. Both are maps on the row. The screens send them from one form: the business name is `name` on both, and `public` is the legal fields whose box is ticked (Public fields, one box per field, all ticked while Openly operated is on, none when it is off) plus the links; the gerp screen's edit re-derives `public` from the same boxes, so a legal edit re-publishes. Nothing about the two maps changes shape — the values converge. **It does NOT provision** — payment info gates a gerp, so vending happens in `save-card` once the card is stored; provisioning then writes the business's `gerp-profiles` row from `public` (`kind=business`, `edges` = the gerp_id, `label` = the published name or the instance label when none) and passes both maps to tower for the tenant blob. `gateway_url` fills later when provisioning completes (gap). |
| `POST /api/billing/setup-link` | JWT | **account-level**: `{gerp_id, return_url}` → cross-account invoke of the seller's `payment_links` (kind `setup`) → a hosted Stripe URL to redirect to. The gerp being created IS the contact id. For a gerp's card the payload carries `name` (the label), `legal` (the row's legal business profile) and `legal_name` (its name; the owner's own name off the account row for a row from before the profile was asked); the account's own card is named by the login and carries neither. `select` on `/api/billing/methods` passes the same two. An Indian payer (the gerp's legal profile country, or the account's own country for the account card) is sent `mandate: "india"` and answered `/card#cs=<client secret>&return=<return_url>`: `web/card.html`, served with `STRIPE_PUBLISHABLE_KEY` injected, mounts Stripe's Payment Element on the SetupIntent that registers the RBI e-mandate and confirms it back to the return url (same origin only). The secret rides the fragment, which no request carries. The page loads Stripe.js from js.stripe.com and no other script; the SPA loads none. The legal profile's optional `tax_id` (the GSTIN field, shown for India) rides to the seller. |
| `POST /api/billing/save-card` | JWT | **account-level**: `{session_id}` or `{setup_intent_id}` (the card page's return) → the seller's `save_payment_method`, then `_provision_gerp` if it stored. Only the session id crosses — whose card it is comes off the session's metadata in the callee, since this request is a redirect the payer's browser followed. A card landing vends only while the account holds fewer than `GERP_LIMIT` (3) live gerps — queued, provisioning, active or stopped — or its `gerp-accounts` row's `gerp_limit`, and while capacity shows an account left; otherwise the gerp stays `awaiting_payment` with `held` = `limit` or `capacity`, which the home card reads, and the next card landing tries again. |
| `GET/POST /api/account` | JWT | **account-level**: read/write the caller's private record — first, middle, last, phone, street, unit, city, state, zip, country — plus `email` (follows the token, § below) and the default card summary, on the operator `gerp-accounts` table (key=sub). `GET` and a `POST` both return `missing`: the required fields (all but middle and unit) the record still lacks. A partial save is accepted. Every save, and the email sync, posts the record to the seller gerp's `customers/upsert` hook (§ the seller's customer contact). |
| `POST /api/billing/pay` | JWT | **gerp-scoped**: `{gerp_id}` — Pay now on a hosting invoice the monthly charge missed. The customer paying is the seller charging the card the gerp selects: the seller's `charge_saved_method {invoice_id}` (cross-account, `CHARGE_FN`) for the oldest unpaid entry in the row's `billing`; the callee's status and reason pass through. 409 when nothing is owed. |
| `POST /api/billing/pay-link` | JWT | **account-level**: `{invoice_id}` → a hosted Checkout link (`payment_links {kind: payment}`) for an invoice the caller owes — the closing invoice of a gerp their priors name (by email or phone), or any invoice on a gerp they are a member of; 403 otherwise. The refusal on create names the invoices it can settle this way. |
| `DELETE /api/account` | JWT | **account-level**: `{confirm: "delete my account"}` — the phrase is required on the request, as on close. Refused `409 {error, gerps}` while any member gerp is not `closed` (`awaiting_payment` rows go with the account — nothing was vended). Then, in order: the priors (§ deleting an account), the `gerp-accounts` row, the `gerp-members` rows, the `gerp-profiles` row, the account's own Stripe customer (`manage_saved_cards {op: forget}`, cross-account), the seller's contact (the `customers/erase` hook), the Cognito user (`AdminDeleteUser`, last — the token stays good until the reply). Every step is idempotent; a 502 mid-way is run again from the top. |
| `GET/POST /api/public-user` | JWT | **account-level**: read/write the caller's public profile — a `gerp-profiles` person row (`gerp_profile_id`=sub, `kind`=person, `edges`=account_id) in the operator-account profile registry (same-account write). `GET` also returns the measured fields — `soc` (`[{code, share, hours}]`, sorted by share) and `soc_window` — read off the row and never written here: `PUBLIC_FIELDS` has no `soc`, so a POST carrying one writes everything else and leaves it. `/api/gerp-config` carries the gerp's `naics` / `naics_window` the same way, off its business row. |
| `GET /api/places/autocomplete`, `GET /api/places/place` | JWT | address autocomplete via AWS Location Places V2 (`geo-places`), SigV4 from the BFF role — no browser key. On select, lat/lng is stored on `gerp-profiles`. |
| `GET /api/gerp-config` | JWT | **gerp-scoped**: own-check `gerp_id` → the whole gerp page in one object: operator-registry fields (`label`, `chat_url`) + a BFF-derived `agent_email` + live status (`openly_operated`, agent-email verify) best-effort-forwarded from the gerp gateway `GET /settings`. |
| `POST /api/gerp-settings` | JWT | **gerp-scoped**: own-check → flip the gerp's `openly_operated` flag via its gateway `PUT /settings`. |
| `GET/POST /api/gerp-info` | JWT | **gerp-scoped** (membership): the business info behind a gerp — `label`, `legal`, `public` — off the row, and a save of all three (`label` and the legal profile required as on create, the public one optional). A save writes the row, then the copies: the business's `gerp-profiles` row here (once provisioning has made one), the tenant blob through tower's `update_business_info` (`BUSINESS_INFO_FN`; best effort, logged), and the gerp's contact in the seller's books through the `customers/upsert` hook as an organization (§ the seller's customer contact). The rename is the label on this form. |

**account-level vs gerp-level:** `GET/POST /api/gerps`, `/api/account`, `/api/public-user`, `/api/places/*` act for the authed *account* (no ownership check — you can always touch your own account/gerps). `GET /api/gerp-config`, `POST /api/gerp-settings` (and future gerp-scoped routes) check ownership of the target `gerp_id`. Identity (`sub`) comes **only** from the APIGW-validated JWT claims in prod — the `x-debug-sub` header fallback is gated to local dev (`not aws.IN_LAMBDA`), so a caller reaching a public `$default` path can't spoof it.

`POST /api/support` — the one `/api` path answered without a subject: the `/support` page
(`web/support.html`, a page of its own the BFF serves; the landing's Private support glyph opens
it in a new tab) posts `{email, subject, message, website}`; the BFF sends through SES from
`SENDER_EMAIL` to `SUPPORT_EMAIL` (both env, the address in no page), Reply-To the email typed,
subject `[support] …`. Refused with the field named: an email that is not one, an empty or
over-long subject (200) or message (5,000), a filled `website` (a hidden field only a
form-filler fills). A send SES refuses is a 502 and the page keeps the text. The route is its own
(`aws_apigatewayv2_route.support`, no authorizer) so the stage throttles it — 1 request a second,
a burst of 5, then 429 from the gateway — since each post is an SES send from the identity Cognito's
verification mail also sends from. `web/paid.html` at `/paid` is where a payment link returns a payer
when the firm named nowhere else: it reads `?paid=<invoice_id>` and sets its message as text.
A page under `web/` ships only when `BFF_FILES` (scripts/deploy.py) names it.

`GET /api/capacity` — the create screen's line above the button, what a create takes one of:
the org's account count against its quota, counted now through `GerpCapacityRead` in the management account
(`prod/platform/management/capacity_read_role.tf`: `ListAccounts` + `GetServiceQuota`, trust
scoped to the BFF's role) — `accounts`, `quota`, `available` = quota less every account listed
(closed ones count until they leave), `measured_at`; at most one read a minute per container;
404 when the reads fail. Not on the landing screen: an account there is a gradientERP login.

`GET /api/regions` — the create screen's region dropdown: config.json `REGIONS` with each
region's `status` (offered | not yet), and `default` for `?country=` (the `countries` lists,
ISO2 codes and the names Places returns; this region otherwise). `POST /api/gerps` takes
`region` (the pick, or the address's country's) and refuses one not offered; the row and the
vend message carry it, and `_invoke_gerp` reaches a gerp's lambdas in its row's region.

## the gerp row, by who writes it

`gerp-customers`, one row per gerp from Create a gerp to closed, keyed by the `gerp_id` (a slug of
the label plus a six-character suffix, resource-safe: it names the sub-account's resources and the
SSM path). Private; the business's public face is its `gerp-profiles` row.

| written by | fields | when |
|---|---|---|
| the BFF, `POST /api/gerps` | `gerp_id`, `owner_sub`, `owner_email`, `label`, `status = awaiting_payment`, `terms_version`, `terms_accepted_at`, `openly_operated`, `legal`, `public` | Create a gerp; the row exists before any card |
| the BFF, the card landing | `status = queued`, `queued_at` (locally: `provisioning`, then `active`, `gateway_url`, `provision_stub`); the business's `gerp-profiles` row from `public`; the provisioning payload on tower's `tower-vends` queue | save-card or select; a card carrying an unpaid ending writes `prior` instead |
| tower `provision_customer` | `status = provisioning` when it takes the message, then `aws_account_id`, `vended_at` | the Account Factory vend, ~15 minutes, four at a time; `owner_sub`, `owner_email`, `label`, `openly_operated`, `legal`, `public` ride the message into the tenant blob |
| the per-customer CodeBuild apply | `gateway_url`, `status = active`, `provisioned_at`, `chat_url`, `runtime_endpoint_arn` | the stack is up |
| the per-customer CodeBuild stop (`deploy.sh stop`) | `status = stopped`, `stopped_at`; `gateway_url`, `chat_url`, `runtime_endpoint_arn` removed | the operator's teardown between sessions: the export, then the destroy; the apply (`deploy.sh start`) writes the row active again |
| the BFF, `POST /api/gerp-info` | `label`, `legal`, `public` | the owner edits Business info; the profile row, the blob (tower `update_business_info`) and the seller's contact follow |
| the BFF, `POST /api/gerps/close` | `status = close_requested`, `close_requested_at`, `close_requested_by` | the owner typed the phrase |
| the closure scripts, tower `close_account` | `status = closing` → `closed`, `closed_at`, `closed_how`, `closed_invoice_id`, `balance_owed` | the export and destroy, then the AWS account closing on day 30 |
| tower `bill_customer` | `billing` (the open hosting invoices), `balance_owed = 0` | at issue, and the daily read that stamps `unpaid_at` and clears a paid one |
| tower `update_owner_email` | `owner_email` | the owner's login changed |
| the account's deletion | the row itself, when `awaiting_payment` | nothing was vended, so the row is all there is |

`status` is the spine — `awaiting_payment → queued → provisioning → active → close_requested →
closing → closed` — and every writer moves it forward or leaves it. The one step back is the operator's:
`active → stopped → active`, the stack destroyed and applied again into the same account.

**The home card renders `status`** and nothing else about the row (`GET /api/gerps` sends it
with `download_until`, and `ahead` on a queued gerp: the `queued` rows with an earlier
`queued_at`, one scan of `gerp-customers`; `gateway_url` never reaches the browser):

| status | the card |
|---|---|
| `awaiting_payment` | waiting on payment method (or the balance a prior holds it on); opens checkout |
| `queued` | the spinner, "in line · N ahead of you" ("up next" at 0) |
| `provisioning` | the spinner, "provisioning… about 15 minutes" |
| `active` | "ERP instance"; opens the gerp |
| `close_requested`, `closing` | "closing… exporting your books first"; no chevron |
| `closed` | "closed · export downloadable until <date>"; opens the gerp |
| `stopped` | "stopped · books exported, account kept"; no chevron |

A gerp with no stack behind it (`close_requested`, `closing`, `closed`, `stopped`) opens onto
a screen of the closing line and the export section only — no chat card, no settings, no
instructions, no Close button, no *Export my data* (the tables are gone; the copy already made
is what *Get download links* mints, off `export_gerp` in `init_customer`, which stands). For
those rows `GET /api/gerp-config` skips the settings forward: there is no gateway to ask.

`unpaid` (a hosting invoice the monthly charge missed) is its own card ahead of these.
`POST /api/gerps/close` refuses a `stopped` gerp with 409: the closure build exports the live
tables first and a stopped gerp has none — the operator starts it, then the close runs as any
other.

**Who reads it.** `_member_gerps` on every gerp-scoped route (`label`, `gateway_url`, `chat_url`,
`status`, `aws_account_id`, `prior`, `billing`, the closed facts — the join behind `/api/gerps`,
`/api/gerp-config`, the pay and delete routes); tower `bill_customer` (`active` rows with an
`aws_account_id` are billed; every row carrying `billing` is read for its receivable state);
tower `close_account` (the status gate and the account to close); the local `/dev` verbs and
the e2e helpers, which write rows the way these writers do.

**The second copy.** Provisioning seeds a tenant blob in the gerp's own account —
`/gradienterp/customers/<gerp_id>`: `business_name`, `business_category`, `owner_email`,
`reporting_schedule`, `openly_operated`, `legal`, `public` — and the gerp's lambdas read that,
never the operator's table. What has a life after the seed: `owner_email` (`update_owner_email`),
`business_name` / `legal` / `public` (`update_business_info`, off a Business info save).
`openly_operated` moved: the live flag is the gerp's settings row (`GERP#openly_operated`),
toggled from the gerp screen through the gerp's gateway; the blob's value and the row's are the
create-time wish, read once by the seed. `owner_sub` has a copy too,
`/gradienterp/customers/<gerp_id>/owner_sub`, the chat lambda's owner check — ownership itself is
the `gerp-members` row.

## the purchase disclosure

Creating a gerp takes a card, and before the redirect the create screen says what the card is for:
`web/purchase-terms.txt`, plain prose — what is bought (a member account in the gradientERP AWS
organization), whose organization it is in, that the bill is usage-based at cost plus 20%, that
leaving is unilateral, and what leaving does to the data (exported at closure, downloadable 15
days, the ERP application and everything running in the account destroyed at once, then the export deleted and the AWS account closed). Readable
in the repo and at `/purchase-terms.txt`, inlined into the shell at serve time behind
`<!--PURCHASE-TERMS-->` the way `llms.txt` is, so the page cannot drift from the file; `app.js`
holds no copy (`PURCHASE_TERMS()` reads the DOM). An *I read & understand* checkbox gates Create,
unticking re-gates it, leaving the screen clears it. The acceptance is a row, not a tick: `POST
/api/gerps` requires `terms_version` and the row carries it with `terms_accepted_at`. The version
is the file's content hash in git's blob form (`sha1("blob <len>\0" + bytes)`, 12 chars, stamped
as `data-version` beside the text) — content, not a commit, and self-verifying: given the wording
anyone recomputes it; an old one resolves out of a `./zip.sh` archive today and out of git history
once the repo is public. The terms are a page of the repo on GitHub, linked from the footer and
the line under the disclosure (`TERMS_URL`) — the same repo the login icons already point at, and a
404 until it is published; the disclosure covers what you are buying, the terms cover data, what
you may send other businesses, and non-payment.

## paying for a gerp

A card is a `(customer, method)` pair and a gerp SELECTS one. Create a gerp lists the account's
cards (*Pay with*, `payWithRow`; the default starts selected) with *Add a card* last
(`payWithAddCard`); Create is enabled once the disclosure is acknowledged and a row is picked.

- **a card the account holds**: `POST /api/gerps` (the row, `awaiting_payment`), then
  `POST /api/billing/methods {action: select, gerp_id, payment_method_id}`. The BFF passes the
  account's Stripe customer as `from_customers`, and the gerp's `label` + the login as `name` /
  `email` — the seller creates the gerp's contact from those when it does not exist yet, named by
  the business. A `200` on a gerp that is `awaiting_payment` is its payment step: the BFF runs
  `_provision_gerp`, the same guard `save-card` uses, so a select on a vended gerp is a card change
  and vends nothing. The buyer never leaves the page.
- **add a card**: Stripe's page in a NEW window. `window.open` happens inside the click (a blocker
  eats one opened after a fetch), the window is pointed at the Checkout url once the session
  exists, and the return url carries `popup=1`. The boot's popup branch calls `save-card`, posts on
  `BroadcastChannel("gerp-cards")`, and closes the window; the create screen, listening, goes home
  with the gerp building. `save-card` provisions, as it always did.

The gerp screen's *Billing* lists the gerp's own cards and the account's in one radio group, each
row labeled with where it lives; a new gerp starts with the account's default selected. Removal is
offered only on a gerp's OWN cards — an account card is removed from Info & Billing, where the
guard sees every gerp billed to it. When the stored selection names a method Stripe no longer holds
on that customer — the issuer replaced or cancelled the card and Stripe's card updater detached
it, or the operator detached it in the Stripe dashboard; the buyer has no Stripe surface and our
own delete refuses while a gerp is billed to it — the screen says the card is gone at the bank
(`cardGone`) and clears nothing: a clear on a read is a
write during a GET, and a transient Stripe error would wipe a good selection. The charge failing
is what reaches the non-payment sequence.

`setup-link` carries `name` too — the label for a gerp, the login for the account's own card — so
every contact the seller creates is billed to a name and never to a gerp id. A card added from
Info & Billing (`?billing=account`) returns to Info & Billing; there is no billing-address row,
because the address on a card is Stripe's and nothing here reads one.

## the record behind a feed

The platform publishes what its gerps do, and a person's published profile is a claim about who
they are. Both go into the feed, so both doors — `POST /api/gerps` and `POST /api/public-user` —
refuse with `409 {error: "complete your account first", missing: [...]}` until the account's
private record is complete: first, last, phone, street, city, state, zip, country. The gerp door
checks its inputs first (a malformed create is a 400 whatever the account's state). The SPA reads
`missing` off `GET /api/account` when either screen opens and renders the same list as an
*Incomplete* panel (`accountGate`) with a link to Info & Billing, so the refusal is seen before it
is hit. The record is private and never served; the public profile is the person's separate,
opt-in object.

Where a person's own information is required, and by whom: sign-in, being reachable and account
recovery (the email — Cognito, platform mail, `verified_email`); the customer's own AWS account
(Account Factory makes an Identity Center user from the owner's email); paying (cardholder name,
card and billing address, on Stripe's page — the platform stores the Stripe ids only, and Stripe
Tax reads the address there); the terms (`terms_version` on the row); and the full private record
for operating a gerp or publishing a profile. The last is the platform's own requirement rather
than a law's: this is an ERP that publishes business and economic data others act on, and a feed
with nobody accountable behind it is the thing this refuses. Nothing technical fails without it —
the Identity Center user takes a placeholder, the invoice bills the business — so it is stated as
a requirement. Not collected from the owner: a birthdate, a government id, a tax id. A typed
record is unverified; identity verification before publishing (Stripe Identity) is the step up if
a feed's consumers ever need more than "someone gave a name" (TODO.md).

Two namespaces. The account owner's information is this record. A gerp's employees, contacts and
contractors — the labor module's I-9s, the payroll rows, a customer's card in the gerp's own
processor — are the BUSINESS's records, held in the customer's own AWS account, exported to them
at closure, never served; the platform requires none of it.

## what the platform holds about a person, and what that obligates

The platform holds little: the private record, the login email, Stripe's ids, a terms
acknowledgement, the business's legal profile on each gerp row (name, address, email, phone —
never served; it goes to the tenant blob and to the gerp's contact in gradienterp's books, which
the hosting invoice bills), and for a deleted account the priors. What that little obligates:

- **two roles.** For the owner's data the platform is the controller (it chose to collect it);
  for the business's people it is a processor on the business's instructions, in the business's
  own AWS account. The first needs a privacy notice, the second a data processing agreement —
  both are golive work (`docs/` has TERMS.md only).
- **erasure** is `DELETE /api/account` (§ deleting an account). Retention runs the other way for
  the seller's invoices: tax law keeps them (seven years is the working number), and they name
  the human who ran the business — the buyer of record is the person, and the legal-obligation
  exception is what lets a kept invoice keep the name.
- **breach notification** needs one thing, a way to reach the person: the email on the row,
  which is why it follows the token and never goes stale.
- **PCI is Stripe's.** The card is entered on Stripe's hosted page; no card number, expiry or CVC
  touches a server the platform runs (SAQ A). The SPA has no Stripe.js and never will —
  `askStripe` in `app.js` is a call to this BFF for a hosted-page url. What is held is last4,
  brand, expiry and opaque ids.
- **data residency is a statement.** Every gerp is in us-east-1 (the region-pin SCP). An EU
  business's data in a US account is a transfer; the DPA's standard contractual clauses are the
  mechanism and the privacy notice says US-only plainly.
- **marketing consent** needs nothing today: platform mail is transactional (codes, invoices,
  incidents, closure notices). A newsletter is opt-in with an unsubscribe and the sender's
  postal address.
- **the AWS account is gradienterp's, and the customer is a user in it** (Account Factory,
  `AccountEmail = ops+<gerp>@gradienterp.cloud`). That is what makes cost-plus resale possible and
  what the disclosure's "member account in the gradientERP AWS organization" says; AWS's
  obligations run to gradienterp and gradienterp's to the customer, which the DPA writes down.
- **third parties collect their own.** Stripe Tax computes from the address on the Stripe
  customer; AWS taxes the per-gerp invoice under the operator's consolidated-billing settings; a
  gerp that collects payments connects its own processor, which does its own KYC of that
  business; chargebacks are Stripe's, with the acknowledged disclosure and `terms_version` as
  the platform's evidence.

## a hosting invoice the monthly charge missed

The seller's receivable state lives on the gerp row (`gerp-customers.billing`, kept by tower —
`prod/tower/AGENTS.md` § the receivable state on the gerp row); this console reads it and nothing
else. `/api/gerps` and `/api/gerp-config` carry `unpaid` when an entry has `unpaid_at`:
`{invoice_id, total, invoices, unpaid_at, closes_on}` — the oldest unpaid invoice, the total across
all of them, and the date the chase's deadline runs to (`unpaid_at` + 15 days, `CLOSES_AFTER_DAYS`).
The home card shows *payment failed · $X past due · closes D* (`gerpCard`), the gerp screen opens
on a panel above the cards (`unpaidPanel`, `payNowBtn`) whose *Pay now* is `POST /api/billing/pay`
against whatever card the gerp selects; a decline shows its reason and leaves the invoice owed, a
success says the row clears on the next daily read. The customer's own copy of the invoice — the
payable in their gerp's books — is the buyer half of cross-firm invoicing and is not what manages
the payment.

After the gerp is gone the emailed link is the invoice, and the console's `pay-link` mints one for
the closing invoice a refusal names (`owedPanel`, `owedPayBtn` on the create screen). A prior
refuses only while `balance_owed > 0`: an `unpaid` ending whose balance tower cleared is a fact on
the row and refuses nothing, at create and when the card lands.

## what a person does, and what a business sells: intended and history

A profile's `soc` (a person) and `naics` (a business) are the history — distributions the platform counts —
`[{code, share, hours}]` off the tasks a person closed across openly-operated gerps,
`[{code, share, revenue}]` off the invoice lines a gerp issued — with a `_window`. Nothing types
one: `PUBLIC_FIELDS` carries neither, the registry marks both `source: derived`, and the index
keys on each element's `code`. The public profile screen shows *Occupation — history* and the gerp screen
*Industry — history* (`measuredTpl`, `measured-occupation`
/ `measured-industry`), or *nothing yet*. The count that writes them, the vocabulary it reads
against (O*NET-SOC task statements, the NAICS index) and how a code lands on a task or a line are
the socnaics design; until it runs, every profile reads *nothing yet*.

## the seller's customer contact

gradienterp keeps its customers as contacts in its own books, and this app is one caller of a hook
that gerp published — an outside caller like any vendor. The operator ran
`manage_hooks {op: publish, path: customers/upsert, script: upsert_customer_contact.py, caller: bff}` on gradienterp
and stored the answer at `/gradienterp/cloud/hooks/customers_upsert` (SecureString `{url, token}`;
env `CUSTOMER_HOOK_PARAM`). `POST /api/account` and the email sync then post the whole record —
`account_id` (the sub), `email`, and the ten fields — with `Authorization: Bearer <token>`, and the
firm's script writes a person contact keyed by the account id and, for each id in `gerp_ids`
(the gerps the account owns from before the business's own legal profile was asked — a gerp
with one names itself), sets `legal_name` on that gerp's contact — the person running the
business, whom the invoice names; a gerp whose contact is not born yet is skipped and the card's
arrival writes it. The same door carries the business behind a gerp: `POST /api/gerp-info` posts
`gerp_id`, `name` (the label), `legal_name` and the legal profile's email, phone and address, and
the script writes the organization contact keyed by the gerp id. The parameter is read once per
container; a 401 drops that cache so a rotated token is read on the next save. The post follows the
row write: a failed post is logged and the save stands. The same door in reverse is
`customers/erase` (`erase_customer_contact.py`, published for the same caller, stored at
`/gradienterp/cloud/hooks/customers_erase`, env `CUSTOMER_ERASE_HOOK_PARAM`): the deletion posts
`{account_id}` and the firm's script updates the contact to a shell — first name "erased", last
name "account", no email, phone or address, `is_customer` kept, the row kept because the firm's
invoices reference its id. Publishing a second hook for one caller rotates that caller's token, so
both parameters are written after the last publish. The role holds `ssm:GetParameter` on those
two parameters and nothing in the seller's account.

## deleting an account

A deletion erases the person: every row that names them, in every place the platform put one.
`DELETE /api/account` (routes table) runs it; the gate is that no gerp of theirs is still running,
because a gerp is torn down by the closure sequence, which has its own gate and notices.

**The priors.** Before anything is deleted, one row per identifier goes to `gerp-priors`
(operator account, hash key `id`): `card#<fingerprint>` per saved card (Stripe's
`card.fingerprint`, one value per card number under any customer on our account — read off
`manage_saved_cards {op: list}`), `email#<sha256 of the lowercased email>`,
`phone#<sha256 of the phone's digits>`. Each carries `endings` (gerp_id, `how`:
`requested` | `unpaid`, closed_at, balance_owed — tower's `close_account` stamps `closed_how`
and `balance_owed` on the gerp row), the summed `balance_owed`, `how` (unpaid if any ending is),
and `stripe_customer_id` (a reference into Stripe's records for a chargeback). Nothing in the
clear; a key already present keeps its endings and gains these.

**Where they are read.** `POST /api/gerps` hashes the account row's email and phone and reads
both keys after the incomplete-record check: a hit is stamped `prior: {how, balance_owed}` on
the account row (returned by `GET /api/account`), and an unpaid one refuses
`409 {error: "an earlier account closed unpaid", balance_owed}` — the create screen says so.
The card is read when it lands: `save-card` and `select` carry the method's `fingerprint`, and
`_provision_after_card` reads `card#` before `_provision_gerp`. An unpaid hit leaves the gerp at
`awaiting_payment` with `prior` on its row and on `GET /api/gerps`, and the gerp card shows the
balance where it showed "add a card"; another card vends it. A `requested` ending is stamped
and refuses nothing.

A fresh card, email and phone walk through; the exposure is one billing cycle of that gerp's
AWS spend. Radar block lists and identity verification are the paid next step and are not built.

**What stays.** The seller's invoices (they name the human who ran the business, kept under the
tax exception), the AWS-invoice evidence the tower filed, logs under their retention. The
person's work history is other gerps' books — the contact each keeps, and the time entries,
task-dimensioned wage accruals, invoices and agreements under it — and stays as written; a
contact's `gerp_profile_id` then resolves to nothing. Gone with each gerp's teardown already:
the `USER#` settings rows, the Identity Center user, the SSM `owner_sub`, the agent's sessions
and memory. The profile-index rows clear on the `gerp-profiles` stream REMOVE (the optimizer's
reindex lambda).

**The screen.** Info & Billing, bottom: *Delete my account* opens the close dialog's pattern —
what goes, what stays, the typed phrase (`deleteDialog`), and, on a 409, the gerps still running
named in the dialog (`deleteBlocked`). Success signs out through Cognito's logout.

## the login email, and the row that copies it

Cognito owns the email — it is the login. `gerp-accounts` keeps a copy so platform mail can
enumerate and address accounts from a process with no JWT. The name is the row's alone: the pool
never holds `given_name`/`family_name` — the signup form sends the name as `ClientMetadata` on
`ConfirmSignUp`, and tower's post-confirmation trigger seeds the row from it. One store per field,
so there is nothing to reconcile and no trigger watching for a change the app makes itself. **The copy follows the token, never the
client**: `GET /api/account` creates a missing row from the validated claims and updates `email`
when the claim differs from the row. Nothing the client sends names an email, and the name columns
are the row's own and are never touched by that write.

Changing the login happens on Info & Billing (`accountInfoAndBillingScreen`): the SPA calls
`UpdateUserAttributes` with the ACCESS token, Cognito mails a code to the new address while the
old one stays the login (`attributes_require_verification_before_update = ["email"]` on the pool),
`VerifyUserAttribute` switches it, the SPA refreshes its tokens (`REFRESH_TOKEN_AUTH`) so the id
token carries the new address, and one `GET /api/account` writes the row. Two things that had to
exist for it: the token exchange keeps `id_token`, `access_token` and `refresh_token`, and the app
client's OAuth scopes include `aws.cognito.signin.user.admin`, without which Cognito answers
"Access Token does not have required scopes". What Cognito does not do, and the screen does not
pretend to: notify the old address, or undo.

The row is not the only copy. The Identity Center user Account Factory made from the signup email
is the owner's foothold in their own AWS account, and each gerp's tenant blob carries
`owner_email` for incidents; so the sync that writes the row also invokes tower's
`update_owner_email` (`OWNER_EMAIL_FN`, same account) with the old and new address and the gerps
the account owns. Best effort: a failure is logged and the row stands. A first read of a row
seeded without an email takes the claim and invokes nothing.

Labels: `acctEmailInput`, `changeEmailBtn`, `emailCodeInput`, `confirmEmailBtn`. Locally the calls
go to the Cognito stand-in on :4243 (`tests/server/AGENTS.md`); in prod the e2e reads the code off
the `gradienterp.cloud` catch-all, where `test+…` addresses are stored and never forwarded.

## closing is OFF by default

`POST /api/gerps/close` needs the typed confirmation on the request (a string typed into a browser
records nothing), checks ownership, marks the row `close_requested`, and hands the request to the
seller gerp's `closure/begin.py` by invoking its `automate` (`CLOSURE_BEGIN_FN`, cross-account by
resource policy, the callee admitting this role by name). From there it is the same sequence an
unpaid invoice ends in — export, destroy, fifteen days of notices, close the AWS account — and the
typed confirmation is what the script records as the approval. `var.closure_enabled` false (the
default) records the request and hands nothing on; on, the scripts still sit behind the operator's
own `CLOSE_BUILD_PROJECT` switch. This stack starts no build.

## vending is OFF by default

`var.provision_queue` is empty, so `POST /api/gerps` records the row and `save-card` leaves it at
`awaiting_payment` — nothing is vended. The whole create → pay → return path runs with no Control
Tower and nothing to clean up after. Named, the BFF sends the provisioning payload to that queue
in the operator account (`PROVISION_QUEUE`, the queue url) and writes `queued`; tower's
provisioner consumes it four at a time, so a burst of signups waits in line — Control Tower runs
five account operations at once, and the sixth is refused.

```bash
terraform apply                                      # off
terraform apply -var 'provision_queue=tower-vends'   # vends real sub-accounts
```

Off is the default because a routine apply must not silently enable 15-minute account vending. Turning
it on is the deliberate act, and the open work around vending is in `prod/tower/TODO.md`.

## SPA (`web/`)

Vendored **lit-html**, no build — the deployed file IS the source. `web/index.html` is a thin shell (`<style>` + `<div id="root">` + `<script type="module" src="/app.js">`); `web/app.js` is the SPA (state, Cognito PKCE auth, `/api/*` calls, view templates); `web/vendor/lit-html.js` is the ~8kb runtime. **All asset refs must be root-absolute (`/app.js`, `/vendor/…`, `/art/…`)** — `_html` serves the shell for *any* non-`/api` route, so on a deep route like `/auth/callback` a relative `src="app.js"` resolves to `/auth/app.js`, 404s to the HTML shell, and the module never loads → blank page (the OAuth callback can't boot). `web/art/gradientERP-lockup.svg` is the outlined-path wordmark (the `.wm-big` login/auth banner — font-independent so it doesn't substitute on Android; see `docs/images/art/`). Adding a view = a template function in `app.js` + an entry in `VIEWS`.

**`data-view` is the one addressing handle** for anything a test or a person points at; the **suffix names the kind**. Screens (on the `.wrap`) use the `screenName` vocabulary — one name across `S.view`, the `VIEWS` keys, and `data-view`: `landingScreen`, `signupScreen`, `confirmScreen`, `homeScreen`, `gerpScreen`, `createGerpScreen`, `publicProfileScreen`, `infoBillingScreen`. Sub-elements take other suffixes — `gerpsTable`, `gerpCard`, `ooToggle`, `whoChip` (`*Table`/`*Card`/`*Toggle`/… as needed). `id=` is reserved for the app's **own functional refs** (the form fields it reads via `$()`, CSS targets like `#savepublic`) — not test handles. Address anything via `[data-view="…"]`; the e2e suite leans on it.

- Info & Billing (`accountInfoAndBillingScreen`, crumb `Account › Info & Billing`) is four
  sections: *Your info* (the record — `accountGate`, `saveAcctBtn`, the fields as `acct-<name>`
  inputs with `addressFieldsTpl`), *Login email* (`acctEmailInput`, `changeEmailBtn`,
  `emailCodeInput`, `confirmEmailBtn`), *Billing* (`acctCard`, `cardRow` with `defCardBtn` /
  `delCardBtn`, `addCardBtn`; the selected row's key reads *Billed to*, the sentence under the
  list says a new gerp starts with the default) and *Deleting* (`deleteAccountBtn`, the dialog's
  `deleteDialog` / `deleteConfirmInput` / `deleteSubmitBtn` / `deleteCancelBtn` / `deleteBlocked`).
  Specs: `tests/e2e/account.spec.mjs`.
- The BFF bundle is the `BFF_FILES` allowlist in `scripts/deploy.py` (`build_webapp`) — `bff/main.py` + the named `web/` files; `WEB_DIR=/var/task/web`. Media never rides the bundle: the demo gifs and stills serve from the assets CDN (below).
- `_html()` serves real `web/` assets with content-type by extension (`_EXT_CT`; `_EXT_BIN` base64s binary types through the lambda shape) and falls back to `index.html` for client routes. ES modules must carry a JS content-type, so a non-`.js` MIME on `app.js`/`vendor/lit-html.js` blanks the page. Responses carry `cache-control:no-cache` + a content `ETag`, so the browser revalidates each load and gets a `304` (no body) until a redeploy changes the file — fresh-on-deploy without per-load refetch.

## a link into the console

`gradienterp.cloud/?gerp=<id>&open=chat[&say=<key>]` — the ready mail's link. With a session the
console goes to that gerp's chat door with the token on the fragment; without one it stashes the
link in `sessionStorage`, runs the hosted sign-in, and follows it after the token exchange.
`say` is a phrase KEY the console owns (`onboard` → "onboard my business"), never free text from
a url; the chat door reads it off the fragment and sends it as the first turn, once.

## the agent-readable surface

A fetching model gets raw markup (no JS), so the page carries its own guide:

- **`web/llms.txt` is the single source.** `_html()` substitutes its escaped content into the
  shell's visually-hidden `.agents-note` (`<!--LLMS-->` marker, `index.html`) at serve time — the
  in-page copy can't drift from `/llms.txt`. The SPA's `loginTpl` (`app.js`) carries a short
  pointer version, since a JS-rendered fetch sees that instead. A `<link rel="alternate">` in head
  covers metadata parsers.
- **the demos are machine-readable as stills**, not gifs: `llms.txt § demos` links
  `demo-<name>-end.png` (the conversation as a transcript) and `-mid.png` for the long ones. The
  stills are emitted by each demo's `cut.sh` — see `docs/demos/AGENTS.md`.
- editing `llms.txt` is a BFF push (it rides the bundle and the serve-time inline).
- **docs for agents** — the glyph (`index.html`, `app.js`) and the docs line in both sites' `llms.txt`
  link `docs/AGENTS.md` on `main`: the file a user hands their own agent to learn gradientERP, one entry
  per module a gerp is built with. A new module in `prod/per_customer/main.tf` needs its entry there, or
  `tests/gradienterp_cloud/local/test_docs_for_agents.py` fails.

## demo assets CDN (`assets.tf`)

`assets.gradienterp.cloud` = private S3 + CloudFront (OAC), the openlyoperated.biz shape. Serves
the login-page gifs + the agent stills; the login page's `DEMOS` array (`app.js`) maps rows to
gifs (`gif:` overrides the module name). **Content is `bash scripts/deploy.sh assets`** (etag-diffs
`assets/`, uploads the delta, invalidates those paths; operator-org creds — the bucket lives in
the operator account); the tf owns SHAPE only, and the s3 objects are deliberately unmanaged so a
stale apply can't clobber a deploy. The bucket also carries the demo-media archive under
`BACKUP/` — a bucket-policy Deny keeps `/BACKUP/*` off the CDN (see `docs/demos/AGENTS.md`
§ media for restore/backup).

## forwarding (scaffold) + the hardening it needs

The BFF forwards the caller's `Authorization: Bearer <id_token>` to the gerp's gateway, whose own JWT authorizer validates it. The BFF's ownership check guards *this* path. **Open hole:** the gerp gateways are still reachable browser-direct with any pool token (the cross-tenant gap). **Hardening (TODO):** lock the per-customer routes (`/secrets`, …) to the **BFF's IAM principal** (the BFF signs SigV4; the gerp route's authorizer becomes IAM trusting the BFF role) so they can't be hit except through the BFF, which is the authz gate. That replaces the browser-direct JWT authorizer currently on those routes.

## local dev

`bash scripts/local-dev.sh --start` runs this stack at `http://localhost:3000` (a registered Cognito callback origin) — `tests/server/bff`, which binds the gateway's OWN routes (taken by `tests/server/snapshot.py bff`) to the REAL handler, in-process, against the four operator tables on moto. The only prod difference is auth *validation*: APIGW validates the token there, here the bearer's claims are decoded without a signature check. Real Hosted UI login still happens, and `LOCAL_GERPS` seeds a sub → gerps map (incl. each gerp's real gateway URL) so a local round-trip hits the real deployed gerp gateway. A dogfood user (`ops+dogfood@gradienterp.cloud`) lives in the operator pool for these runs — log in as it to exercise the full Hosted-UI-PKCE → `/api/gerps` → ownership-check → gerp-gateway path.

## deploy

The web client (`web/`) is bundled INTO the BFF lambda (`gerp-cloud-bff` serves it from
`/var/task/web`), so a web change is a lambda code change — deploy it like any module lambda, no
terraform:

    bash scripts/deploy.sh push --dirs prod/gradienterp_cloud

`deploy.sh` special-cases this src-dir (`_push_webapp`): it builds `bff/main.py` + the `web/`
files, versions them to the operator artifact bucket, and update-function-codes the BFF at that
version. The lambda sources from the bucket (`data.aws_s3_object.bff`), so `terraform apply`
(operator account, `AWS_PROFILE=default`) is SHAPE-only — new routes, IAM, APIGW, env vars — and a
pointer-sync no-op after a push. A brand-new deploy is push-then-apply (the data source fails the
plan until the artifact exists).

## gaps (see TODO.md)

- a gerp's **`gateway_url`** is only filled once `per_customer` has applied (~15 min after `POST /api/gerps`), so a freshly created gerp isn't routable from the BFF until the back-fill lands.
- the per-customer-route IAM lock (hardening, above).

## the stage's own log

The `$default` stage writes `/aws/apigateway/gerp-cloud-access` (JSON, `LOG_RETENTION_DAYS`): route, status, latency, and why when the BFF never ran — `authorizerError` ("missing: token not provided" on a call with no JWT), `error`, `integrationStatus`, `integrationError`. `gerp-cloud-gateway-5xx` on the ops topic is the stage's own failure; the collector files it as a task.
