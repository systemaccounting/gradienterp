"""manage_mcp — install a vendor's MCP server for this gerp, and uninstall it.

op install:   the client at the vendor (DCR against its registration endpoint, naming this gerp's
              callback), the credential provider in the vault, the target on the vendor gateway,
              the `MCP#<provider>` row — and the target's own consent link, read off the target.
              With `secret_name`: the key path — an API-key credential provider from the owner's
              key in SSM, the same target, no consent.
              A `client_by: owner` vendor (no registration endpoint: Xero, GitHub, HubSpot) installs
              in two halves: `install {provider}` makes the credential provider and answers its
              callback url for the owner to paste into an app of their own at the vendor; `install
              {provider, client_id, client_secret_name}` reads the app's secret from SSM, puts the
              client on the provider, and goes on to the target and the row as above.
op uninstall: the reverse; the client at the vendor too where its management uri answers.
op status:    the target's state, the pending consent, the link (reissued when the target's
              consent lapsed).
op list:      the rows, as the agent may see them.

The gateway's per-caller consent (the firm's) is not this door's: the container gets it as a url
elicitation on the first call and writes the pending session on the row itself; the landing
(complete_mcp_auth) finishes both kinds.
"""
import json
import os
import urllib.error
import urllib.request

from aws import bind, client as _aws, log
from _helpers import catalog, delete_row, err, get_row, list_rows, now_iso, ok, public, put_row

GATEWAY_ID = os.environ.get("VENDOR_GATEWAY_ID", "")
LANDING_URL = os.environ.get("LANDING_URL", "")
SECRETS_PREFIX = os.environ.get("SECRETS_PREFIX", "")
GERP_ID = os.environ.get("GERP_ID") or os.environ.get("CUSTOMER_ID", "")

PLACEHOLDER_SECRET = "none"  # Identity wants a secret; a public client (Stripe) has none


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.get("op") or "").strip()
    provider = (body.get("provider") or "").strip().lower()
    bind(gerp_id=GERP_ID, provider=provider, op=op)
    if op == "list":
        return ok({"installed": [public(r) for r in list_rows()], "catalog": _catalog_summary()})
    if not provider:
        return err("provider is required")
    cat = catalog().get(provider)
    if cat is None:
        return err(f"{provider} is not in the catalog", 404, catalog=sorted(catalog()))
    if op == "install":
        return _install(provider, cat, body)
    if op == "uninstall":
        return _uninstall(provider)
    if op == "status":
        return _status(provider)
    return err("op must be install, uninstall, status or list")


# ─── install ───

