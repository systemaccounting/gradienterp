"""The one place the local/AWS decision is made.

`AWS_LAMBDA_FUNCTION_NAME` already tells a process two things at once: you are not in Lambda, and
you should not touch real AWS. That is a good invariant and this module keeps it — it just changes
what "not real AWS" means. Instead of every caller carrying a second, hand-written implementation
that appends to a jsonl, the SAME boto3 call goes to a LOCAL endpoint.

    in Lambda            → boto3, real endpoints
    not in Lambda        → boto3, http://localhost:5000 (moto_server)

Absence of the variable still cannot reach production: an unset endpoint is not a fallback to real
AWS, it is a local address that fails to connect if nothing is listening. The failure mode stays
safe, and it gets LOUD instead of silently diverging.

WHY THIS EXISTS. The old shape put `if IS_LAMBDA:` at every call site — 258 of them across 54
files, each pairing a real AWS write with a jsonl imitation of it. That is two implementations of
every operation, and the imitation is always the weaker one: it does not evaluate
`ConditionExpression`, does not reject floats the way `put_item` does, does not enforce a GSI's
sparseness, and does not run the registry's fail-closed validation. Every one of those gaps has
shipped a prod-only bug — `private_values` silently dropped, `post_journal_entry` deduping on the
wrong key locally, floats accepted locally and rejected in prod. One code path removes the class.

USAGE — replace `boto3.client(...)` / `boto3.resource(...)`, nothing else changes:

    from aws import client, resource, table
    ddb   = resource("dynamodb")
    tbl   = table(os.environ["TASKS_TABLE"])
    evt   = client("events")
"""

import os

# Set in every Lambda runtime by AWS itself. Empty ⇒ not in Lambda ⇒ do not touch real AWS.
IN_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

# moto_server by default — Apache-2.0, pure Python, runs as a plain process, so the test loop needs
# no Docker. `scripts/test.sh` starts one if none is up. AWS itself ships only DynamoDB Local, which
# would leave the event-emitting lambdas with nothing to talk to. Overridable to point at a different
# port or a shared dev stack later.
LOCAL_ENDPOINT = os.environ.get("LOCAL_AWS_ENDPOINT", "http://localhost:5000")

# The local endpoint ignores credential values, but botocore still requires them to sign.
_LOCAL_CREDS = dict(aws_access_key_id="local", aws_secret_access_key="local")

_cache: dict = {}


def json_default(o):
    """What `json.dumps(..., default=…)` should use on anything read back from DynamoDB.

    A number written to DDB comes back a `Decimal`, and `default=str` turns it into `"2"` — so a
    caller reading a response sees a string where the row holds a number, and an agent relaying it
    passes a string on. Numbers stay numbers here; whole values go out as ints so a count of 3 does
    not arrive as 3.0.

    Everything else still falls back to `str`, because that is what the callers replacing
    `default=str` were also relying on for datetimes.
    """
    from decimal import Decimal
    if isinstance(o, Decimal):
        return int(o) if o % 1 == 0 else float(o)
    return str(o)


def _kwargs(service: str) -> dict:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    if IN_LAMBDA:
        return {"region_name": region}
    return {"region_name": region, "endpoint_url": LOCAL_ENDPOINT, **_LOCAL_CREDS}


def client(service: str, **overrides):
    """A boto3 client — real in Lambda, local otherwise. Cached per service.

    `overrides` (e.g. `config=Config(signature_version="s3v4")` for a presigned GET of an SSE-KMS
    object) are passed through to boto3 and NOT cached, since the whole point is a client that
    differs from the shared one."""
    if overrides:
        import boto3
        return boto3.client(service, **{**_kwargs(service), **overrides})   # an override wins (region_name for another region's bus)
    key = ("c", service)
    if key not in _cache:
        import boto3
        _cache[key] = boto3.client(service, **_kwargs(service))
    return _cache[key]


