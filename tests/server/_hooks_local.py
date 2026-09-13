"""The seller gerp's customer-contact hook, published on the local stack.

In prod the operator ran `manage_hooks {op: publish}` for customers/upsert on gradienterp and stored the answer at
`/gradienterp/cloud/hooks/customers_upsert`. Locally the same two acts run at seed time: the
script is put where approval puts it, manage_hooks runs in this process, and the url + token land
in moto SSM where the BFF reads them. `HOOKS_BASE_URL` on the image is the prod api; it is pointed
at the local gateway so the url it returns is one this stack serves.
"""
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APPROVED = "automations/approved/modules/"
# the two hooks the BFF calls, one caller: (path, script, the parameter the BFF reads)
HOOKS = [("customers/upsert", REPO / "prod/gradienterp/automations/upsert_customer_contact.py",
          "/gradienterp/cloud/hooks/customers_upsert"),
         ("customers/erase", REPO / "prod/gradienterp/automations/erase_customer_contact.py",
          "/gradienterp/cloud/hooks/customers_erase")]


def point_image_at_local(image: dict) -> None:
    base = os.environ.get("LOCAL_GATEWAY_URL")
    if not base:
        return
    for cfg in image.get("functions", {}).values():
        env = cfg.get("env") or {}
        if "HOOKS_BASE_URL" in env:
            env["HOOKS_BASE_URL"] = base
    os.environ["HOOKS_BASE_URL"] = base


def seed_customer_hook(image: dict) -> None:
    """Approve the scripts, publish the hooks, store what they returned where the BFF reads it."""
    name, fn = next(((n, c) for n, c in image.get("functions", {}).items() if n.endswith("-manage_hooks")),
                    (None, None))
    if not fn or not os.environ.get("LOCAL_GATEWAY_URL"):
        return
    try:
        _publish(name, fn)
    except Exception as e:  # noqa: BLE001 — the stack comes up without the hook; the BFF then posts nothing
        print(f"[hooks] not published: {e}")


def _publish(name: str, fn: dict) -> None:
    from aws import client
    env = fn.get("env") or {}
    for k in ("CABINET_BUCKET", "AUTOMATION_ENV_PATH", "ROUTES_PREFIX"):
        if k in env:
            os.environ[k] = env[k]
    s3 = client("s3")
    try:
        s3.create_bucket(Bucket=env["CABINET_BUCKET"])
    except Exception:  # noqa: BLE001 — already there
        pass
    # one caller, so the second publish rotates the token the first returned: every parameter is
    # written after the last publish, with the token that stands
    urls = {}
    for path, script, param in HOOKS:
        s3.put_object(Bucket=env["CABINET_BUCKET"], Key=APPROVED + script.name, Body=script.read_bytes())
        out = client("lambda").invoke(FunctionName=name, Payload=json.dumps(
            {"op": "publish", "path": path, "script": script.name, "caller": "bff"}).encode())
        body = json.loads(out["Payload"].read())
        if isinstance(body.get("body"), str):
            body = json.loads(body["body"])
        if "token" not in body:
            raise RuntimeError(f"manage_hooks did not publish {path}: {body}")
        urls[param], token = body["url"], body["token"]
    for param, url in urls.items():
        client("ssm").put_parameter(Name=param, Type="SecureString", Overwrite=True,
                                    Value=json.dumps({"url": url, "token": token}))
        print(f"[hooks] {url} published for the bff · {param}")
