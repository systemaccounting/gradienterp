"""Tags — what a firm calls things, as opposed to what the accounting calls them.

A tag is not a status. Statuses are `draft -> issued -> paid`, they are money positions, and modules
depend on them. A tag means whatever the firm decides, carries no accounting meaning, and is where
their own automation hangs.

A SET: many per invoice, unordered, independent. Applying one never removes another.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LAMBDAS = REPO_ROOT / "modules" / "invoicing" / "lambdas"

sys.path.insert(0, str(REPO_ROOT / "tests"))
from helpers.localaws import books, invoicing, make_table   # noqa: E402
os.environ["RULE_INSTANCES_TABLE"] = make_table("rules-instances")
os.environ["RULES_PARAMS_TABLE"] = make_table("rules-params")
os.environ.update(books("invoice_tags"))
os.environ.update(invoicing("invoice_tags"))
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
sys.path.insert(0, str(LAMBDAS))
sys.path.insert(0, str(REPO_ROOT / "modules" / "rules"))


def _load(name):
    path = LAMBDAS / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_{name}", path)
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


CREATE = _op(_load("manage_invoice"), "create")
TAGS = _load("manage_invoice")
import _helpers as H   # noqa: E402


def _call(mod, payload):
    r = mod.handler(payload, None)
    return r["statusCode"], json.loads(r["body"])


def _declare(tag):
    """What write_schema (op: extend) does — the firm declaring a word it intends to use."""
    H._registry_table().put_item(Item={
        "registry": "invoice_tags", "bucket_name": f"common#{tag}",
        "bucket": "common", "name": tag, "origin": "extension",
        "schema": {"type": "string", "class": "operational"},
    })


def _invoice():
    _, inv = _call(CREATE, {"customer": "cafe", "lines": [
        {"account": "SALES_REVENUE", "accountType": "REVENUE", "amount": 40}]})
    return inv["invoice_id"]


def test_a_tag_must_be_declared_first():
    """The registry is what keeps a vocabulary comparable across firms rather than invented per
    invoice — and what stops two spellings of one idea becoming two ideas."""
    iid = _invoice()
    code, r = _call(TAGS, {"op": "tag", "invoice_id": iid, "tag": "disputed"})
    assert code == 409 and "not declared" in r["error"], r
    assert _call(TAGS, {"op": "tags", "invoice_id": iid})[1]["tags"] == []


def test_apply_and_list():
    iid = _invoice()
    _declare("disputed")
    code, r = _call(TAGS, {"op": "tag", "invoice_id": iid, "tag": "disputed"})
    assert code == 200 and r["applied"] is True, r
    assert [t["tag"] for t in _call(TAGS, {"op": "tags", "invoice_id": iid})[1]["tags"]] == ["disputed"]


def test_tags_are_a_set_not_a_lifecycle():
    """Applying one never removes another. Exclusivity would lead to ordering, ordering to legal
    transitions, and that is the status machine again with none of its guarantees."""
    iid = _invoice()
    for t in ("checked-in", "checked-out"):
        _declare(t)
        _call(TAGS, {"op": "tag", "invoice_id": iid, "tag": t})
    got = [t["tag"] for t in _call(TAGS, {"op": "tags", "invoice_id": iid})[1]["tags"]]
    assert got == ["checked-in", "checked-out"], got


def test_applying_twice_is_one_tag():
    iid = _invoice()
    _declare("vip")
    _call(TAGS, {"op": "tag", "invoice_id": iid, "tag": "vip"})
    _call(TAGS, {"op": "tag", "invoice_id": iid, "tag": "vip"})
    assert len(_call(TAGS, {"op": "tags", "invoice_id": iid})[1]["tags"]) == 1


def test_remove_and_removing_what_is_not_there():
    iid = _invoice()
    _declare("vip")
    _call(TAGS, {"op": "tag", "invoice_id": iid, "tag": "vip"})
    assert _call(TAGS, {"op": "untag", "invoice_id": iid, "tag": "vip"})[1]["removed"] is True
    assert _call(TAGS, {"op": "untag", "invoice_id": iid, "tag": "vip"})[1]["removed"] is False
    assert _call(TAGS, {"op": "tags", "invoice_id": iid})[1]["tags"] == []


def test_find_is_a_query_not_a_scan():
    """`tag-index` is why a set attribute on the invoice was the wrong store — a set cannot be a GSI
    key, and a firm with ten thousand invoices should pay for the matches."""
    _declare("audit-2029")          # its own tag: the cases share tables, and find is global
    a, b = _invoice(), _invoice()
    c = _invoice()
    for iid in (a, b):
        _call(TAGS, {"op": "tag", "invoice_id": iid, "tag": "audit-2029"})
    found = set(_call(TAGS, {"op": "find_by_tag", "tag": "audit-2029"})[1]["invoice_ids"])
    assert found == {a, b} and c not in found, found


def test_the_invoice_reads_back_its_tags():
    iid = _invoice()
    _declare("vip")
    _call(TAGS, {"op": "tag", "invoice_id": iid, "tag": "vip"})
    assert H.get_invoice(iid)["tags"] == ["vip"]


def test_a_tag_is_a_rule_callsite():
    """Where the firm's automation hangs. Applying `ready-to-bill` could fire a rule that issues."""
    import instances as INST
    iid = _invoice()
    _declare("ready-to-bill")
    INST.add(matches=INST.key(INST.INVOICE_TAG, "ready-to-bill"), n=100, name="autopay",
             rule="charge_saved_card", param={})
    os.environ["INTERNAL_BUS_NAME"] = os.environ.get("INTERNAL_BUS_NAME") or "x"
    code, r = _call(TAGS, {"op": "tag", "invoice_id": iid, "tag": "ready-to-bill"})
    assert code == 200
    assert r.get("rules"), "the attached rule ran"


def test_an_unattended_refusal_files_an_incident():
    """An agent applying interactively is told by name and declares it in the turn. A rule effect or
    a script has nobody watching, so the refusal has to become a task with the owner."""
    import contextlib
    import io
    iid = _invoice()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _call(TAGS, {"op": "tag", "invoice_id": iid, "tag": "typoed"})
    hit = next(json.loads(l) for l in buf.getvalue().splitlines()
               if l.startswith("{") and '"incident"' in l)
    assert hit["incident"] == "fail"
    assert hit["subject"] == "invoice-tag:typoed"
    assert hit["category"] == "invoicing"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all invoice-tag tests passed")
