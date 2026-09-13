"""Shared response envelope for the automation lambdas.

Same shape every other module's tools return — {statusCode, body-as-json-string} —
because the gateway and `ctx.call` both unwrap it the same way.
"""

import json
from decimal import Decimal

# Where an approved automation is filed, and what runs it. Derived from the FILENAME rather than
# asked for: the extension already answers it, one to one, because each runner can execute exactly
# one thing.
#
#   .py        modules    `automate` — the gerp's gateway tools, the rules table, no internet
#   .sh        external   `modules/cmd` — internet, the cabinet, its own env secrets, no module tool
#   .asl.json  machines   Step Functions — nothing of ours executes it; it is handed to AWS
#
# Asking the caller meant a `.py` could be filed as `external`, which was accepted and then failed
# as AccessDenied at run time, far from the mistake. Derived, that is unrepresentable.
#
# For the first two the destination prefix IS the boundary — only `automate`'s role can read
# `approved/modules/`, only `cmd`'s can read `approved/external/`. For machines it decides only who
# may DEPLOY; what a running machine can touch is its execution role (`machines.tf`), unrelated to
# where the definition was filed. So do not reason that the prefix bounds a machine. It does not.
BY_EXTENSION = ((".asl.json", "machines"), (".py", "modules"), (".sh", "external"))
KINDS = tuple(kind for _, kind in BY_EXTENSION)


def kind_of(script: str) -> str | None:
    """Which prefix `script` belongs under, or None when its extension names none of them."""
    name = (script or "").strip().lower()
    return next((kind for ext, kind in BY_EXTENSION if name.endswith(ext)), None)


def kind_or_error(script: str):
    """(kind, None) or (None, a message naming what is accepted)."""
    kind = kind_of(script)
    if kind:
        return kind, None
    return None, (f"cannot tell what {script!r} is from its name — a script is '.py' (runs through "
                  "automate), '.sh' (runs through cmd), or '.asl.json' (a Step Functions definition)")



class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        return super().default(o)


def ok(body: dict, status: int = 200) -> dict:
    return {"statusCode": status, "body": json.dumps(body, cls=_DecimalEncoder)}


def err(message: str, status: int = 400, **extra) -> dict:
    return {"statusCode": status, "body": json.dumps({"error": message, **extra}, cls=_DecimalEncoder)}