def resource(service: str):
    """A boto3 resource — real in Lambda, local otherwise. Cached per service."""
    key = ("r", service)
    if key not in _cache:
        import boto3
        _cache[key] = boto3.resource(service, **_kwargs(service))
    return _cache[key]


def table(name: str):
    """The DynamoDB Table resource for `name`. The document interface either way, so callers keep
    passing Python-native values and keep getting Decimals back — including locally, which is the
    point: `put_item` rejects floats in LocalStack exactly as it does in prod."""
    return resource("dynamodb").Table(name)


def reset() -> None:
    """Drop cached clients — for tests that change endpoint or region between cases."""
    _cache.clear()


# ─── cross-lambda invoke, locally ────────────────────────────────────────────
#
# A lambda that calls another (`POST_JOURNAL_ENTRY_FN`, `UPDATE_STOCK_FN`, …) used to write the
# payload to a jsonl locally, so the test asserted what was SENT and never what the other side did.
# Instead, dispatch in-process: resolve the function name back to its source and call the real
# handler. The callee's own logic runs — assets posting an acquisition entry actually reaches
# accounting's ledger — and there is nothing to keep in sync, because the last segment of a deployed
# function name IS its src dir (the same `fn → src_dir` mapping `scripts/deploy.py` relies on).

import importlib.util
import io
import json
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parents[2]


def _local_handler(function_name: str):
    """`gerp-accounting-gradienterp-post_journal_entry` → that lambda's handler.

    Imported FRESH each call, deliberately. Lambdas read their config into module-level constants at
    import (`LOCAL_LEDGER = os.environ.get(...)`), so a cached handler would pin the first caller's
    environment and every later test would write to the first one's files. This path only ever runs
    locally — in Lambda the real client is used — so the import cost is a test-time concern only."""
    src = function_name.rsplit("-", 1)[-1]
    # `prod/` holds lambdas too — the tower's provisioner, the optimizer's hub — and the BFF calls
    # one, so both trees are searched.
    hits = sorted(h for pattern in ("modules/*/lambdas/%s/main.py", "prod/*/lambdas/%s/main.py")
                  for h in _REPO.glob(pattern % src))
    if not hits:
        raise RuntimeError(
            f"local invoke: no modules|prod/*/lambdas/{src}/main.py for '{function_name}'. "
            "Cross-invokes resolve by the function name's last segment.")
    path = hits[0]
    # The zip puts every bundled lib at its ROOT, so an import that resolves to a nested dir
    # (`transform` lives in modules/accounting/lambdas/ingest/) is a flat `import transform` in the
    # deployed function. Reuse deploy.py's resolver rather than guessing a path set — it is the
    # thing that decides what actually ships, so the two cannot drift.
    _front(_bundle_dirs(path))
    spec = importlib.util.spec_from_file_location(f"_localfn_{src}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.handler


def _front(dirs: list[str]) -> None:
    """This bundle's dirs first on sys.path, and any cached module that one of them shadows evicted.

    Every bundle flattens to a root, and two bundles name the same module (`_helpers`, `transform`)
    with different contents. In Lambda each function has its own process; here they share one, so
    the first bundle imported would otherwise answer every later `import _helpers`."""
    import os
    sys.path[:] = dirs + [p for p in sys.path if p not in dirs]
    for d in dirs:
        for f in pathlib.Path(d).glob("*.py"):
            m = sys.modules.get(f.stem)
            file = getattr(m, "__file__", None) if m is not None else None
            if file and os.path.dirname(os.path.abspath(file)) not in dirs:
                del sys.modules[f.stem]


def _bundle_dirs(main_py: pathlib.Path) -> list[str]:
    """Every directory the deployed zip would flatten into its root, for `main_py`."""
    lambdas_root, module_root = main_py.parent.parent, main_py.parent.parent.parent
    dirs = [str(main_py.parent), str(lambdas_root)]
    try:
        # loaded by PATH, not by name: this file is itself bundled into lambdas, and an
        # `import deploy` statement here would be a dangling import for deploy's own resolver.
        spec = importlib.util.spec_from_file_location("_deploy", _REPO / "scripts" / "deploy.py")
        deploy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deploy)
        resolved = deploy._local_imports(str(main_py), str(lambdas_root), str(module_root), {})
        dirs += [str(pathlib.Path(v).parent) for k, v in resolved.items() if k != "__vendor__"]
    except Exception as e:                             # noqa: BLE001 — fall back to the flat guess
        print(f"[local invoke] import graph unavailable for {main_py.name}: {e}")
    return dirs


