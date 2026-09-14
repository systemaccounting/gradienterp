# openlyoperated.biz

CloudFront + S3 static dashboard at `openlyoperated.biz` — **live**. The public-facing surface for the openly-operated subset of customers. A pure CDN edge (no lambda / no BFF): the page fetches its data from `api.openlyoperated.biz` and subscribes on `events.openlyoperated.biz` client-side. This dir never reaches into customer sub-accounts.

## current features

- **the economy** — the `kpi` region: `revenue`, `expense` and `margin` off `GET /v1/economy/counters`, rendered as metric cards; `revenue` and `expense` repaint when `/oob/counters` delivers a delta over the WebSocket door (the public subscribe key is in the page).
- **the business screener** — `GET /v1/gerps`, then each gerp's `financials` through the read-through; one row per business, a row opens `#b/<gerp_id>`.
- **search** — the masthead input on Enter: `GET /v1/gerps?q=`, a picker overlay, the curl under the results.
- **the business page** (`#b/<gerp_id>`) — the directory row for its name and place, `GET /v1/gerps/<id>/sources` for the catalog, each source through `GET /v1/gerps/<id>/sources/<key>`, rendered by `kind`; the curl under every section; an *as it happens* section subscribed to `/oob/<gerp_id>/journal-entry-posted`.
- **`llms.txt`** — the site in three calls on the api and the stream, and the metric shape.
- **security headers** — `aws_cloudfront_response_headers_policy.site` on every response: a CSP that allows no
  inline script (`script-src 'self'`; connections to `api.openlyoperated.biz` and `wss://events.openlyoperated.biz`;
  Google Fonts for styles and fonts; `frame-ancestors 'none'`), `nosniff`, HSTS, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy: same-origin`. So a page's script is a file: `index.html` loads `main.js`,
  `index.mock.html` loads `mock.js`, beside `app.js`. A new connection or outside asset goes in the policy first.

Every read goes through `getJson` with `cache: 'no-store'`, so the page shows the api's answer now and a live repaint reads the table, not the browser's copy of a minute ago.

## what's deployed

- `versions.tf` / `main.tf` — S3 (private origin, OAC) → CloudFront → apex + `www`, ACM cert DNS-validated against the `prod/dns` zone. `main.tf` uploads everything under `web/` (content-type by extension). State key `openlyoperated_biz/terraform.tfstate`. Operator sub-account; `api.openlyoperated.biz` is the only backend.

## front-end (lit-html, no build)

Framework-light + agent-first: vendored **lit-html** (`web/vendor/lit-html.js`, ~8kb ESM), **no bundler** — the deployed file IS the source. Shared shell in `web/app.js` (helpers, app `state`, control handlers, masthead/persona/tab-bar/footer chrome, `mount()`); shared styles in `web/app.css` (desktop 3-column / phone 3-tab IA). Two thin entry pages inject their own region templates into `mount()`:

- `web/index.html` — the live main page: a **reactive empty scaffold**. Regions render the drained/annotated state; each names the api/stream that fills it (`oo-needs` + a data-contract manifest in the file's top comment). Masthead carries a **mock site ↗** link to the demo (`mockLink:true`). Safe to ship — nothing fabricated.
- `web/index.mock.html` — the seeded **demo** (the populated target), with a "mock data for demonstration" banner + live tick. A visual reference, not real data.

**Adding a view** = a new region template function dropped into a page's `mount()` (or a new thin entry reusing the shell) — not a copy of the chrome. Wiring real data = swap a region's seeded source for an `api.openlyoperated.biz` read. Don't reach for a build-step framework (React/Vite/etc.) — it would break the no-build / source-is-artifact / agent-legible properties this is built on.

## the business page renders by kind

`#b/<gerp_id>` resolves a business through `GET /v1/gerps`, fetches its catalog and every source
in parallel through the read-through, and renders each by the catalog row's `kind` — one renderer
per kind (`RENDERERS` in `web/index.html`), a labelled stub for a kind with none yet. Two businesses
publishing different sources get different pages from one client that knows neither; a new kind
is one renderer. `metrics` renders first whatever the catalog order: it is the header, the
statement and the rest the detail.

**Metrics are one shape, a gerp's own or the economy's** — `{key, label, unit, grain, headline,
points, definition, source: {curl}}` — and `metricCard` renders one (every value from the url or the api goes through `esc` before it meets markup; a published label is text): the label and period, the
headline in its unit, the trend, the definition sentence (the metric's text of record), the call
that produced it as a copyable line, and *propose a change*, a link to the repo's metric issue form
(`.github/ISSUE_TEMPLATE/metric.yml`) with the key and the definition filled in. A viewer is treated as the owner of the number they are
looking at: they can call it, read what it means, and propose what it should mean. The page never
shows a number it cannot hand you the call for.

## the agent-readable surface

Raw markup is an empty shell (client-rendered), so both entry pages open with a visually-hidden
agents note pointing at `/llms.txt` — the site description, the planned data contracts, and where
the substance lives (repo, docs, console, discord). `llms.txt` ships as a plain `web/` object, so
editing it is the same apply as any page edit. Keep the note and `llms.txt` in step with the
gradienterp.cloud pattern (`prod/gradienterp_cloud/AGENTS.md` § the agent-readable surface) —
that site inlines its llms.txt at serve time via the BFF; this one has no server, so the note
stays a pointer.

## deploy / update

```
bash scripts/apply.sh --stack openlyoperated_biz        # --plan to stop after the plan
```

Base creds = management (`default` profile); the provider assume-roles into the operator account. `apply` re-uploads changed `web/*` objects; they carry `Cache-Control: max-age=60`, so the edge refreshes within ~1 min — no manual CloudFront invalidation for routine edits.

## the page demos the api

Every number on a page is one call on `api.openlyoperated.biz/v1`, and the call is printed beside
the number. The page holds no data source but the api and the stream, and no aggregation of its
own past a client-side fan-out; the answer to a scale case is the api, not a bigger page.
`tests/e2e/published.spec.mjs` is that in one assertion: a fact posted on gradienterp's ledger
shows on the page at the api's value, and every data request the page made went to the api or
the stream. Regions the api does not serve yet (opportunities, the lens, the ticker) stay
annotated with the call that will fill them.

## not an openly-operated customer

`openlyoperated.biz` is the dashboard, not a business with books. The operator's own openly-operated books live in the `gradienterp` customer sub-account (the dogfood customer), and their public surface comes through this dashboard like any other openly-operated customer's.
