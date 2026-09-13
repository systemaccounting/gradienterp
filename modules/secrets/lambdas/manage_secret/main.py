"""manage_secret — the gerp's vault: put, list, delete.

One lambda for every secret a customer hands gerp. A secret is a SecureString under one of two
prefixes, chosen by `scope`:

  vault    (default) `/gradienterp/customers/<CUSTOMER_ID>/secrets/<name>`
           read BY NAME by the one tool that consumes it (configure_webhook, manage_mcp ...)
  automation_env     `/gradienterp/customers/<CUSTOMER_ID>/automation/env/<name>`
           exported as an env var into EVERY cmd script, so it's reachable by any script the
           agent writes — the home for a credential a script needs, the wrong one for a
           credential a single tool needs.

The caller supplies only the leaf name — never a path. The prefix is fixed and `CUSTOMER_ID` is
the lambda's own env (deployed per customer, so one tenant). The value is never logged or
returned.

op put:    {name, value, type?, overwrite?, scope?} → {status: stored, name, scope}. `overwrite`
           defaults to false (a live secret is not clobbered by a name reuse); true rotates in
           place, SSM keeps prior versions. Refused with 403 when the invoke carries the
           agent gateway's client context: a value must never come from a model's context. The
           chat lambda (the `collect_secret` form's sink, by the `agent_frame_sink` tag) and any
           other direct invoker put.
op list:   {scope?} → {secrets: [{name, scope, updated_at}]}, by DescribeParameters, which
           answers names and dates and no values.
op delete: {name, scope?} → {status: deleted, name, scope}; 404 when absent.
"""

import json
import os
import re

from aws import client as _aws, log

CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")
SECRET_PARAM_PREFIX = os.environ.get("SECRET_PARAM_PREFIX", f"/gradienterp/customers/{CUSTOMER_ID}/secrets")
# the cmd tool's env path — every param under it becomes an env var in an agent-written script
# (modules/cmd), which is why it's a separate scope rather than the default
AUTOMATION_ENV_PARAM_PREFIX = os.environ.get("AUTOMATION_ENV_PARAM_PREFIX", f"/gradienterp/customers/{CUSTOMER_ID}/automation/env")
PREFIX_BY_SCOPE = {"vault": SECRET_PARAM_PREFIX, "automation_env": AUTOMATION_ENV_PARAM_PREFIX}

# leaf names only — no path separators or traversal. the agent dictates these.
_LEAF = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "body": json.dumps(body)}


def _from_gateway(context) -> bool:
    """The agent's gateway sets bedrockAgentCoreToolName on every invoke's client context."""
    try:
        return bool(context.client_context.custom.get("bedrockAgentCoreToolName"))
    except Exception:  # noqa: BLE001 — no client context on a direct invoke
        return False


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else (event or {})
    op = (body.get("op") or "put").strip()
    scope = body.get("scope") or "vault"
    if scope not in PREFIX_BY_SCOPE:
        return _resp(400, {"error": "scope must be vault or automation_env"})
    if op == "list":
        return _list(scope)
    name = body.get("name", "")
    if not _LEAF.match(name or ""):
        return _resp(400, {"error": "invalid name; use [A-Za-z0-9_-], 1-128 chars"})
    if op == "put":
        if _from_gateway(context):
            return _resp(403, {"error": "a secret's value is collected through the owner's form (collect_secret), never as a tool argument"})
        return _put(name, body, scope)
    if op == "delete":
        return _delete(name, scope)
    return _resp(400, {"error": "op must be put, list or delete"})


def _put(name: str, body: dict, scope: str) -> dict:
    value = body.get("value")
    param_type = body.get("type", "SecureString")
    overwrite = bool(body.get("overwrite", False))
    if not value:
        return _resp(400, {"error": "value required"})
    if param_type not in ("SecureString", "String"):
        return _resp(400, {"error": "type must be SecureString or String"})
    path = f"{PREFIX_BY_SCOPE[scope]}/{name}"
    try:
        _aws("ssm").put_parameter(Name=path, Value=value, Type=param_type, Overwrite=overwrite)
    except Exception as e:  # noqa: BLE001 — surface failure without the value
        if type(e).__name__ == "ParameterAlreadyExists":
            return _resp(409, {"error": f"secret '{name}' already exists; resubmit with overwrite to replace, or use a new name"})
        log.exception("manage_secret.put_failed")
        return _resp(502, {"error": f"store failed: {type(e).__name__}"})
    return _resp(200, {"status": "stored", "name": name, "scope": scope})


def _list(scope: str) -> dict:
    prefix = PREFIX_BY_SCOPE[scope] + "/"
    ssm = _aws("ssm")
    out, kwargs = [], {"ParameterFilters": [{"Key": "Path", "Option": "Recursive", "Values": [PREFIX_BY_SCOPE[scope]]}]}
    while True:
        resp = ssm.describe_parameters(**kwargs)
        for p in resp.get("Parameters", []):
            when = p.get("LastModifiedDate")
            out.append({"name": p["Name"][len(prefix):], "scope": scope,
                        "updated_at": when.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(when, "strftime") else str(when or "")})
        if not resp.get("NextToken"):
            break
        kwargs["NextToken"] = resp["NextToken"]
    return _resp(200, {"secrets": sorted(out, key=lambda s: s["name"])})


def _delete(name: str, scope: str) -> dict:
    ssm = _aws("ssm")
    try:
        ssm.delete_parameter(Name=f"{PREFIX_BY_SCOPE[scope]}/{name}")
    except ssm.exceptions.ParameterNotFound:
        return _resp(404, {"error": f"no secret named {name} in {scope}"})
    log.info("manage_secret.deleted", name=name)
    return _resp(200, {"status": "deleted", "name": name, "scope": scope})
