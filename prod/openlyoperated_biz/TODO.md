# openlyoperated_biz — open work

`AGENTS.md` covers what this dir is + how to deploy. The consumption model + open work below.

## consumption model — two scopes

two load-bearing principles. first, **the platform never stores a copy of the businesses' data.** second,
the scopes are **two different consents** — a terms-of-use line:
- **economic aggregate = terms of use.** every business's economic activity feeds the index by being on
  the platform — no opt-in. we're building the economy's books; a business *is* part of the economy. not
  interested in businesses that pretend otherwise.
- **per-business detail = opt-in** (the `openly_operated` toggle) — publishing your own identifiable books.

the invariant across both: **public = the business's economic activity + public-profile entities; the
personal namespace (individuals' contacts/PII, worker-legal, secrets) is never served.** that's a different
axis than economic opacity — "part of the economy" makes the books public, not a barista's SSN. the platform
stays module-agnostic; a new module surfaces on oob.biz with no platform-side wiring. two disjoint scopes:

1. **per-business — read-through from public-profile data (no filter).** a business page loads its
   numbers + operations (accounting statements, inventory levels, open orders, schedules, tasks — the
   business's financial and operational dashboard) **directly from that gerp's own tables**, keyed by
   `gerp_profile_id`.
   we **source public data, we don't filter private data**: the public surface is composed of
   public-profile-shaped entities (a schedule of public roles/shifts, inventory by SKU, orders by
   status); the private namespace — contacts/PII, worker-legal, secrets — is simply never in the public
   read path. no runtime PII filter; the separation is structural.
   - eg viewing the corner cafe's schedule serves a public profile's shifts — never the private contact
     behind the shift or their legal docs.
   - extensibility: a new module contributes its public reads to the page; its private records aren't
     public by construction. no per-field classification, no filter to forget.

2. **economic — every event counts (no store, no gate).** neither an economic-vs-non gate nor an opt-in
   gate: **every** business's event fires at the platform → one generic **counter lambda** → atomic `ADD`
   on a schemaless counters DDB (`pk = signal#period`, e.g. `revenue#2026-07`). `signal` = the event kind,
   which just *partitions* the counters. the only variation is **magnitude vs occurrence**: an event with an
   amount sums it, a bare event does `+1`. oob.biz features + queries these as an **economic index** across
   all businesses. a new event kind = a new counter key, no schema change.
   - **no k-floor.** counters are lit by default; the query console slices freely. a business's number
     reconstructable from a thin slice (`revenue by category × zip`, cell of one) is **intended + consented
     in the ToU** — a transparency platform, not one that lets a business pretend it's not part of the
     economy. suppression logic would contradict the thesis.
   - because the counter takes everyone, the event-level `openly_operated` flag + the publication rule are
     **not** the gate for anything → they retire (see consequences); emitters stop stamping the flag.
   - **live feed = `/oob/counters` on the stream** — every delta as it lands, `{key, op, magnitude,
     period, at}`, never a gerp id: aggregate-only by construction. a published gerp's own events ride its
     own channels, projected through their contracts.

**consequences:**
- **retire the store-everything archive.** `prod/api_openlyoperated`'s firehose→S3→athena kept a copy of
  every published event — dropped; we don't store a copy of all biz data on the platform. the query
  console queries the counters + reads through to gerps, not athena-over-archive. the line is
  BUSINESS data (a firm's tables: read-through, never copied) vs ECONOMIC data (what firms emit as
  they trade: every gerp, the ToU baseline, the platform's to record) — the counters are the first
  store of the second, and a per-gerp event store (columnar for SQL, series, graph; metered through
  api keys; a query page for humans) is the later one. the FRED-class data product.
- **retire the event `openly_operated` flag + publication rule.** aggregate = ToU baseline (all events),
  display = read-through gated on the settings DDB flag → nothing consumes the event-level flag. emitters
  stop stamping it; the `{"detail":{"openly_operated":[true]}}` rule degrades to "route economic events to
  the counter," no filter. the DDB settings flag stays — it's the display opt-in. (this makes Stream R's
  emit-side flag *read* vestigial — the SSM→DDB move stands, the stamping doesn't.)
- [ ] **S3 backup routine** — a lighter, periodic backup, separate from the retired real-time archive.
  a safety net, not a queryable store.

## the agent interface comes first

Every page is one JSON read. `GET /p/<gerp_profile_id>` renders what the same url returns with
`Accept: application/json`; a business page renders its gerp's `GET /oob` sources the same way.
Nothing appears on a page that its JSON lacks — the HTML is a renderer with no data of its own,
and a person who wants the substance without the giftwrapping asks for the JSON or points an agent
at it. Three pieces make that hold, none of which gate golive:

- **the profile is a source, not a special reader.** A person's page is the registry header plus
  the same per-module oob reads with `?profile=<gerp_profile_id>` — a module that publishes
  shifts publishes shifts by person through the same route, gated the same way. The feed is those
  reads, newest first, per gerp.
- **one door for an agent.** A public MCP endpoint at openlyoperated.biz whose tools are the
  directory lookup (`find_profiles` over the registry, published gerps only) and the per-gerp
  `/oob` reads proxied by profile id — `tools/list` is the catalog, `tools/call` is a read. One
  url, no session, everything the pages show as JSON.
- **`llms.txt` names the contracts.** The routes, the query parameters, the MCP endpoint, and the
  shape each source returns, kept in step with the catalog.

## open work

- [ ] **more sources, more kinds** — each module's `oob.tf` registers a source; the page gets one
  renderer per new `kind` (unknown kinds render as a labelled stub). `iot` is the first non-ddb
  kind (`GET /oob/energy`, `iot_timeseries`).
- [ ] **the regions the api does not serve** — opportunities, the lens, the ticker, the event
  stream panel: each names its call in `index.html`'s top comment; the SaaS and gerp-count
  metrics are `gh/044`.
- [ ] **search autocomplete** — a name index on `gerp-profiles` for prefix matches; sector and
  place filters ride `GET /v1/gerps` already.
- [ ] **`GET /v1/screener`** — the screener's fan-out as one api resource (`prod/api_openlyoperated/TODO.md`).
- [ ] **the subscribe key** — hand-pasted into `index.html` from `terraform output events_subscribe_key`;
  it rotates when the key does (AppSync caps a key at a year).
- [ ] **query console** — `/query` over the counters and the read-through.

## the feed aggregator as a direct route (merged from the optimizer plan, 2026-07-17)

the counters/index design above is the CONSUMER shape; its input is the `journal_entry.posted`
route on the gerp-events bus (LIVE — the publication Firehose→S3 archive is already accumulating
the raw material, so the aggregator can bootstrap as Athena over data already landing). two
event-side gaps to mind when building: the event doesn't yet carry the entry's `dimensions`
(per-location slices need the additive v2 field — `modules/events/TODO.md`), and sector
aggregation needs a `business_category` join operator-side (it's in the tenant blob, not on the
event). routing sequence + routed-list mechanics: `prod/optimizer/TODO.md` § status + the routing
sequence.
