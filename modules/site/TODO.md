# site — open work

Everything is open (nothing built); pulls when the first real public use case arrives — a
gerp that wants a menu / booking / order page in front of strangers. `AGENTS.md` carries the
settled design. Build order as it decomposes:

1. **the public bucket + namespace** — a per-gerp SITE bucket (public storage: SSE-S3, no
   CMK, nothing private by construction) owned here; manage_storage maps the `site/` key
   prefix to it internally (agent surface stays keys-only; `move` publishes cross-bucket);
   the `ui` lambda serves it at `GET /p/<path>` (branch before the slug gate) with its role
   scoped to the site bucket + the private prefixes it already holds. Public data routes
   are an enumerated allowlist (route names in code).
2. **the render path** — ui lambda invokes the runtime for dynamic surfaces (stream the html
   as it composes; a "composing" shell for cold renders). Requires the read-only render mode
   in modules/agent (`AGENT_MODE` tool-subset gating) FIRST — no public render before the
   mode exists. A surface declares its temperature on ITS OWN CAPTION (`temperature:
   static | warm | no-store`, read at serve time) — the object is the surface's
   registration, its last render, AND its cache policy; the link is created once by the put
   and never changes as the content re-renders.
3. **the distro ships day one; the DOMAIN is optional** — CF in front of the ui lambda from
   the start, on its default `d….cloudfront.net` host (no cert, no DNS, no owner
   involvement): the public link is behind a button on the owner's existing site, so nobody
   reads the href — and the distro is what gives WAF its home and warm/static responses an
   edge cache. The custom domain (cert us-east-1 + alias) is owner-pulled polish, added any
   time without changing a single published object.
4. **abuse posture** — Shield Standard rides the distro automatically (L3/L4); WAF rate
   rules on it for L7 (exists day one, per item 3); in the lambda: size caps + rate limits
   on public form posts, the render-spend counter + settings-row cap (serve the last warm
   render past the cap — a token-burn attack degrades the site to cached, costing the owner
   ~nothing), CSP headers on every public response. And the raw function URL CLOSES once
   the distro fronts it (CF OAC for function URLs; lambda auth → AWS_IAM) — no walking
   around WAF by finding the lambda-url host. Doctrine: anti-abuse is COST-shaped (rate +
   spend metering), never are-you-human walls — agent visitors driving the forms are
   customers (and recruitment signal), not bots to CAPTCHA out; model-authored semantic
   html is deliberately the easiest surface on the web for them to drive.
5. **visitor sessions** — cookie → runtimeSessionId issuance for surface-as-conversation.
6. **first-publish approval** — owner yes-in-chat on the first put of a public page / new
   public form kind.
7. **templates, lambda-merge mode** — `{{placeholder}}` substitution serve-side (read-time
   substitution precedent: playbooks, the BFF llms splice, the shell inject).
8. **KB guide section** — `modules/storage/kb.md` gains the public-surface
   authoring rules (what may render publicly, kind naming for public forms, the approval
   step).
9. **table booking** — the reservation page: tables are inventory CAPACITY items already
   (`net = default − scheduled`), so availability on the page is a live read and a booking
   is `reserve`. The form (`f/reservation`: party size, time, name, contact) wants INSTANT
   confirmation, so this is the one submission kind with a deterministic happy-path handler
   (watcher → `reserve` → confirmed page/SMS) instead of waiting on agent judgment — which
   makes it the case that forces the **concurrent-booking guard** (two visitors, one slot:
   `reserve` must reject the second atomically, the known gap from the hotel row in the
   usecases sweep). Declines and weird asks still fall through to the agent.
10. **the submission follow-up loop** — never block intake on completeness: accept, then
   confirm the clear and chase the unclear. Submit → 303 to a holding page carrying the
   submission id → it polls a per-submission STATUS object `{status, next}` → the QA pass
   (the watcher poke) either flips status to confirmed, or the agent authors a clarification
   form keyed to the submission (`site/followup/<id>.html` — composed for exactly this
   ambiguity) and writes its path into `next`; the holding page redirects, the answer lands
   as another submission on the same id. Tab already closed → the contact field + the email
   tool carry the question out-of-band. All existing rails: objects, the poll, forms, the
   watcher.
11. **the payment leg** — the follow-up chain's last `next`: once the guest has zero
   objections, the agent cuts the invoice (invoicing rails, draft → issued), authors the
   guest's invoice page keyed to the submission id (an unguessable path — a capability link
   only this guest holds), and its one button is PAY — navigating to the STRIPE-HOSTED
   payment page (a payment link created for the invoice, metadata = invoice_id +
   submission_id; the per-customer Stripe secret is already vaulted). Stripe's webhook lands
   like any charge → ingest → `record_invoice_paid` → books closed; return_url drops the
   guest on a confirmed page. Card data never touches the ui lambda. The only new piece:
   creating the payment link. An objection instead of payment is just the clarification loop
   again — "something wrong?" posts back into the same submission thread.
12. **fulfillment = what invoicing already offers** — receive the paid event, publish it to
   `pay_invoice` (`record_invoice_paid`, live rails), and the owed work needs NOTHING created:
   state lives on invoice ITEMS (`manage_invoice (op: transition)`, open vocabulary — a diner's
   `made`/`served` is vocab like a hotel's `check-in`, zero code), so "paid and pending
   fulfillment" is the `manage_invoice (op: get)` FOLD: paid invoices whose items haven't reached their
   done-state. The staff screen is a `/data` route serving that fold (auto-refreshing like
   the tasks page); "made it" is a form posting `manage_invoice (op: transition)`; any money rules attached
   to the entering state fire by attachment (COGS/stock on the inventory items being sold),
   and a pure annotation state posts nothing. Vendor-independent for free — any rail that
   pays the invoice pends the same fold.

13. **the public MCP door** — homed on modules/server (the per-gerp public APIGW where
   webhooks + `/oob` already live, routes declared module-side per the oob.tf pattern):
   `POST /mcp` → an mcp lambda speaking thin JSON-RPC over the SAME handlers the forms hit.
   TF owns the shape once (route + lambda + throttle); the TOOLS are DATA: the agent
   declares one by putting a schema object at `site/tools/<name>.json` — `tools/list` reads
   the prefix, `tools/call` dispatches the named kind into the submission intake — so
   adding an mcp resource is a put, same freedom as authoring a form. The one
   code-enumerated exception: tools with direct deterministic effects (`reserve_table`'s
   instant confirm binds to its handler); data-declared tools land as submissions the agent
   QAs, so a declaration can't create effects by itself. A visitor's own agent connects and
   the breakfast prompt is two tool calls and a pay link. APIGW brings the throttle floor
   natively (usage plans, per-route rate limits, WAF attachment). Anonymous by design (the agent GATEWAY stays the
   private SigV4 surface); effects stay gated by the deterministic handlers / owner-agent
   judgment. The llms-note demotes to a breadcrumb pointing at the endpoint; html forms
   remain the human skin over the same kinds. Split: server = the machine door (webhooks,
   oob, MCP), ui lambda = the human door (pages, forms).

Non-goals here: order/booking EFFECTS (invoicing / inventory rails own them, under the
owner agent's judgment); the render mode itself (modules/agent); serving/intake code
ownership (modules/storage — this module adds routes and wraps them in the boundary).
