# gradienterp.cloud — open work

What's live is in [`AGENTS.md`](AGENTS.md) (routes, auth split, SPA, local dev). Open work below.

## to make it work end-to-end (prod)

- [ ] **domain (no CloudFront, no S3)** — the owner app is a tiny SPA for a few authed owners: nothing to cache, so CloudFront/S3 don't earn their place here (that pattern is for the high-traffic public `openlyoperated.biz`). Keep the **lambda serving the SPA** (HTTPS comes free from API Gateway). Just add: an **ACM cert** + an **API Gateway custom domain** + base-path map to the HTTP API + DNS. DNS wrinkle: `gradienterp.cloud` is at Squarespace — apex→AWS HTTPS wants Route 53 (ALIAS); a subdomain (`app.gradienterp.cloud`) can CNAME from Squarespace (simplest). Tradeoff: SPA is bundled in the lambda zip → updating the page = redeploy the lambda (fine while it changes rarely). **When the domain lands**, swap the hardcoded interim URL (`https://oq5y2j1trc.execute-api.us-east-1.amazonaws.com`) wherever it's pinned: the Cognito callback/logout URLs (`prod/platform/operator/cognito.tf`).

## hardening (close the cross-tenant hole)

- [ ] **lock per-customer routes to the BFF** — switch the gerp gateways' owner routes (`/settings`, future capability routes) from a browser-facing JWT authorizer to **IAM trusting the BFF role** (BFF signs SigV4 cross-account; resource policy / route IAM on the customer side allows the BFF principal). Then the only path to a gerp's owner routes is through the BFF, which is the ownership gate. Replaces the browser-direct JWT authorizer currently on those routes (e.g. `modules/settings`'s `/settings`).

## routes

`/api/public-user` scope is an account creating ITS OWN profile. Creating a `gerp_profile_id` for someone *referenced* by a business (a cash employee, no account) is a separate identity concern — undesigned.

- [ ] **`gateway_url` back-fill** — `POST /api/gerps` writes the `gerp-customers` row with `status=provisioning` and **no `gateway_url`** (the gerp's APIGW doesn't exist until `per_customer` applies, ~15 min later). Until `gateway_url` is filled, the BFF can't route to the new gerp (settings/etc.). Fill it when provisioning completes — `provision_customer` (or its codebuild) writes the endpoint back to the row, or a poll/Step-Functions step does.
- [ ] **agent chat** — `POST /api/chat`, proxy turns to the selected gerp's runtime. Needs each gerp's runtime endpoint resolved (another field alongside `gateway_url` on the `gerp-customers` row).

## go-live: landing page

The landing view is `loginTpl` in `web/app.js` (markup); styles live in `web/index.html`'s `<style>`. Hero (`.login-hero`) auto-centers, footer (`.login-foot`) pins to the bottom; the glyph row (`.login-icons`) carries the public lane (Discord invite + GitHub repo), a docs link, and a private headset-mic icon → mailto, with a Terms link in the footer. Remaining, minimal, no corporate boilerplate:

- [ ] **demo gifs** — single-column module rows are in place (`.demos` / `.demo-row`, each ending in a `.thumb` "gif" placeholder) in `loginTpl`, between Create-an-account and the glyphs; the captions are the target prompts (monthly statements, top customers, FOH coverage, treasury offer) tagged module · role. Remaining: record the 4 short gifs against the live agent chat, store under `web/` (or `docs/images/`), swap each `.thumb` for its gif. Shows the agent standing in for the accountant / BI team.
- the documents are pages of the repo on GitHub, not routes: the footer's Terms and the create screen's terms line link `docs/TERMS.md` there (`TERMS_URL` in `app.js`), a 404 until the repo is published; the landing footer and the create screen link `docs/PRIVACY.md` and `docs/DPA.md` the same way (`PRIVACY_URL`, `DPA_URL`).

## stopping a gerp — after golive

- [ ] **closing a stopped gerp** — `POST /api/gerps/close` refuses a `stopped` row because the
  closure build exports the live tables before it destroys, and a stopped gerp has none. The
  export the stop made is in the bucket already; a closure from `stopped` would skip the export,
  take that one as the closure's (`export_id`, `download_until` stamped from it) and go straight
  to the notices and the day-15 close. Worth building the day an owner can stop their own gerp
  from the screen — a *Stop* beside *Close this gerp*, the row `stopped`, nothing billed until
  *Start* — which is the same two builds the operator runs today from `deploy.sh`.

## the public profile — after golive

The object is built: the person's profile with `soc` measured and the gerp's with `naics`
measured, both empty until the count runs (the socnaics design). What follows it:

- [ ] **the page and the feed** — openlyoperated.biz `/p/<gerp_profile_id>` as one JSON read the
      HTML renders: the header from `gerp-profiles` (display name, occupations with their titles,
      city/state, links, the badge, email and phone as published) and under it the work, newest
      first, per gerp — the per-module oob reads with `?profile=<id>`, a shift shown only when the
      firm is openly operated and its contact carries the id; 404 for an id with no row; the name
      lookup beside it. The screen's `profileUrl` link points there and is a 404 until then.
- [ ] **a picture** — `picture` in the profile registry; the browser resizes to 256px square and
      caps at 256KB; the BFF issues a presigned PUT for the caller's own key under the assets
      bucket (`profiles/<id>.<ext>`) and a delete for *Remove*; the row carries the url.
- [ ] **verified** — bought: the account's default card, an invoice to the person's contact at
      Stripe Identity's fee + 20% ($1.50 → $1.80) issued and charged at once, a
      `VerificationSession` with the profile id in its metadata, Stripe's hosted page, the
      `identity.verification_session.verified` webhook setting `verified` + `verified_at`; the
      platform stores the session id, the outcome and the date, never the document; a failed
      check is still charged and the screen says so. A save that changes first, middle or last
      resets it; links, phone, a picture leave it alone.
- [ ] **profile.spec** — every owner field round-trips; the gate; a stranger loads the page with
      no session and the display name renders; deleting the account 404s it.

## a person's information — decided, not built

Cases that would add a requirement on the account owner, each settled to a shape and none needed
at golive. The second admin and the transfer are in the golive doc's after-golive list.

- [ ] **a recovery contact** — a deceased or unreachable owner's gerp keeps billing until the
      closure sequence destroys it. A second email that can request access is the smallest thing
      that changes that; Cognito does not model it, so it is a row on `gerp-accounts`.
- [ ] **a verified publisher** — a name typed at signup is a claim. If the feed's consumers want
      it checked before a business publishes: a government-id verification (Stripe Identity)
      attached to the account, shown as a badge on the public profile and the feed. Collects the
      one thing nothing else asks for, and only from accounts that publish.
- [ ] **a tax id for a B2B buyer** — reverse-charge or an exemption on the hosting fee has
      nowhere to be entered until one asks; Stripe Tax takes it on the customer.
- **a minor** — nothing collects a birthdate; the terms assert capacity to contract. If it ever
  matters it is a terms question, not a field.
- **a person who is many accounts** — nothing links two live accounts; the priors link a deleted
  one backward (AGENTS.md § deleting an account). Deliberate: the platform knows businesses.
- **the operator's own identity on platform mail** — CAN-SPAM wants the sender's postal address;
  that is gradienterp's, once, in the mail template.

- **the person's public profile asks for the whole address** as if it were the record. The
  business's public profile is optional field by field, so an owner publishes the city and state
  and leaves the street blank; the person's should default the same way — city and state for the
  match, refined by radius, the street only when turned on, `lat`/`lng` resolved at the finest
  thing published. A home-based business is found for Chicago and nobody walks to the door off
  the platform.
