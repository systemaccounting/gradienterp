# tests/server — the stacks, running locally

Lambda CODE is already a 10-second `deploy.sh push`. Everything else about a stack — a new route, a
new table, an env var — is a `terraform apply`, and that is exactly what designing a surface
touches. This is the loop that removes the apply: bring a stack up locally, curl it, edit, curl
again.

**Generate, never hand-write.** Every route, every table name, every key schema comes from
something AWS already knows. The moment a resource is typed into a Python literal, this has become
the thing it was built to delete — the two that existed (a mock's route handlers, the BFF's table
list) are gone, and `image.overlay.json` is the one sanctioned exception, for a route that does not
exist yet.

## current features

- **three stacks as plain processes**, started together by `bash scripts/local-dev.sh --start`:
  `bff` :3000 · `per_customer` :8080 · `openlyoperated_biz` :3001, over moto on :5000. `--stop`
  ends every running copy of each surface, found by the module it runs (`pgrep -f`) rather than by
  pidfile or port, so a copy another session started by hand goes too; `--start` does the same
  first and starts fresh. A non-surface process on a surface's port is reported and left alone.
- **routes bound to the REAL handler**, imported from source and run in-process — not a mock, not a
  reimplementation. A request runs the signature check, the transform, the cross-invoke and the
  write that ship
- **`image.json` per stack**, taken from the RUNNING stack by `snapshot.py` and committed, so the
  loop needs no AWS. Nothing about a route, a table name or an env var is authored by hand
- **real state**: every table the stack owns, with its deployed name and key schema, created on moto
  at startup from `tests/testdata/table-schemas.json`
- **handler code is live-edit** — the handler is re-imported per request, so a change shows on the
  next curl with no restart
- **`image.overlay.json`** for a route you are still designing, before it exists in AWS at all
- **events are delivered**, not just accepted: `pump.py` runs the bus's real rules and hands each
  match to its target handler, so an emit reaches its consumer — and every emit is logged

## the pieces

```
tests/server/
  snapshot.py            # <stack> → tests/server/<stack>/image.json, by asking AWS
  _image.py              # the shared runner: state, routes, dispatch, $default
  pump.py                # the bus's lambda targets, delivered (no port)
  bff/                   # server.py + image.json   :3000
  per_customer/          # server.py + image.json   :8080
  openlyoperated_biz/    # server.py + image.json   :3001
  platform/  tower/      # image.json ONLY — no server, no port (see "dependencies")
```

## the model

Everything here is one idea applied four times: **ask the running system what it is, then run the
real code against a local emulator.**

```
  a request        →  the route table the GATEWAY reports  →  the REAL handler, in-process
  a table          →  the shape the LIVE table reports     →  created on moto, with its real name
  a handler's env  →  what the FUNCTION reports            →  set before the call
  an emit          →  the rules the BUS reports            →  the REAL target handler
```

Nothing in that column on the right is written by hand, and nothing in the middle is a mock. The
consequence is that a local failure is a real failure: a query naming an index that does not exist
fails here, a rule pattern that matches nothing delivers nothing here, and a handler that reads an
env var the deployment does not set raises here.

Three things are deliberately NOT reproduced, because reproducing them would only be reproducing
AWS: the JWT authorizer (claims are decoded, not verified), IAM (every call is allowed), and the
model behind the agent — a real network call either way, and the one thing no emulator should fake.

**The agent, locally.** `modules/agent/dev` runs the turn loop in-process, with the 83 tools
DISCOVERED from their `schema.json` (the same declarations the AgentCore Gateway registers) and
each call dispatched to the real handler against this stack. It defaults to the Anthropic API
(`ANTHROPIC_API_KEY`); `AGENT_PROVIDER=bedrock` switches to production's transport and model id
(`us.anthropic.claude-sonnet-4-6`) using your AWS creds — same messages/tool_use shape either way,
so only the credentials change. What stays absent is the MCP hop: production reaches a tool through
the Gateway, and locally the dispatch is in-process, so gateway-level failures (a target that will
not sync, a description over 200 chars, a name collision in the shared namespace) do not appear
here.

### what a request actually does

`POST /webhooks/stripe` on :8080 →`_image.py` builds the payload-format-2.0 event → the real
`ingest_stripe` handler runs, its signature check and transform included → it cross-invokes
`post_journal_entry` through `aws.py`'s in-process lambda client → that writes DynamoDB rows on
moto → it emits on the bus → `pump.py` sees the match and runs the target. A later
`GET /oob/financials` reads those same rows back.

A stack's `server.py` is ~20 lines and supplies only what is genuinely its own: which tables to
create, what to seed into them, and whether a `$default` route serves files.

## what `image.json` holds, and where each part comes from

