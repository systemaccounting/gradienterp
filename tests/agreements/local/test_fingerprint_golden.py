"""Golden fingerprints — the one thing consolidating `request` must not break.

`terms_hash` is a sha256 of `{items, total}` after sorting items and canonicalising the json, and
**both sides of a cross-firm deal compute it independently**. A buyer's `create_po` and the seller's
`accept_po` land on the same row only because they produce the same 16 hex characters from the same
substance.

So if a consolidated `agreements/request` folds a kind's named arguments into even a slightly
different shape than the wrapper it replaces — a key that used to be omitted now present as null, a
number that arrives as a string, an item field reordered into the dict — every in-flight agreement
silently stops matching its counterparty's. Nothing errors. The rows just never meet, and the deal
sits at one stamp forever looking like the other side never answered.

These hashes were captured from the CURRENT wrappers on 2026-07-28. `capital_capped` is
`1b72ab6df94ea7b4` — the same hash a live agent quoted back on a real bid, so the golden is
production's, not a fixture's. They are not a description of what the fold SHOULD be — they are what
it IS, and any change is a break unless someone decided otherwise deliberately.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "agreements"))
from agreements import terms_fingerprint  # noqa: E402


# ── the three kinds, folded exactly as their wrappers fold them today ──────────────────────────────

def _capital_terms(factor, price, cap=None, product="net_income_percent_dividend"):
    """treasury/propose_offer: the instrument IS the item, the price IS the total."""
    item = {"product": product, "factor": factor}
    if cap is not None:
        item["cap"] = cap
    return {"items": [item], "total": price}


def _po_terms(lines):
    """purchasing/create_po: items carry description+amount, and sku+qty when a line names them
    (2026-09-06: the seller's item id and the count are substance a seller's rule reads, so they
    hash). The buyer's posting accounts and its own item_id ride as row metadata, deliberately
    outside the fingerprint, because the seller doesn't have them and both sides must hash the same
    substance. A line without sku/qty hashes exactly as before that date."""
    items = [{"description": ln["description"], "amount": ln["amount"],
              **{k: ln[k] for k in ("sku", "qty") if ln.get(k) is not None}} for ln in lines]
    return {"items": items, "total": sum(ln["amount"] for ln in lines)}


GOLDEN = {
    "capital_capped":     (_capital_terms(0.10, 500000, 550000),             "1b72ab6df94ea7b4"),
    "capital_perpetuity": (_capital_terms(0.05, 100000, None,
                                          "net_income_percent_perpetuity"),  "ef3eb334f6499967"),
    "po_single":          (_po_terms([{"description": "espresso beans", "amount": 240}]),
                                                                             "039cc7575dd4c318"),
    "po_multi":           (_po_terms([{"description": "espresso beans", "amount": 240},
                                      {"description": "oat milk", "amount": 60}]),
                                                                             "b055901e5c626be4"),
    # pinned 2026-09-06 when sku+qty entered the wire: a line that names them hashes with them
    "po_sku_qty":         (_po_terms([{"description": "espresso beans", "amount": 240,
                                       "sku": "1#beans-1kg", "qty": 10}]),
                                                                             "4bc31487d3dbf59b"),
}


def test_golden_fingerprints_have_not_moved():
    """Every kind's fold still produces the hash it produced before consolidation."""
    drift = {}
    for name, (terms, expected) in GOLDEN.items():
        got = terms_fingerprint(terms)
        if got != expected:
            drift[name] = (expected, got)
    assert not drift, ("terms_hash moved — in-flight agreements will stop matching their "
                       f"counterparty's row: {drift}")


def test_item_order_does_not_change_the_hash():
    """Items sort before hashing, so two sides listing the same lines differently still meet.

    This is why a PO with lines in a different order still lands on one row."""
    a = _po_terms([{"description": "espresso beans", "amount": 240},
                   {"description": "oat milk", "amount": 60}])
    b = _po_terms([{"description": "oat milk", "amount": 60},
                   {"description": "espresso beans", "amount": 240}])
    assert terms_fingerprint(a) == terms_fingerprint(b)


def test_an_absent_cap_is_not_a_null_cap():
    """A perpetuity carries NO `cap` key — it does not carry `cap: None`.

    The difference is invisible in Python and total in the hash, and it is exactly the mistake a
    rewritten fold makes: building the item dict with every field and letting None ride along."""
    without = {"items": [{"product": "p", "factor": 0.05}], "total": 100000}
    with_null = {"items": [{"product": "p", "factor": 0.05, "cap": None}], "total": 100000}
    assert terms_fingerprint(without) != terms_fingerprint(with_null)


def test_a_number_as_a_string_is_a_different_deal():
    """`500000` and `"500000"` hash differently. A fold that stringifies on the way through — or a
    caller that passes JSON-decoded Decimals where ints were — breaks the match."""
    assert terms_fingerprint({"items": [], "total": 500000}) != \
           terms_fingerprint({"items": [], "total": "500000"})


if __name__ == "__main__":
    for fn in [n for n in dir() if n.startswith("test_")]:
        globals()[fn]()
        print(f"ok {fn}")
    print("all fingerprint tests passed")