def _install(provider: str, cat: dict, body: dict) -> dict:
    row = get_row(provider)
    owner = cat.get("client_by") == "owner"
    client_id = (body.get("client_id") or "").strip()
    secret_name = (body.get("secret_name") or "").strip()
    half_done = bool(row) and (row.get("pending") or {}).get("kind") == "client"
    if row and not half_done:
        return err(f"{provider} is already installed; uninstall first", 409)
    if half_done and secret_name:
        return err(f"the owner's app install for {provider} is half done; finish it with client_id, or uninstall first", 409)
    if client_id and not owner:
        return err(f"{provider} registers its own client; install without client_id", 400)
    ctl = _aws("bedrock-agentcore-control")
    if half_done:
        return _owner_second_half(provider, cat, row, body, ctl) if client_id else ok(_owner_answer(provider, cat, row))
    if client_id:
        return err(f"install {{provider: {provider}}} first; it answers the callback url the owner's app needs", 404)
    installed_by = str(body.get("_authed_by") or "")
    write = bool(body.get("write", False))
    name = f"mcp-{provider}"
    row = {
        "gerp_id": GERP_ID, "sk": f"MCP#{provider}", "provider": provider, "prefix": cat["prefix"],
        "credential_provider": name, "write": write, "installed_at": now_iso(),
        "installed_by": installed_by,
    }
    if cat.get("write_tools"):
        row["write_tools"] = list(cat["write_tools"])  # the container's write bound reads the row, not the catalog
    if secret_name:
        if not cat.get("key_header"):
            return err(f"{provider} takes no key; install without secret_name for the consent path", 400)
        key = _secret(secret_name)
        if key is None:
            return err(f"no secret named {secret_name}; collect it first", 404)
        cp = ctl.create_api_key_credential_provider(name=name, apiKey=key)
        row["credential_kind"] = "key"
        cred = {"credentialProviderType": "API_KEY", "credentialProvider": {"apiKeyCredentialProvider": {
            "providerArn": cp["credentialProviderArn"],
            "credentialParameterName": cat["key_header"]["name"],
            "credentialPrefix": cat["key_header"].get("prefix", ""),
            "credentialLocation": "HEADER",
        }}}
    else:
        meta = _metadata(cat["authorization_server"])
        # the callback url is Identity's and unique per credential provider, so the provider is
        # made first with a placeholder client, the client registered against its callback, then
        # the provider updated with the client it got
        auth_method = cat.get("token_endpoint_auth_method", "client_secret_post")
        cfg = _oauth_config(meta, "placeholder", PLACEHOLDER_SECRET, auth_method)
        cp = ctl.create_oauth2_credential_provider(name=name, credentialProviderVendor="CustomOauth2", oauth2ProviderConfigInput=cfg)
        row.update(credential_kind="oauth", callback_url=cp["callbackUrl"], credential_provider_arn=cp["credentialProviderArn"])
        if owner:
            # the first half: the owner makes the app at the vendor with this callback; the
            # second half brings the client id and the secret's name
            row["pending"] = {"kind": "client", "since": now_iso()}
            put_row(row)
            log.info("mcp.owner_app_pending")
            return ok(_owner_answer(provider, cat, row), 201)
        try:
            reg = _register(cat, meta, cp["callbackUrl"])
        except Exception as e:
            ctl.delete_oauth2_credential_provider(name=name)
            log.exception("mcp.dcr_failed")
            return err(f"{provider} refused the client registration: {e}", 502)
        cfg = _oauth_config(meta, reg["client_id"], reg.get("client_secret") or PLACEHOLDER_SECRET, auth_method)
        ctl.update_oauth2_credential_provider(name=name, credentialProviderVendor="CustomOauth2", oauth2ProviderConfigInput=cfg)
        row["client_id"] = reg["client_id"]
        if reg.get("registration_client_uri"):
            uri = reg["registration_client_uri"]
            if uri.startswith("/"):
                from urllib.parse import urlparse
                u = urlparse(cat["authorization_server"])
                uri = f"{u.scheme}://{u.netloc}{uri}"
            row["registration_client_uri"] = uri
            if reg.get("registration_access_token"):
                row["registration_access_token"] = reg["registration_access_token"]
        _allow_landing(ctl)
        cred = _oauth_cred(cat, cp["credentialProviderArn"])
    try:
        return _finish(provider, cat, row, cred, ctl)
    except TargetRefused as refused:
        _delete_provider(ctl, name, row["credential_kind"])
        return refused.response


class TargetRefused(Exception):
    def __init__(self, response: dict):
        self.response = response


def _finish(provider: str, cat: dict, row: dict, cred: dict, ctl) -> dict:
    """The target on the vendor gateway, then the row with the target's own consent."""
    try:
        tgt = ctl.create_gateway_target(
            gatewayIdentifier=GATEWAY_ID, name=cat["prefix"], description=f"{provider} for {GERP_ID}",
            targetConfiguration={"mcp": {"mcpServer": {"endpoint": cat["endpoint"]}}},
            credentialProviderConfigurations=[cred],
        )
    except Exception as e:
        if "already exists" in str(e):
            raise TargetRefused(err(f"the previous {provider} target is still being removed; install again in a moment", 409)) from None
        log.exception("mcp.target_failed")
        raise TargetRefused(err(f"the target for {provider} was refused: {e}", 502)) from None
    row["target_id"] = tgt["targetId"]
    row["target_status"] = tgt["status"]
    auth = _authorization(tgt)
    if auth:
        row["pending"] = {"kind": "target", "session": _session_of(auth["authorizationUrl"]), "user_id": auth.get("userId", ""), "since": now_iso()}
        row["consent_url"] = auth["authorizationUrl"]
    else:
        row.pop("pending", None)
    put_row(row)
    log.info("mcp.installed", target=tgt["targetId"], status=tgt["status"])
    out = public(row)
    out["next"] = ("send the owner consent_url; the target syncs when they approve" if auth
                   else "the target is syncing; op status reads it back")
    return ok(out, 201)


def _oauth_cred(cat: dict, provider_arn: str) -> dict:
    return {"credentialProviderType": "OAUTH", "credentialProvider": {"oauthCredentialProvider": {
        "providerArn": provider_arn,
        "scopes": list(cat.get("scopes") or []),
        "grantType": "AUTHORIZATION_CODE",
        "defaultReturnUrl": LANDING_URL,
    }}}