class _LocalLambdaClient:
    """The `lambda` client when not in Lambda: invoke runs the target handler in this process.

    Only `invoke` is special — anything else falls through to the real (local-endpoint) client, so
    control-plane calls behave as they would against the emulator."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def invoke(self, FunctionName, Payload=b"{}", InvocationType="RequestResponse", **kw):  # noqa: N803
        event = json.loads(Payload if isinstance(Payload, (str, bytes)) else json.dumps(Payload))
        try:
            result = _local_handler(FunctionName)(event, None)
        except Exception as e:                      # noqa: BLE001
            # Lambda does NOT propagate a callee's exception to the caller — it returns 200 with
            # `FunctionError` set and the error serialized as the payload. Letting it propagate here
            # would make the caller's except-clauses fire on a path that cannot fire in production,
            # and would hide the ones that should.
            import traceback
            return {"StatusCode": 200, "FunctionError": "Unhandled",
                    "Payload": io.BytesIO(json.dumps({
                        "errorMessage": str(e), "errorType": type(e).__name__,
                        "stackTrace": traceback.format_tb(e.__traceback__),
                    }).encode())}
        return {"StatusCode": 202 if InvocationType == "Event" else 200,
                "Payload": io.BytesIO(json.dumps(result).encode())}

_real_client = client


def client(service: str, **overrides):  # noqa: F811 — wraps the factory above
    c = _real_client(service, **overrides)
    if service == "lambda" and not IN_LAMBDA:
        key = ("local-lambda",)
        if key not in _cache:
            _cache[key] = _LocalLambdaClient(c)
        return _cache[key]
    return c


# ─── the line a handler writes ───────────────────────────────────────────────
#
# One JSON object per line: the level, the message, the gerp, the function and whatever ids the
# call site names, every one a top-level field. In Lambda the function runs with JSON log format
# (modules/terraform/lambda), the root logger writes the record and `extra` spreads the fields
# beside `timestamp`, `level`, `message`, `requestId` and `location` (the call site, by
# `stacklevel`) — so a filter reads `{ $.level = "ERROR" }` or `{ $.po_id = "…" }`, and any log
# backend parses the line on ingest. Locally the same object is printed.
#
#     from aws import log, Kind, Failure
#     log.error("stock move failed", po_id=po_id, item_id=item_id, response=out)
#     log.exception("settle effect raised", thread=thread)      # + the traceback, error, error_type
#     log.warning("mailbox list unreadable, using the default", error=e)
#     log.info("routed", detail_type=dt, handler=handler)
#
# What a failure record is:
#
#     kind        the type — snake_case, fixed, one per failure kind. A `Kind` declared once in the
#                 module's `_kinds.py` (`log.error(STOCK_MOVE_FAILED, po_id=…)`), or the slug of a
#                 fixed-string message ("stock move failed" → stock_move_failed). Both accepted.
#     category    what class of thing broke: dependency | permission | config | data | timeout.
#                 On the Kind, or classified from the exception's type and botocore code.
#     error_type  type(e).__name__; error  str(e)
#     the ids     named what the table's key is named (IDS below); anything else is a value.
#                 Two are written under a wire name because a LogRecord owns the word: a call
#                 site's `thread=` lands as `thread_id`, `name=` as `resource_name`; and `kind=`
#                 (an agreement's kind) lands as `agreement_kind`, since `kind` is the record's.
#
# `bind(po_id=…)` at the top of a handler puts an id on every line of the invocation; `Failure`
# is the raise that carries the record so a generic catch (`stream_batch`, a handler-level
# `except Exception`) writes it whole. The level is the line's kind: `error` a failure (the work
# did not happen), `warning` a fallback taken, `info` the narrative and a refusal. The message
# itself is never JSON: a dict or a JSON string as the message nests as text and no filter can
# read it. Under test (LOG_STRICT=1) a field outside IDS ∪ VALUES raises; in prod nothing here
# ever raises.

import contextvars
import logging
import re

# the id vocabulary: a field is named what the table's key is named, and nothing else
IDS = frozenset({
    "gerp_id", "account_id", "aws_account_id",                       # the four-id model
    "thread", "terms_hash",                                            # an agreement row
    "po_id", "invoice_id", "item_id", "entry_id", "task_id", "contact_id", "shipment_id",
    "instrument_id", "session_id", "event_id", "sequence", "inbound_id", "script", "name",
    "worker_id", "export_id", "unit",
})
# what a line may carry that is not an id
VALUES = frozenset({
    "error", "error_type", "kind", "category", "function", "location", "status", "code", "count",
    "reason", "response", "attempt", "attempts", "limit", "detail", "detail_type", "side",
    "sender", "buyer", "seller", "issuer", "holder", "to", "from_gerp", "from_account", "account",
    "handler", "decided", "poke", "fn", "op", "provider", "host", "address", "mailbox", "sub",
    "period", "amount", "total", "moved", "dispatched", "holes", "assignee", "type", "needs",
    "reasons", "skipped", "journal_entry_id", "operator_gerp", "window_days", "balances_table",
    "log_group", "hours", "messages", "test_to", "machine", "prefix", "group", "stream", "product",
    "expected", "old_email", "new_email", "user_id", "vended_at", "registry", "bucket", "spec_key",
    "script_key", "automation", "tool", "args", "event", "label", "task", "customer", "movement_type",
    "quantity", "n", "start", "end", "session", "loc", "location_id", "target", "level",
    "execution", "payment_method_id", "endpoint_id", "build_id", "message_id", "secret_name",
    "subject", "ticket", "key", "path", "param", "raised_at", "opened", "struck", "closed",
    "accounts", "quota", "ou_accounts", "hub",
})
CATEGORIES = ("dependency", "permission", "config", "data", "timeout")
STRICT = bool(os.environ.get("LOG_STRICT"))

_root = logging.getLogger()
_LEVELS = {logging.ERROR: "ERROR", logging.WARNING: "WARNING", logging.INFO: "INFO"}
# a LogRecord owns these names, so the runtime refuses them in `extra`: two of ours are written
# under a wire name (a call site still says `thread=` and `name=`, what the table's key is called)
_WIRE = {"thread": "thread_id", "name": "resource_name", "kind": "agreement_kind"}
_RESERVED = {"message", "msg", "level", "timestamp", "requestId", "args", "exc_info", "levelname",
             "filename", "module", "lineno", "funcName", "pathname", "process", "asctime"}
_bound: contextvars.ContextVar = contextvars.ContextVar("aws_log_bound", default={})


def _slug(message: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:80]


class Kind:
    """A failure kind, declared once in a module's `_kinds.py`: the identifier a metric counts,
    a task keys on and an investigator filters by; its message for a person; its category; the
    ids a line of this kind carries. An id outside IDS fails here, at import."""

    def __init__(self, kind: str, message: str, category: str, ids=()):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", kind):
            raise ValueError(f"kind {kind!r} is not snake_case")
        if category not in CATEGORIES:
            raise ValueError(f"kind {kind!r}: category {category!r} is not one of {CATEGORIES}")
        bad = [i for i in ids if i not in IDS]
        if bad:
            raise ValueError(f"kind {kind!r}: ids {bad} are not in aws.IDS")
        self.kind, self.message, self.category, self.ids = kind, message, category, tuple(ids)

    def __repr__(self):
        return f"Kind({self.kind})"


class Failure(Exception):
    """A raise that carries the record: `raise Failure(STOCK_MOVE_FAILED, po_id=…, item_id=…)`
    (or a message string). Whoever catches it writes it whole — `stream_batch`, a handler-level
    catch — and reads the fields as attributes (`e.status`)."""

    def __init__(self, what, **fields):
        # `what` (not `kind`): a call site may carry a `kind=` field of its own — an agreement's
        # kind — which lands on the line as `agreement_kind`
        import sys
        f = sys._getframe(1)   # the raise site: a generic catch logs it, and `location` names the catch
        self.raised_at = f"{os.path.basename(f.f_code.co_filename)}:{f.f_code.co_name}:{f.f_lineno}"
        self.kind = what if isinstance(what, Kind) else None
        self.message = what.message if isinstance(what, Kind) else str(what)
        if self.kind:
            unknown = [k for k in fields if k not in self.kind.ids and k not in VALUES]
            if unknown:
                raise TypeError(f"{self.kind!r} does not declare {unknown}")
        self.fields = fields
        super().__init__(self.message)

    def __getattr__(self, item):
        try:
            return self.__dict__["fields"][item]
        except KeyError:
            raise AttributeError(item) from None


def classify(e) -> str:
    """The category of a plain exception, from its type and (botocore) code."""
    if isinstance(e, Failure) and e.kind:
        return e.kind.category
    code = ""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        code = str((resp.get("Error") or {}).get("Code") or "")
    name = type(e).__name__
    text = f"{name} {code}"
    if re.search(r"AccessDenied|Unauthorized|not authorized|Forbidden", text, re.I):
        return "permission"
    if re.search(r"Throttl|TooManyRequests|ServiceUnavailable|InternalServer|ConnectionError|EndpointConnection", text, re.I) \
            or (name == "HTTPError" and int(getattr(e, "code", 0) or 0) >= 500):
        return "dependency"
    if re.search(r"Timeout|TimedOut", text, re.I):
        return "timeout"
    if re.search(r"ParameterNotFound|ResourceNotFound|NoSuchKey|NoSuchBucket", text, re.I):
        return "config"
    if name in ("KeyError", "ValueError", "TypeError", "JSONDecodeError", "UnicodeDecodeError", "AttributeError"):
        return "data"
    return "dependency"


def bind(**ids):
    """Ids for every line of this invocation; `bind()` with nothing clears."""
    _bound.set(dict(ids) if ids else {})


class _Log:
    def _emit(self, level, msg, exc_info, ctx, exc=None):
        kind = None
        if isinstance(msg, Kind):
            kind, msg = msg, msg.message
        elif isinstance(exc, Failure):
            kind = exc.kind
            ctx = {**exc.fields, "raised_at": exc.raised_at, **ctx}
            if msg is None:
                msg = exc.message
        fields = {"gerp_id": os.environ.get("GERP_ID", ""),
                  "function": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
                  **_bound.get()}
        if level == logging.ERROR:
            fields["kind"] = kind.kind if kind else _slug(msg)
            if kind:
                fields["category"] = kind.category
            elif exc is not None:
                fields["category"] = classify(exc)
        if exc is not None:
            fields.setdefault("error", str(exc))
            fields["error_type"] = type(exc).__name__
        for k, v in ctx.items():
            if STRICT and k not in IDS and k not in VALUES:
                raise KeyError(f"log field {k!r} is not in aws.IDS or aws.VALUES")
            key = _WIRE.get(k, f"{k}_" if k in _RESERVED else k)
            fields[key] = v if isinstance(v, (str, int, float, bool, type(None), list, dict)) else str(v)
        if IN_LAMBDA:
            if _root.level > logging.INFO:
                _root.setLevel(logging.INFO)
            _root.log(level, msg, extra=fields, exc_info=exc_info, stacklevel=3)
        else:
            print(json.dumps({"level": _LEVELS[level], "message": msg, **fields}, default=str))
            if exc_info:
                import traceback
                print(traceback.format_exc(), end="")

    def error(self, msg, **ctx):
        self._emit(logging.ERROR, msg, False, ctx)

    def exception(self, msg=None, **ctx):
        """The line for the exception being handled: the traceback, `error`, `error_type`, and a
        `Failure`'s kind and fields. `msg` may be omitted for a Failure."""
        import sys
        exc = sys.exc_info()[1]
        self._emit(logging.ERROR, msg, True, ctx, exc=exc)

    def warning(self, msg, **ctx):
        self._emit(logging.WARNING, msg, False, ctx)

    def info(self, msg, **ctx):
        self._emit(logging.INFO, msg, False, ctx)


