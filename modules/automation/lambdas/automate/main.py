"""automate — the modules-kind runner: executes an APPROVED script from the cabinet.

The script is firm code nobody on the platform read: an agent wrote it, a cold agent
turn reviewed it against the playbook, and `approve_automation` copied it into
`automations/approved/modules/`. Two things keep that safe, and neither is code in
this file.

**Approval is a path.** This role can read `approved/modules/` and nothing else under
`automations/`, so an unapproved script is not where GetObject looks. AccessDenied IS
the answer — there is no approved-flag to check and no check to forget.

**The role is the boundary.** `exec` runs the script in this process, so a script that
ignores `ctx` and imports boto3 gets exactly these grants: invoke this firm's own gateway,
read the approved prefix, write this log. No SSM (one GetParameter on the secrets path and
any script reads the whole vault), no direct lambda invoke, no DDB.

`ctx.rules` needs no grant for the same reason: the rule instances arrive IN the payload. What a
rule computes never depends on an earlier result — that is what makes it a rule — so its params
are known before the script starts, and whatever invoked the script resolved them already.

Tools are reached THROUGH the gateway, by the same route as the agent — one
`bedrock-agentcore:InvokeGateway` on one ARN is the whole grant, so nothing enumerates tools
and a firm never waits on an operator to automate something new. See `_gateway.py`.

Outcome is LOGGED, never handled here: a subscription filter on this log group feeds
`create_inc_from_log`, which opens the incident and mails the owner. Keeping the task
write and the mail out of this role is deliberate — this is the process running
untrusted code, so it holds the fewest grants of anything in the chain.
"""

import json
import os
import traceback


import _gateway
from aws import client as _client
from aws import json_default as _json_default
from aws import log
from aws import refuse_non_owner
from botocore.exceptions import ClientError
import automation_rules   # the script-facing catalog: retry_order, retry_decision
import general_rules      # the generic arithmetic a script may also ask
import rules
import schedule_names

from _helpers import err, ok

BUCKET = os.environ["CABINET_BUCKET"]
PREFIX = os.environ.get("APPROVED_PREFIX", "automations/approved/modules/")
# The web door. A route record maps a url to a script: `/automate/collections/chargecards` reads
# `automations/routes/collections/chargecards.json`, so adding a url is a `manage_storage op=put`
# and there is no index to keep in step. NOT "published" — that word means the openly-operated
# public feed, and these sit behind the owner JWT. This role holds GetObject on this prefix and on
# the approved one, nothing else; a proxy path containing `..` is not traversal, because an S3 key
# is an opaque string, so it resolves to a key that does not exist.
ROUTES_PREFIX = os.environ.get("ROUTES_PREFIX", "automations/routes/")
# The second door, `POST /hooks/{proxy+}`: no authorizer at the gateway, because the gateway cannot
# check a per-route secret. The route RECORD says who may call it — `caller.bearer` names a secret at
# the firm's own automation env path — and this lambda checks it before it resolves the script.
AUTOMATION_ENV_PATH = os.environ.get("AUTOMATION_ENV_PATH", "")
# No allowlist. A script reaches tools through the gerp's own gateway, by the same route and with
# the same reach as the agent that wrote it — see _gateway.py for why the enumerated map went.

_s3 = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = _client("s3")
    return _s3


def _log(event, **fields):
    """One structured line per outcome — the contract `create_inc_from_log` filters on.

    `incident`/`subject`/`category`/`label` are what that lambda reads: which stream to dedupe
    against and what to call it. `event` stays for anything else reading these logs."""
    script = fields.get("automation")
    incident = {"automation_fail": "fail", "automation_ok": "ok"}.get(event)
    print(json.dumps({
        "event": event,
        **({"incident": incident, "subject": f"automation:{script}",
            "category": "automation", "label": f"Automation `{script}`"} if incident and script else {}),
        **fields,
    }, default=_json_default))


class ToolError(RuntimeError):
    """A tool returned non-2xx. Carries the tool and args so the failure line names the
    field to fix — a traceback alone would not."""

    # `tool_args`, not `args`: Exception.args is a builtin that super().__init__ overwrites,
    # and silently losing the arguments would gut the failure line — they are the thing that
    # tells a repairing agent which field to change.
    def __init__(self, tool, tool_args, status, body):
        self.tool, self.tool_args, self.status, self.body = tool, tool_args, status, body
        super().__init__(f"{tool} returned {status}: {body}")


