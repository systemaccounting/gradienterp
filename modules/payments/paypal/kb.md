# connecting PayPal

Connecting PayPal posts your payment captures, refunds, and payouts to your books
automatically. You don't have to configure anything in PayPal beyond making one
app and copying its two credentials — I create the webhook for you. This connects
your **live** account.

PayPal is different from Stripe and Square in one way: it doesn't hand out a
single key. Your app has **two** credentials — a **Client ID** and a **Secret** —
and I need both. So you'll submit two secrets through the same secure-form flow,
one at a time.

1. Log into the **PayPal Developer Dashboard** at developer.paypal.com with your business account, on the **Live** toggle.
2. Go to **Apps & Credentials**. Your account already has a default app; open it, or click **Create App** to make a fresh one named something like "gradientERP setup".
3. On the app's page, find the **API credentials** section. It shows two values — the **Client ID** and the **Secret** (click **Show** to reveal the Secret). These are app-level credentials: the Client ID identifies the app, and the Secret authenticates it. Treat the Secret like a password.
4. I'll open a secure form for the **Client ID** first — paste it in and submit. Then I'll open a second form for the **Secret** — paste that in and submit.
5. Both values go **straight to your vault**; neither passes through me (I only learn they saved). Don't type either one into the chat itself — only into the form fields.
6. With those two names, I'll call `configure_webhook`. It uses the Client ID + Secret to create your webhook (pointed at your `…/webhooks/paypal` ingest URL, subscribed to **PAYMENT.CAPTURE.COMPLETED** and **PAYMENT.CAPTURE.REFUNDED**) and stores the **webhook id** it returns. I never see the credentials.
7. Verification is your next real PayPal activity: the first capture or refund after connection lands on your books on its own, and I'll confirm it when it does.

One PayPal-specific note: these credentials **stay in your vault and stay
live** — PayPal's security model has us call back to PayPal with them to verify
every single event it delivers, so unlike Stripe and Square there's no
use-once-then-delete here. Both values go to your secure store rather than into
chat, so neither ever passes through the assistant. If the Secret is ever
compromised, rotate it in **Apps & Credentials** and resubmit it through the
same form — ingestion pauses until the new value lands.

> **The two-credential difference.** Stripe and Square each give you a single
> key; PayPal splits identity (Client ID) from authentication (Secret), and the
> webhook-management API needs both. That's why you submit two secrets here
> instead of one.

> **Testing against PayPal Sandbox instead?** Sandbox app credentials only work
> when the platform operator has pointed this stack at
> `api-m.sandbox.paypal.com` — credentials and environments can't mix. The live
> flow above is the default for a real business.
