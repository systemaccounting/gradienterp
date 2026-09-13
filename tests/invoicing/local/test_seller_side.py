"""Local-mode tests for invoicing's sell-side commit — the O2C mirror of purchasing.

A buyer's inbound po.proposed stamps the buyer slot (apply_inbound); the seller's accept_po
stamps the seller slot and emits po.accepted; convergence (both stamps) settles into a DRAFT
invoice. Same shared agreements machinery as purchasing, opposite role, settle → invoice.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import sys as _s
REPO_ROOT = Path(__file__).resolve().parents[3]
import sys as _sys; _sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import make_table   # noqa: E402
LAMBDAS = REPO_ROOT / "modules" / "invoicing" / "lambdas"
OUT = REPO_ROOT / "out" / "invoicing_seller_test"
OUT.mkdir(parents=True, exist_ok=True)
AGREEMENTS = OUT / "agreements.jsonl"
EVENTS = OUT / "invoicing-events.jsonl"
INVOICES = OUT / "invoices.jsonl"

os.environ["AGREEMENTS_TABLE"] = make_table("agreements")
os.environ["GERP_ID"] = "seller-gerp"
sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import make_table   # noqa: E402
from helpers.localaws import books, invoicing, make_table   # noqa: E402

os.environ.update(books("seller_side"))          # the journal post lands on a real ledger
os.environ.update(invoicing("seller_side"))      # invoice + lines + transitions + the catalog
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(LAMBDAS))
sys.path.insert(0, str(REPO_ROOT / "modules" / "agreements"))


def _load(name):
    spec = importlib.util.spec_from_file_location(f"inv_{name}", LAMBDAS / name / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _op(mod, op):
    """The merged tool: every call carries its op. A string body gets the op inside it; a dict
    event (the tool-call shape) gets it beside the fields — the same event the sibling always saw."""
    import json as _json
    import types as _types

    def _handler(event, ctx=None):
        if isinstance(event.get("body"), str):
            return mod.handler({**event, "body": _json.dumps({**_json.loads(event["body"]), "op": op})}, ctx)
        return mod.handler({**event, "op": op}, ctx)
    return _types.SimpleNamespace(handler=_handler)


SETTLE = _load("settle_agreement")   # the settle EFFECT — direct shape, dispatched by agreements/settle
GET = _op(_load("manage_invoice"), "get")


def _fresh():
    for p in (AGREEMENTS, EVENTS, INVOICES):
        if p.exists():
            p.unlink()


def _inv(h, payload):
    out = h.handler(payload, None)
    return out["statusCode"], json.loads(out["body"])


def _agreements():
    return [json.loads(l) for l in AGREEMENTS.read_text().splitlines() if l.strip()] if AGREEMENTS.exists() else []


def _events():
    return [json.loads(l) for l in EVENTS.read_text().splitlines() if l.strip()] if EVENTS.exists() else []


def _stream(rows):
    return {"Records": [{"eventName": "MODIFY", "dynamodb": {"NewImage": r}} for r in rows]}


def _inbound(detail_type, from_gerp, detail):
    return {"detail_type": detail_type, "from_gerp": from_gerp, "detail": json.dumps(detail)}


def test_direct_settle_drafts_the_invoice_and_writes_nothing_else():
    """The shared agreements settle dispatches {"agreement": row} for the domain effect only —
    the draft appears, and the agreement store here is NOT touched (the dispatcher owns the row)."""
    _fresh()
    row = {"thread": "d-1", "terms_hash": "cafe0000cafe0000",
           "buyer": "cafe", "seller": "seller-gerp",
           "buyer_stamp": 1, "seller_stamp": 2, "kind": "po",
           "terms": {"items": [{"description": "beans", "qty": 25}], "total": 100}}
    assert SETTLE.handler({"agreement": row}, None)["settled"] == ["d-1"]
    inv = _inv(GET, {"invoice_id": "d-1"})[1]["invoices"][0]
    assert inv["status"] == "draft" and inv["customer"] == "cafe" and inv["subtotal"] == 100
    assert _agreements() == [], "no settled stamp, no stray row — the dispatcher marks its own table"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all invoicing seller-side tests passed")