class Ctx:
    """What a script is HANDED. Small on purpose: a reviewer has to know it cold.

    Not the limit of what it can reach. `exec` gives the script `__builtins__`, so it imports like any
    module — including this lambda's own bundle, which carries `automation_rules`, `general_rules` and
    `schedule_names`.
    A script that wants a rule's stored params asks `rules`; one that wants a shared function calls it.
    The role is the boundary (`modules/automation/TODO.md`), and restricting the namespace would sit in
    front of a boundary that already holds."""

    def call(self, tool: str, args: dict | None = None):
        """Invoke a tool on this firm's gateway and hand back its body.

        A non-2xx raises rather than being carried on silently — from the GATEWAY (400 for arguments
        that do not match the tool's schema, 403 for a policy refusal, 404 for no such tool) or from
        the tool itself. Both arrive as `ToolError`, which is what every script and the failure chain
        already read.
        """
        try:
            out = _gateway.call(tool, args or {})
        except _gateway.GatewayError as e:
            raise ToolError(tool, args, e.status, e.body) from None

        # the tool's own envelope, inside the gateway's: {statusCode, body:"<json>"}
        status = out.get("statusCode", 200) if isinstance(out, dict) else 200
        if not isinstance(out, dict) or "statusCode" not in out:
            return out
        body = out.get("body")
        parsed = json.loads(body) if isinstance(body, str) else (body if body is not None else out)
        if status >= 300:
            raise ToolError(tool, args, status, parsed)
        return parsed

    def subject(self, text: str) -> str:
        """What `text` becomes once a schedule carries it.

        A schedule's subject is stored in its name, so listing hands back the sanitized form and
        nothing hands back the original. A script comparing what is scheduled against what its
        records say has to meet it there, and the sanitizing is not guessable — hyphens go, runs of
        replacements collapse, and it is cut to 34 characters. Reproducing that in a script means
        matching most ids and silently missing the ones that were cut, which are not a random
        sample: they are the longest, and on a fleet keyed by customer number they arrive together.
        """
        return schedule_names.slug(text, schedule_names.MAX_SUBJECT)

    def __init__(self, by_subject: dict | None = None):
        # `AUTOMATION#<subject>` rows, resolved by whoever invoked this script and passed down. Not
        # fetched: this process runs untrusted code and holds the fewest grants in the chain.
        self._rules = by_subject or {}

    def rules(self, subject: str, obj: dict):
        """Ask the rule instances attached at `AUTOMATION#<subject>`.

        The second thing a script may reach, and the reason the first is not enough: `call` performs
        effects, this computes. A script deciding for itself how many attempts to make has put an
        opinion where one reviewer sees it; the same opinion as an instance row is written once and
        retuned by its owner without the script going back through review.

        The rows came in the payload. A rule cannot branch on an earlier rule's result — every
        instance runs against the same object and none sees another's return — so its params are
        settled before the script starts and the invoker had them. A script that needed to LOOK
        something up would, by that fact, not be asking a rule.

        Returns what the matched instances produced, `[]` when none matched or none produced
        anything, which is what a rule returning nothing means everywhere in the catalog.
        """
        rows = self._rules.get(subject) or []
        return rules.run_instances(obj, rows, modules=[automation_rules, general_rules])


def _fetch(script: str) -> str:
    key = PREFIX + script
    return s3().get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()


def _route(path: str) -> dict:
    key = f"{ROUTES_PREFIX}{path}.json"
    return json.loads(s3().get_object(Bucket=BUCKET, Key=key)["Body"].read().decode())


def _env_secret(name: str) -> str:
    """A value at the firm's automation env path — the one place a script's credentials live and
    the one path this role can read. `name` comes from a route record, never from the caller."""
    return _client("ssm").get_parameter(Name=f"{AUTOMATION_ENV_PATH}/{name}",
                                         WithDecryption=True)["Parameter"]["Value"]


def _admit_hook_caller(event, record) -> dict | None:
    """The hooks door's check. Returns an error envelope to send, or None to proceed.

    A record with no `caller` was published for the owner door only and is not reachable here —
    the same 404 an unpublished path gets, so a probe learns nothing about what exists. A wrong or
    missing bearer is a 401 that names nothing. The compare is constant-time."""
    import hmac
    name = ((record.get("caller") or {}).get("bearer") or "").strip()
    if not name:
        return err("no such hook", 404)
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    presented = headers.get("authorization", "")
    presented = presented[7:] if presented.lower().startswith("bearer ") else ""
    try:
        expected = _env_secret(name)
    except Exception as e:  # noqa: BLE001 — a missing secret is the firm's to fix, not the caller's to learn
        log.error("hook secret unreadable", event="hook_secret_unavailable", name=name, error=str(e))
        return err("unauthorized", 401)
    if not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        return err("unauthorized", 401)
    return None


class AutomationFailed(Exception):
    """A failure raised rather than returned, for `raise_on_error` callers.

    The class name is what Step Functions matches on, so a definition catches it as
    `"ErrorEquals": ["AutomationFailed"]`.
    """


