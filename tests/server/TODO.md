# tests/server — open work

## the websocket half of `openlyoperated_biz`

Its three routes (`$connect` / `$disconnect` / `$default`) are in `image.json` but not bound. The
handler answers by calling back through `apigatewaymanagementapi` against a `WS_ENDPOINT`, so
closing the loop needs a shim that routes `post_to_connection` to a local socket instead — the same
trick `_LocalLambdaClient` plays for `invoke`. FastAPI serves websockets natively, so the missing
piece is only the callback.

Until then the dashboard loads and its read works; live counters do not.

## `openlyoperated_biz` has no tables

Its two (connections, counters) are operator-side, and `tests/testdata/table-schemas.json` is swept
as ONE CUSTOMER — `snapshot_schemas()` cannot see them. Same gap that leaves the five operator
singletons hand-kept in that file. Fix is one sweep per account, or two credential sets in one.

## shapes for what is not a table

31 table shapes are snapshotted; buckets, the event bus and its rules, scheduler groups and SSM
paths are not. `make_bus` and `make_bucket` build them from arguments at each call site instead.
A stack that needs one of those stood up has nowhere to read its shape from.

## a second gerp needs its own environment

DONE: the bus delivers (`pump.py`), and the cross-firm path runs end to end for LOOPBACK — emit →
the hub's spoke rule → that gerp's bus → its consume rule → its inbox.

What is left is the second tenant. A handler reads `os.environ["LEDGER_TABLE"]` and the runner sets
one union across the stack, so `receive_inbound` for gerp B would write gerp A's tables. The env
has to be selected per-gerp at delivery, keyed on the bus the spoke rule put to.

The env for a gerp that does not exist in AWS cannot be derived from gradienterp's by substitution:
49 values are tenant-scoped, 14 are not, and the brand string IS the dogfood tenant's id
(`agent@gradienterp.agents.gradienterp.cloud` is both at once). Snapshot a second real gerp and the
diff gives the rule exactly — which is an argument for vending `westwood` (the second live tenant) before
building this, not after.

## a cross-account invoke runs in the CALLER's process, with the caller's env

`POST /api/export` (the BFF) invokes a customer account's `export_gerp`. Locally
`modules/aws/aws.py` dispatches that in-process, so the callee executes inside the bff stack, which
does not carry the callee's env — `export_gerp` does `os.environ["CUSTOMER_ID"]` and raises KeyError.
The payments pair does not hit this only because those lambdas default their env with `.get()`.

So the local harness can exercise the route's own branches (ownership, argument checks, sync vs
async dispatch, the derived arn) but not the callee. Unit tests cover the arn and the modes;
end to end it is prod-only until this is fixed.

- [ ] **give a cross-stack invoke the callee's env** — the per_customer image already holds it, so
      the pump could look the function up there rather than running it bare.