| part | source |
|---|---|
| routes | `get-routes` + `get-integrations` + `get-authorizers` |
| each function's env | `get-function-configuration`, with the `REDACT` values in `snapshot.py` (the owner's address, the portal slug) written as placeholders |
| each function's source dir | the `gerp:src-dir` tag |
| which functions are in the stack | the `gerp:stack` tag |
| Function URLs | `get-function-url-config` on the function |
| table shapes | `tests/testdata/table-schemas.json` (its own `gerp:layer` snapshot) |

**Not the OpenAPI export**, though it looks like the obvious source. It drops `$default` — the
BFF's SPA catch-all, and the route that makes an unrouted `/api/*` a 401 rather than a 404 — and
OAS cannot model a websocket API, which `openlyoperated_biz` is. `get-routes` covers all three
stacks with one call shape.

Refresh one stack: `python3 tests/server/snapshot.py <stack>`.

## the loop

| you changed | what it takes |
|---|---|
| a handler, `_helpers`, a shared lib | nothing — the next request re-imports it |
| a file under `web/` | nothing — reload |
| `_image.py` or a stack's `server.py` | `bash scripts/local-dev.sh --restart` |
| a route, an env var, a table (in terraform) | apply → `snapshot.py <stack>` → `--restart` |

That last row is the honest boundary: `image.json` describes what is deployed, so a route that does
not exist in AWS does not exist here — unless you say so explicitly, which is the overlay.

### designing a route before it exists

`tests/server/<stack>/image.overlay.json` is merged over `image.json` and wins. Same shape; its
routes and functions are additive, and the runner announces them at startup so a local-only route
is never mistaken for a shipped one. Gitignored.

```json
{
  "routes": [{"route_key": "POST /credit-notes", "method": "POST", "path": "/credit-notes",
              "function": "gerp-invoicing-gradienterp-draft_credit_note"}],
  "functions": {"gerp-invoicing-gradienterp-draft_credit_note":
                {"src_dir": "modules/invoicing/lambdas/draft_credit_note", "env": {}}}
}
```

Write the handler at that `src_dir` and curl it. Terraform catches up later.

### secrets are under the `local` prefix

A lambda reads its processor keys from `/gradienterp/customers/<CUSTOMER_ID>/secrets/<name>`, and
`CUSTOMER_ID` is unset in the local BFF's process — so locally that path is
`/gradienterp/customers/local/secrets/…`, NOT `…/gradienterp/…`. Seeding the tenant path and
wondering why the lambda still says "no secret" is the way this is discovered.

moto holds them in memory, so a seed lasts until the next `--restart`.

```python
client("ssm").put_parameter(Name="/gradienterp/customers/local/secrets/stripe_billing",
                            Value="rk_test_…", Type="SecureString", Overwrite=True)
```

The seed writes `sk_local` there at start, because cards do not go to Stripe locally at all — see
the next section. To run the real test-mode flow instead, start with `LOCAL_STRIPE_URL=off` and put
a Stripe TEST key there; the live key would create real customers against real cards and does not
belong here.

### putting the stack in a state

`curl localhost:3000/dev` lists what the local stack can be put into — an account, its record,
a card, a gerp row, a signed-in browser — each a curl that answers with what it did. The e2e
fixtures' local branches call the same verbs. Local server only (`bff/dev.py`).

### cards without Stripe

Every lambda that talks to Stripe reads `STRIPE_API_BASE` from its env and builds paths under it, so
the stand-in is a base-url swap and nothing else: `tests/server/stripe/server.py` on :4242, and the
real `payment_links`, `save_payment_method`, `manage_saved_cards` and `charge_saved_method`
run unchanged against it. `_stripe_local.py` is the caller-side half — the override on each image
and the key + signing secret seeded under every prefix a function reads.

It models the paths those four call and nothing else; a Stripe path it lacks is a 404 that says so.
The hosted page is the point: `GET /c/{session}` is a form, submit creates the card on the
session's customer, completes the setup intent and the session, and sends the browser to the
session's `success_url` — :3000, where the SPA already calls `save-card`. The number typed decides
the card (4… Visa, 5… Mastercard, 3… Amex; ending 0002 saves but declines when charged). A charge
posts `charge.succeeded` to :8080, signed with `whsec_local`, so `ingest_stripe` verifies it and the
invoice moves the way it does in production.

Not modelled: card validation, 3-D Secure, disputes, refunds, Stripe's own errors beyond one
decline. Those stay unit tests against canned bodies. In memory; `--restart` wipes the cards with
moto.

### the seller's customer hook