def handler(event, context):
    """    script          the key under the approved prefix; may nest (`collections/charge_next_card.py`)
    params          what run(ctx, **params) is called with
    rules           optional {"<subject>": [<instance row>, …]} — what `ctx.rules(subject, obj)` runs.
                    Resolved by the caller, because this process is not trusted with a table read and
                    does not need one: a rule's params cannot depend on a result the script has not
                    produced yet, so the caller always had them.
    raise_on_error  optional; raise `AutomationFailed` instead of returning the error envelope.

    Every tool here answers with `{statusCode, body}` because the gateway and `ctx.call` unwrap it
    that way, and a caller reading the envelope sees a failure either way. A step function does not
    read it: a returned dict is a successful Task, so it follows `Next` and a sequence keeps walking
    after a step failed. `raise_on_error` is how such a caller says the invocation itself should
    fail, which is what `Retry` and `Catch` watch.
    """
    refused = refuse_non_owner(event)   # the /automate/ door: the owner, not any signed-up account
    if refused:
        return refused
    proxy = (event.get("pathParameters") or {}).get("proxy")
    body = json.loads(event["body"] or "{}") if isinstance(event.get("body"), str) else dict(event)
    via_hooks = (event.get("rawPath") or "").startswith("/hooks/")

    if proxy:
        # The web caller names a URL, never a script. `POST /automate` taking {script, params} would
        # hand anyone holding the owner JWT any approved script with any arguments; a record narrows
        # the surface to the one script that url routes to.
        try:
            record = _route(proxy)
        except Exception as e:
            # NOT automation_fail. This door faces the web, so an unknown path is a caller's
            # mistake, and `create_inc_from_log` would mail the owner for every probe of a URL
            # that never existed. Logged without the incident fields so it is still diagnosable.
            _log("route_missing", path=proxy, error=str(e))
            return err(f"/{'hooks' if via_hooks else 'automate'}/{proxy} has no route", 404)
        if via_hooks:
            refused = _admit_hook_caller(event, record)
            if refused:
                return refused
        # Baked args LAST so the route wins. A param with a default is the route record's to fix
        # (`spec()` reads that off the signature); the caller supplies the ones without defaults.
        # Reversing this would let a caller raise a route’s `attempts` to any number it liked.
        body = {"script": record.get("key") or "",
                "params": {**(body.get("params") or body), **(record.get("args") or {})}}

    out = _run(body)
    if body.get("raise_on_error") and out.get("statusCode", 200) >= 400:
        try:
            message = json.loads(out["body"]).get("error") or out["body"]
        except (ValueError, TypeError):
            message = out.get("body", "")
        raise AutomationFailed(message)
    return out


def _run(body):
    script = (body.get("script") or "").strip()
    params = body.get("params") or {}

    if not script:
        return err("script is required — the key under the approved prefix, e.g. "
                   "'close_stale_incidents.py' or 'collections/charge_next_card.py'")
    # No path check. A name that walks out of the approved prefix resolves to a key this role cannot
    # read, and `_fetch` already returns the same 404 for AccessDenied as for a name that does not
    # exist — so a string test catches nothing IAM does not, and banning `/` only stopped a firm
    # filing thirty automations in groups.
    if not isinstance(params, dict):
        return err("params must be an object")

    try:
        source = _fetch(script)
    except Exception as e:
        # AccessDenied and NoSuchKey mean the same thing to a caller: this script is not
        # approved to run. Anything else is the read failing.
        if isinstance(e, ClientError) and e.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied"):
            _log("automation_fail", automation=script, error=f"unavailable: {e}")
            return err(f"{script} is not available to run (not approved, or no such script)", 404)
        log.error("script fetch failed", automation=script, error=str(e))
        return err(f"could not read {script}: {e}", 502)

    ns: dict = {}
    try:
        exec(compile(source, script, "exec"), ns)  # noqa: S102 — the role is the boundary
    except Exception:
        _log("automation_fail", automation=script, error=traceback.format_exc(limit=8))
        return err(f"{script} failed to load", 422)

    run = ns.get("run")
    if not callable(run):
        _log("automation_fail", automation=script, error="no run(ctx, **params) entrypoint")
        return err(f"{script} defines no run(ctx, **params) entrypoint", 422)

    try:
        result = run(Ctx(body.get("rules")), **params)
    except ToolError as e:
        # the arguments are what got rejected, so they go in the line — a traceback
        # would not tell the repairing agent which field to change
        _log("automation_fail", automation=script, tool=e.tool, args=e.tool_args, error=str(e))
        return err(str(e), 422, automation=script, tool=e.tool)
    except TypeError as e:
        # the payload and the script's signature disagree — name it as that rather than
        # letting it read like a bug inside the script
        _log("automation_fail", automation=script, args=params, error=str(e))
        return err(f"{script}: {e}", 422, automation=script)
    except Exception:
        _log("automation_fail", automation=script, args=params, error=traceback.format_exc(limit=8))
        return err(f"{script} raised", 422, automation=script)

    _log("automation_ok", automation=script)
    return ok({"automation": script, "result": result})