log = _Log()


# ─── a DynamoDB stream batch ─────────────────────────────────────────────────
#
# A handler on a table stream that raises poisons its shard: the batch is retried until the record
# expires and everything behind it waits. A handler that returns after a failed record has consumed
# it. `stream_batch` is the third way: each record runs alone, a raise becomes one line and one
# entry in `batchItemFailures`, and the mapping (`modules/terraform/stream`) retries that record by
# itself and parks it on the queue when it keeps failing.
#
#     def handler(event, context):
#         return stream_batch(event, _one)          # _one(record) raises on a failure, returns on a skip
#
# A refusal inside `_one` — a record this handler does not act on — is a return, never a raise.
# A `Failure` raised inside carries its kind and ids onto the line.

def stream_batch(event: dict, one) -> dict:
    """`results` carries what `one` returned for each record it acted on (None is a skip) — the
    mapping ignores it; a test reads it."""
    failed, results = [], []
    for record in event.get("Records", []):
        try:
            out = one(record)
            if out is not None:
                results.append(out)
        except Exception as e:  # noqa: BLE001 — the line and the report ARE the handling
            seq = (record.get("dynamodb") or {}).get("SequenceNumber") or record.get("eventID", "")
            log.exception(None if isinstance(e, Failure) else "stream record failed",
                          event_id=record.get("eventID", ""), sequence=seq)
            failed.append({"itemIdentifier": seq})
    return {"batchItemFailures": failed, "results": results}


