# site module

The PUBLIC surface on the gerp's web substrate. The substrate — the `ui` lambda serving
agent-published objects from the cabinet bucket, taking form posts into `submissions/` — is
built and live in `modules/storage` (the owner portal rides it today, slug'd, `pages/`).
This module is the public twin: the PUBLIC bucket (`site/` keys), no slug, a real domain, and the
security boundary that anonymous visitors require. Design settled 2026-07-31; nothing here
is built.

## current features

Nothing deployed — this module is design + the build ledger (`TODO.md`). What it will own,
and what deliberately lands elsewhere:

- **site owns**: the PUBLIC BUCKET — the world-bound half of the storage split (cabinet =
  private storage: receipts, legal, portal pages, submissions; site bucket = public storage:
  public pages, templates, images — nothing private ever, BY CONSTRUCTION; SSE-S3, no CMK,
  CF-originable for static). Plus: serving it at `GET /p/<path>` (no slug — visitors are
  anonymous), the CF distribution + cert + domain alias, WAF rate rules, the public-render
  spend counter, visitor-session issuance, CSP on public responses.
- **lands elsewhere**: the constrained render MODE (modules/agent — an `AGENT_MODE` with a
  read-only tool subset), order/booking EFFECTS (invoicing / inventory / purchasing rails,
  always under the owner agent's judgment), the serving/intake code paths (the `ui` lambda,
  modules/storage). The agent's publishing surface stays keys-only: manage_storage maps the
  `site/` key prefix to the public bucket internally — path = visibility survives, one tool,
  two stores; draft→publish is one `move` (CopyObject crosses buckets).

## the requirement

A visitor asks for the menu and the agent renders it on demand — a model turn in the
visitor's path. The page is a RESPONSE, not a maintained artifact: at render time the agent
reads whatever it deems dependent (inventory for 86s, the open queue for wait times, its own
judgment for specials). Prices are catalog reads at render time — the site cannot drift from
the books.

And the html is the on-ramp, not the product: the KIND is the interface, and its
agent-native door is MCP on modules/server — `POST /mcp` on the per-gerp public APIGW
(where webhooks + `/oob` already live; APIGW throttling is the abuse floor), JSON-RPC over
the same handlers the forms hit (`TODO.md` item 13). A visitor's own agent discovers
`reserve_table` / `place_order` via `tools/list` and "window table 30min@8am + corned beef
hash" is two tool calls and a pay link, no page involved. Server = the machine door, the ui
lambda = the human door, same kinds and effects behind both. Pages are the human skin; the
llms-note is a breadcrumb to the MCP endpoint; agent traffic on it is the signal for when
the skin stops mattering (and, between gerps, for recruitment onto the bus — the
browse-drives doctrine applied to inbound).

## one rail, three temperatures (the owner's knob)

visitor → ui lambda → [maybe] invoke runtime → serve. Caching is a per-surface header
decision, not architecture:

- **no-store** — every visitor gets their own render (personalized, time-aware; costs a
  turn per door-open). Unlocks surface-as-conversation: per-visitor sessions (cookie →
  runtimeSessionId), so "anything gluten free?" re-renders as an answer.
- **warm** — micro-TTL / event-invalidated; first visitor of the window pays the render.
- **static** — brochure pages, images: plain objects, no model.

Temperature + the render spend cap are settings rows — the owner's cost variables, enforced
in the lambda (the continuation-budget pattern).

## templates: optional, never required

No template (the default): the agent improvises a clean page — zero onboarding friction. A
template is just an object (`public/templates/<name>.html`), acquired conversationally any
time: the agent composes one, or the owner hands one over ("i made this in claude design").
Two run modes: *agent-as-filler* (agent reads template + books, emits the page) or
*lambda-merge* (`{{placeholders}}` + an agent values-json, substituted serve-side — the
model never emits markup, per-visitor renders collapse to a small values json). Owner taste
without a template rides the standing-instruction rail (`instruct`).

## the public security boundary

Anonymous strangers reaching an agent is a different threat model than the slug'd portal —
this boundary is the module's reason to exist:

- **constrained render mode** — visitors PROMPT the agent, so public renders run under a
  read-only tool subset by ARCHITECTURE (`AGENT_MODE` gating, the hub/spoke mechanism), not
  persona discipline. Writes are unreachable from the visitor's path.
- **throttle** — Function URLs have no built-in rate limiting: WAF rate-based rules on the
  CF distro + size caps in the lambda.
- **spend metering** — a per-window counter checked before every runtime invoke; past the
  owner's cap, serve the last warm render instead.
- **reflection / XSS** — visitor text rendered into agent-authored html: CSP headers on
  every public response + escaping doctrine in the render mode.
- **first-publish approval** — publishing to the WORLD is an outward act: the first put of
  a public page / new public form kind wants an owner yes in chat (interrupt machinery
  exists). The namespace rule holds regardless: nothing personal ever renders.
