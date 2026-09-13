# connecting Stripe

Connecting Stripe posts your charges, refunds, and payouts to your books automatically, and
gives me Stripe's own tools — the balance, products, payment links, tax, disputes — so the
things you used to do in the dashboard I do here. You approve access once at Stripe; no key is
copied between screens.

## the approval

1. I run `manage_mcp {op: install, provider: stripe}` and hand you a link. Say whether I may
   write to Stripe (`write`) or read only; the webhook needs the write.
2. Open the link. Stripe shows its own sign-in, then **Choose an environment** — pick your
   **live** account (Test mode is for trying things; a webhook made in test mode never sees a
   real charge). Then **Set permissions**: choose **Customize** and set **Webhook Endpoints**
   to **Write**; the rest can stay Read. Review, Authorize.
3. You land back on gradienterp.cloud and the connection finishes. My first Stripe call after
   that hands you one more link — the firm's own grant, once — approve it the same way.
4. Then `configure_webhook {provider: stripe}`: I create your webhook endpoint at your ingest
   url, subscribed to charge / refund / invoice / payout events, and store its signing secret.
   Stripe may ask you to approve that one write on its own page first; I send that link and
   run it again when you say you approved. The secret comes back to the platform, never to me.
5. Verification is your next real Stripe activity: the first charge, refund, or payout after
   connection lands on your books on its own, and I confirm it when it does. A test-mode
   payment proves nothing here: test events go only to a test-mode endpoint, and yours is live.

**The approval's environment must match the collecting key's mode.** A webhook is created in the
mode of the account that makes it. If you approved test mode and your collecting key is live,
setup refuses and says so; approve the live account.

## a key instead

A firm that prefers a key to the approval flow makes one restricted key in Stripe (**Developers →
API keys → Create restricted key**) with one permission, **Webhook Endpoints: Write**, and submits
it through the secure form I open in the chat (it goes straight to the vault; never type it into
the chat itself). Then `configure_webhook {provider: stripe, secret_name}` uses it once to make
the endpoint, and the key can be deleted in Stripe afterwards. Setup refuses a test-mode key
beside a live collecting key for the reason above.

## the second key — only if they want me to COLLECT

Everything above is money coming IN and landing on the books. It does not let me charge anyone or
send a payment link. **Do not ask for this unless the owner actually wants that** — a firm whose
customers pay however they already pay needs only the webhook, and asking for more access than the
job needs is how you lose their trust.

If they do want it — charge a saved card when an invoice is issued, email a pay link, let a customer
save a card — it is a restricted key with these permissions:

| permission | what it is for |
|---|---|
| **Payment Intents — Write** | charging a card the customer saved |
| **Checkout Sessions — Write** | the hosted page behind a payment link, and behind "save my card" |
| **Customers — Write** | creating the customer record when they first save a card |
| **Payment Methods — Read** | reading back the card they saved |
| **Setup Intents — Read** | following a completed save-a-card page to the card it saved |

Everything else stays **None**. Same secure form, same vault. It is stored as `stripe_billing`.
The charging tools read a key today; the firm's approval reaching them as well is open work.

**Be straight with them about how this one is different.** It **stays**, and it **can move money**
— that is the whole point of it. What it still cannot do: read payouts, change bank details, issue
refunds, or touch anything outside the list above. If they want it gone later, deleting it in
Stripe stops collection and leaves the books working, because those run off the webhook.

**Setup can be re-run safely.** Running `configure_webhook` again creates a fresh webhook and
disables the old one (Stripe's tools have no delete; a key deletes it), so the owner never ends up
with duplicates delivering everything twice. The signing secret only exists in the create
response, which is why re-running makes a new endpoint rather than reusing the old one.

**A missing permission looks like a 403, not a bug.** A charge that comes back *"does not have the
required permissions for this endpoint"* means the key exists and one of the rows above is set to
None — the owner opens the key in Stripe, sets it, and saves; nothing needs re-entering. A
`configure_webhook` that comes back 403 saying the approval is read-only means the Stripe
approval lacks Write on webhook endpoints: `manage_mcp uninstall`, then `install` and approve
again with it.