def _secret(name: str) -> str | None:
    ssm = _aws("ssm")
    try:
        return ssm.get_parameter(Name=SECRETS_PREFIX + name, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None


# ─── the owner's own app (client_by: owner) ───

def _owner_answer(provider: str, cat: dict, row: dict) -> dict:
    out = public(row)
    out.update(
        callback_url=row["callback_url"], new_app_url=cat["new_app_url"], callback_field=cat["callback_field"],
        client_secret_name=f"{provider}_client_secret", scopes=list(cat.get("scopes") or []),
        next=(f"the owner makes an app at new_app_url on the firm's own {provider} account, pastes callback_url "
              f"into its {cat['callback_field']} field, submits the app's client secret through collect_secret "
              f"as client_secret_name, and tells you the client id; then install again with client_id and client_secret_name"),
    )
    return out


def _owner_second_half(provider: str, cat: dict, row: dict, body: dict, ctl) -> dict:
    client_id = body["client_id"].strip()
    secret_name = (body.get("client_secret_name") or "").strip()
    if not secret_name:
        return err("client_secret_name is required with client_id: the name the owner gave the app's client secret in collect_secret", 400)
    secret = _secret(secret_name)
    if secret is None:
        return err(f"no secret named {secret_name}; collect it first", 404)
    meta = _metadata(cat["authorization_server"])
    cfg = _oauth_config(meta, client_id, secret, cat.get("token_endpoint_auth_method", "client_secret_post"))
    ctl.update_oauth2_credential_provider(name=row["credential_provider"], credentialProviderVendor="CustomOauth2", oauth2ProviderConfigInput=cfg)
    _allow_landing(ctl)
    row = dict(row)
    row["client_id"] = client_id
    if "write" in body:
        row["write"] = bool(body["write"])
    try:
        return _finish(provider, cat, row, _oauth_cred(cat, row["credential_provider_arn"]), ctl)
    except TargetRefused as refused:
        # the provider keeps the client; the row stays half done, and this half runs again
        return refused.response


def _oauth_config(meta: dict, client_id: str, client_secret: str, auth_method: str) -> dict:
    return {"customOauth2ProviderConfig": {
        "oauthDiscovery": {"authorizationServerMetadata": {
            "issuer": meta["issuer"],
            "authorizationEndpoint": meta["authorization_endpoint"],
            "tokenEndpoint": meta["token_endpoint"],
            "responseTypes": ["code"],
        }},
        "clientId": client_id,
        "clientSecret": client_secret,
        # a public client (`none`) still has to send client_id in the body: POST, never basic
        "clientAuthenticationMethod": "CLIENT_SECRET_BASIC" if auth_method == "client_secret_basic" else "CLIENT_SECRET_POST",
    }}


def _register(cat: dict, meta: dict, callback: str) -> dict:
    reg_url = cat.get("registration_endpoint") or meta.get("registration_endpoint")
    if not reg_url:
        raise RuntimeError("no registration endpoint")
    body = {
        "client_name": f"gradientERP {GERP_ID}",
        "redirect_uris": [callback],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": cat.get("token_endpoint_auth_method", "client_secret_post"),
    }
    if cat.get("scopes"):
        body["scope"] = " ".join(cat["scopes"])
    req = urllib.request.Request(reg_url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "gradientERP"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.read()[:200].decode(errors='replace')}")


def _metadata(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "gradientERP"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _allow_landing(ctl) -> None:
    """The vendor gateway's own workload identity has to list the landing among its return urls
    for a per-caller consent to come back there."""
    try:
        wi = ctl.get_workload_identity(name=GATEWAY_ID)
    except Exception:
        log.warning("mcp.workload_identity_unread")
        return
    urls = list(wi.get("allowedResourceOauth2ReturnUrls") or [])
    if LANDING_URL not in urls:
        ctl.update_workload_identity(name=GATEWAY_ID, allowedResourceOauth2ReturnUrls=urls + [LANDING_URL])


def _authorization(tgt: dict) -> dict | None:
    return ((tgt.get("authorizationData") or {}).get("oauth2")) or None


def _session_of(url: str) -> str:
    from urllib.parse import parse_qs, urlparse, unquote
    q = parse_qs(urlparse(url).query)
    return unquote(q.get("request_uri", [""])[0])


# ─── uninstall ───

def _uninstall(provider: str) -> dict:
    row = get_row(provider)
    if not row:
        return err(f"{provider} is not installed", 404)
    ctl = _aws("bedrock-agentcore-control")
    if row.get("target_id"):
        try:
            ctl.delete_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=row["target_id"])
        except ctl.exceptions.ResourceNotFoundException:
            pass
        # the deletion is asynchronous and the target's name stays taken until it is done; an
        # install right after answered "already exists" live. Wait for it here, so uninstall
        # answers once the name is free
        _wait_target_gone(ctl, row["target_id"])
    _delete_provider(ctl, row.get("credential_provider") or f"mcp-{provider}", row.get("credential_kind", "oauth"))
    vendor = "kept"
    if catalog().get(provider, {}).get("client_by") == "owner":
        vendor = f"the owner's own app; it stays at {provider} until the owner deletes it there"
    if row.get("registration_client_uri"):
        req = urllib.request.Request(row["registration_client_uri"], method="DELETE", headers={"Accept": "application/json", "User-Agent": "gradientERP"})
        if row.get("registration_access_token"):
            req.add_header("Authorization", "Bearer " + row["registration_access_token"])
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                vendor = f"deleted ({r.status})"
        except urllib.error.HTTPError as e:
            vendor = f"kept ({e.code})"
        except Exception:
            vendor = "kept (unreachable)"
    delete_row(provider)
    log.info("mcp.uninstalled")
    return ok({"provider": provider, "uninstalled": True, "vendor_client": vendor})


def _wait_target_gone(ctl, target_id: str, seconds: int = 40) -> bool:
    import time
    deadline = time.time() + seconds
    while True:
        try:
            ctl.get_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=target_id)
        except ctl.exceptions.ResourceNotFoundException:
            return True
        if time.time() >= deadline:
            log.warning("mcp.target_still_deleting")
            return False
        time.sleep(2)


