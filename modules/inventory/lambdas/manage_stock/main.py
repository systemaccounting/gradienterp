"""manage_stock — the stock half of the physical meter, one tool.

op `create_item` / `move` / `get`. `move` records a movement on the append-only log (the old
update_stock, under inventory's own vocabulary — one movement log, totals are folds); `get` reads
the cached count. Capacity items are `reserve`'s (op `availability` there). The op bodies live in
their original modules beside this router, moved in unchanged.
"""

import json

import create_item as _create_item
import get_stock as _get_stock
import update_stock as _move

from _helpers import err

OPS = {"create_item": _create_item.handler, "move": _move.handler, "get": _get_stock.handler}


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    op = (body.pop("op", "") or "").strip()
    fn = OPS.get(op)
    if not fn:
        return err("op must be create_item | move | get")
    return fn(body, context)
