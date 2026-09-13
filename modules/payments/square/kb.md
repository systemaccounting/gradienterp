# connecting Square

Connecting Square posts your payments, refunds, and payouts to your books
automatically. You don't have to configure anything in Square beyond copying one
token — I create the webhook for you. This connects your **live** account; the
token is powerful, so the flow is built around using it once and rotating it.

1. Log into the **Square Developer Console** at developer.squareup.com and open (or create) your application.
2. Keep the console in **Production** (the toggle at the top of the application).
3. Go to the application's **Credentials** page, find the **Production Access Token**, and click **Show** to reveal it. This is the token I'll use to create your webhook subscription — it manages webhooks for the application. One token covers it.
4. Copy the token.
5. I'll open a secure form right here in the chat — paste the token into it and submit. The token goes **straight to your vault**; it never passes through me (I only learn that it saved). Don't type the token into the chat itself — only into the form field.
6. With that name, I'll use the token **once** to create your webhook subscription (pointed at your `…/webhooks/square` ingest URL, subscribed to **payment.updated**, **refund.updated**, and **payout.sent** events) and store the subscription's **signature key** securely. I never see or keep the token.
7. **Right after I confirm the webhook is created, rotate the token** on the same Credentials page (Replace token). The webhook keeps working — verification against your books is your next real payment, refund, or payout landing on its own, and I'll confirm it when it does.

Square's Webhook Subscriptions API is owned by your *application*, not by a
seller, so it doesn't accept a per-permission scoped key the way Stripe does —
the access token is full-access. That's why the flow is use-once-then-rotate:
the token's whole exposure is the minute it sits in your vault before the
webhook exists, and you close that window yourself. It never passes through the
assistant either way — the form writes straight to your vault.

> **Testing against Square Sandbox instead?** A Sandbox token only works when
> the platform operator has pointed this stack at
> `connect.squareupsandbox.com` — tokens and environments can't mix. The live
> flow above is the default for a real business.