def _delete_provider(ctl, name: str, kind: str) -> None:
    try:
        if kind == "key":
            ctl.delete_api_key_credential_provider(name=name)
        else:
            ctl.delete_oauth2_credential_provider(name=name)
    except ctl.exceptions.ResourceNotFoundException:
        pass


# ─── status ───

def _status(provider: str) -> dict:
    row = get_row(provider)
    if not row:
        return err(f"{provider} is not installed", 404)
    if not row.get("target_id"):
        # the owner's app is half done: no target yet, the callback still to paste
        return ok(_owner_answer(provider, catalog()[provider], row))
    ctl = _aws("bedrock-agentcore-control")
    tgt = ctl.get_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=row["target_id"])
    status = tgt["status"]
    reasons = tgt.get("statusReasons") or []
    pending = row.get("pending") or {}
    changed = False
    lapsed = status == "FAILED" and any("timed out" in r for r in reasons) and pending.get("kind") == "target"
    if lapsed or (status == "FAILED" and row.get("credential_kind") == "key"):
        # the target's consent lapsed (about fifteen minutes): a re-sync reissues it with a new
        # gateway user id. A key target that failed to sync (a grant fixed since, a vendor that
        # was down) re-syncs the same way, with no consent to reissue
        ctl.synchronize_gateway_targets(gatewayIdentifier=GATEWAY_ID, targetIdList=[row["target_id"]])
        tgt = ctl.get_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=row["target_id"])
        status = tgt["status"]
        reasons = tgt.get("statusReasons") or []
        auth = _authorization(tgt)
        if auth:
            row["pending"] = {"kind": "target", "session": _session_of(auth["authorizationUrl"]), "user_id": auth.get("userId", ""), "since": now_iso()}
            row["consent_url"] = auth["authorizationUrl"]
            changed = True
    if status == "READY" and pending.get("kind") == "target":
        row.pop("pending", None)
        row.pop("consent_url", None)
        changed = True
    if row.get("target_status") != status:
        row["target_status"] = status
        changed = True
    if changed:
        put_row(row)
    out = public(row)
    out["target_status"] = status
    if reasons:
        out["status_reasons"] = reasons
    return ok(out)


def _catalog_summary() -> list[dict]:
    return [{"provider": k, "use": v.get("use", ""), "key": bool(v.get("key_header")),
             "client_by": v.get("client_by", "registration")} for k, v in sorted(catalog().items())]