`_hooks_local.py` runs at the bff's seed what the operator ran once in prod: the two firm scripts
`prod/gradienterp/automations/upsert_customer_contact.py` and `erase_customer_contact.py` are put
under `automations/approved/modules/` in the moto cabinet, `manage_hooks` runs in-process with
`HOOKS_BASE_URL` pointed at the local gateway, and the answers land at
`/gradienterp/cloud/hooks/customers_upsert` and `.../customers_erase` in moto SSM (one caller, so
both parameters carry the token of the last publish). A save on Info & Billing then posts to
`:8080/hooks/customers/upsert`, and the seller's contact shows on `GET /dev/account/<sub>` as
`contact`; `DELETE /dev/account/<sub>` runs `DELETE /api/account` as that account, which posts
to `customers/erase` and leaves the contact a shell. `GET /dev/priors` lists what deleted
accounts left behind. A failure here prints `[hooks] not published` and the stack comes up
without it.

### tax without Stripe

The stand-in answers `POST /v1/tax/calculations` the way the charge adapter reads it:
`amount_total` and `tax_amount_exclusive`, zero tax unless `LOCAL_STRIPE_TAX_RATE` (e.g. `0.0725`)
is set, applied to every line. `/v1/payment_intents` accepts the calculation hook and the tax
metadata as any other field.

### the login without Cognito

Same seam, client side: `cognito()` in the SPA posts to `CFG.COGNITO_IDP`, which on localhost is
`tests/server/cognito/server.py` on :4243. It models signup, confirmation, the login-email change
(`UpdateUserAttributes` → `VerifyUserAttribute`, the old address staying the login until the code
lands) and `REFRESH_TOKEN_AUTH`, accepts `000000` as every code, and mints unsigned tokens whose
`email` claim moves on verify — so `GET /api/account` writing the row from the claim runs for real.
A session that never signed up through it — the e2e helper's token, one typed into a console — is
admitted on its first call, created from the token's own `sub` and `email`; a refresh token
`rt_<sub>` names that user. The Hosted-UI sign-in itself is still real Cognito or the unsigned
JWT; the stand-in does not serve `/oauth2/*`.

The seller's card lambdas run in the BFF's process because `bff` depends on `per_customer`
(`scripts/tags.json`) — the BFF invokes them by ARN in production, and `depends_on` is where a
cross-stack call is declared. Both `image.json`s have to carry them: a stale snapshot is a
`seller function not configured` 502 two hops in.

### auth

There is no authorizer here — production's APIGW validates the token before a handler sees it, and
reproducing that locally would only be reproducing AWS. Claims come from `x-debug-sub`, or from an
unverified decode of a real bearer, so a real Hosted-UI login against :3000 works.

```
curl localhost:3000/api/gerps -H 'x-debug-sub: <cognito-sub>'
```

## events

`emit_event` is a real `put_events`, and moto does the matching — patterns are evaluated exactly as
production evaluates them. What moto cannot do is invoke a lambda TARGET, because these functions
are source on disk, not zips in its Lambda service. `pump.py` fills that one gap:

```
put_events → moto matches the rule → an SQS queue per rule → pump → the real handler
```

