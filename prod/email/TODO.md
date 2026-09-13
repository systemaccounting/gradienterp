# prod/email — open work

- **Tighten DMARC to `p=reject`** — currently `p=quarantine` (in `aws_route53_record.dmarc`).
  Once you've watched live mail a few days and confirmed nothing legitimate is being quarantined
  (forwards align via SES DKIM, so they should pass), bump the policy to `p=reject`.

- **Bounce + complaint handling beyond the suppression list** — AWS asked for this explicitly when
  granting production access (2026-08-06, case 178606059100092): *"Set up a process to handle bounces
  and complaints."* What exists today is the SES account-level suppression list (BOUNCE + COMPLAINT
  enabled), which is real and was cited in the request — a bad address is suppressed account-wide and
  removes itself. What does NOT exist is visibility: no configuration set, no event destination, so
  nobody learns that a customer's invoice bounced. Add a configuration set with an SNS/EventBridge
  event destination for `BOUNCE` / `COMPLAINT` / `DELIVERY_DELAY` and route it somewhere that
  surfaces — a task on the operator gerp is the obvious sink, since that is already the escalation
  intake. Matters more once invoices go out (golive phase 4): a silently bounced invoice reads as an
  unpaid customer.
- **Reputation is now ours to keep.** Out of sandbox at 50k/day, bounce and complaint rates are
  account-level metrics AWS enforces on. The `test+` addresses are safe (they resolve to our own
  catch-all), but any spec that mails a fabricated external address would bounce and count against
  the account. Keep generated recipients on `@gradienterp.cloud`.