# ─── the owner routes ───
#
# A gerp's HTTP API authorizer checks that a token comes from the operator's pool and the
# gradienterp.cloud client — which every account that signs up holds. The routes that act as the
# owner (settings, invoices, `/automate/`) add the part the authorizer can't: the token's `sub` is
# this gerp's owner, read from `OWNER_SUB_PARAM` (the `owner_sub` parameter provisioning writes).

_owner_sub: dict = {}


def refuse_non_owner(event: dict):
    """The 403 to send when a request through the HTTP API carries Cognito claims whose `sub` isn't
    this gerp's owner; None to go on. A direct invoke or an IAM-signed request carries no claims and
    passes. In Lambda a missing parameter refuses; off Lambda (the local stack, the tests) no
    parameter means no owner to check against."""
    claims = (((event or {}).get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims")
    if not claims:
        return None
    forbidden = {"statusCode": 403, "headers": {"content-type": "application/json"},
                 "body": json.dumps({"error": "this gerp's owner only"})}
    param = os.environ.get("OWNER_SUB_PARAM", "")
    if not param:
        return forbidden if IN_LAMBDA else None
    if param not in _owner_sub:
        try:
            _owner_sub[param] = client("ssm").get_parameter(Name=param)["Parameter"]["Value"]
        except Exception:
            return forbidden
    return None if claims.get("sub") and claims.get("sub") == _owner_sub[param] else forbidden