The buses, their rules, patterns and targets come from the running stacks — three of them:
`snapshot.py hub` (the hub's `gerp-events`: a spoke edge per gerp whose target is that gerp's own
bus, and the forward to the operator's), `snapshot.py platform` (the operator's `gerp-operator`:
the counters, the publisher, the reports), `snapshot.py per_customer` (the gerp's
`gerp-internal-<gerp>`: the inbox's consume rule, the automation and collection rules). So the
local buses route the way the deployed ones do. A rule whose target is a bus is delivered by a put
onto the local bus of that name — a new event there, as a bus-to-bus target lands; a lambda
target runs the real handler. The firehose archive is reported at startup as not delivered rather
than silently skipped.

**Every emit is logged**, matched or not, by a catch-all rule on each bus that exists only to show it:

```
[emit] gerp-events · purchasing · quote.requested to=gradienterp  {"from":"gradienterp","to":"gradienterp",…}
[pump] gerp-events: matched gerp-edge-spoke-gradienterp
  → bus gerp-internal-gradienterp: put
[emit] gerp-internal-gradienterp · purchasing · quote.requested to=gradienterp  {…}
[pump] gerp-internal-gradienterp: matched gerp-inbox-gradienterp-consume
[inbox] recorded quote.requested from gerp=gradienterp
  → gerp-inbox-gradienterp-receive_inbound: ok
```

An `[emit]` line with no `→` under it matched no rule — which is the failure a pattern change
causes and the one you would otherwise never see.

## multiple gerps

Cross-firm is the expensive thing to test against AWS: two accounts, `put-resource-policy` on both
a runtime and an endpoint arn, and container logs you cannot read. Locally the same path is one
process, and the pieces are in place:

- **table names carry the tenant** — `make_table(logical, gerp)` gives
  `gerp-accounting-tanners-coffee-co-ledger`, so two firms' books coexist on one emulator
- **the edges run for real.** The hub's spoke rule for a gerp puts onto that gerp's bus and the
  gerp's consume rule invokes its `receive_inbound`; an event addressed to a gerp with no edge
  matches nothing, and that is visible as an `[emit]` with no `→` under it
- **loopback works today**: a gerp addressing itself emits, matches its own spoke edge, lands on
  its own bus and in its own inbox, which is the whole path minus the second tenant

What is still needed for a SECOND gerp is its environment. A handler reads `os.environ["LEDGER_TABLE"]`,
and the local runner sets one union across the stack — so `receive_inbound` for gerp B would write
gerp A's tables. The env has to become per-gerp at dispatch, keyed on the recipient.

Deriving gerp B's env from gerp A's is not a string substitution: of the values mentioning
`gradienterp`, 49 are tenant-scoped (`gerp-accounting-gradienterp-ledger`) and 14 are not —
`CHAT_BASE_URL` is `https://gradienterp.cloud/chat`, brand not tenant, and `AGENT_ADDRESS` is
`agent@gradienterp.agents.gradienterp.cloud`, which is both in one string. **The dogfood tenant and
the brand are the same word**, so no rule read off gradienterp alone can tell them apart. Snapshot
a second real gerp and the diff IS the rule: what differs is tenant-scoped, what matches is brand.

## dependencies between stacks

All the processes share one moto, so state is shared and a cross-stack call just works — the BFF's
`POST /api/gerps` invokes tower's provisioner in-process. What does NOT come for free is the
callee's CONFIG: it lives in the other stack's `image.json`.

`scripts/tags.json` declares who depends on whom (`bff → platform, tower`) and `_image.py` merges
those functions in. That is why `platform/` and `tower/` have an `image.json` and nothing else:
they are not surfaces, they are config other stacks reach into.

## gotchas

- **A `prod/` root packages with terraform's `archive_file`, which is a hand-listed manifest.**
  `scripts/deploy.py` walks the import graph; these do not. Adding an import of a shared lib to a
  lambda under `prod/optimizer`, `prod/api_openlyoperated`, `prod/tower` or `prod/platform` means
  adding a `source` block for it too, or the function ImportErrors at cold start. `modules/` lambdas
  are built by deploy.py and are not exposed to this.
- **One process, one environment.** Production gives each lambda its own; here the base is the union
  across the stack and the entry function is overlaid per request. Where two functions disagree on a
  var, majority wins and the runner PRINTS the disagreement at startup — read that line rather than
  assuming.
- **A snapshotted `/var/task/...` path is wrong locally, and setting `os.environ` will not fix it.**
  `run()` re-applies each function's snapshotted env before every request, so a value set once at
  startup is overwritten on the first call. Correct it on the IMAGE in the stack's `seed` — the bff
  does this for `WEB_DIR`.
- **HTML goes to the handler, not to `static`.** The shell carries placeholders the handler
  substitutes at serve time (`<!--LLMS-->`, `<!--PURCHASE-TERMS-->`); serving it off disk would hand
  the browser literal comments where production has content. Everything else under `web/` still
  comes straight from disk, so an edit shows on reload either way.
- **A function with no `gerp:src-dir` cannot be resolved.** There is a fallback glob over
  `modules|prod/*/lambdas/<name>/main.py`, but it misses anything nested deeper (platform's lambdas)
  or outside a `lambdas/` dir (the BFF), and it picks the first match when a name repeats across
  modules. Tag the lambda.
- **A route path with `{proxy+}` binds as `{proxy:path}`.** The handler reads
  `pathParameters.proxy` either way.
- **Two bundles, one module name.** Every bundle flattens to a root, and `_helpers` in one module
  is not `_helpers` in another. `aws._front(dirs)` puts the entry function's bundle dirs first on
  `sys.path` and evicts any cached module one of them shadows, before each fresh import — the route
  runner and the in-process `lambda.invoke` both go through it.
- **`ctx.call` on the local stack invokes the tool's lambda in this process** (`_gateway._local`,
  on `LOCAL_GATEWAY_URL`); there is no AgentCore gateway to sign for. The reply is the tool's own
  body and a non-2xx is a `GatewayError`, so a script sees what it sees in prod.

## not covered yet

The websocket half of `openlyoperated_biz`, its two tables, shapes for anything that is not a
table, and the bus→lambda delivery that cross-firm needs: `tests/server/TODO.md`. Plus anything
moto does not implement — AgentCore's data plane most notably, see `tests/AGENTS.md`.

## handlers are cached on their source mtime

`handler_for` execs a handler module once and reuses it, re-execing only when the source file's
`st_mtime_ns` changes — so an edit still shows on the next curl, and module-level state persists
between requests in between.

A module is exec'd under its OWN function's env rather than the stack's base union, since its
import-time constants are now read once. At call time `run` applies only the delta between a
function's env and the base `base_env` set at startup; a function that matches the base — every
function in a single-function stack — is called with no env swap at all.
