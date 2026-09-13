"""Shared helpers for tests/inventory/local/."""

import contextlib
import json
import importlib.util
import inspect
import os
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = REPO_ROOT / "modules" / "inventory"
LAMBDAS_DIR = MODULE_DIR / "lambdas"
RULES_DIR = REPO_ROOT / "modules" / "rules"


def load_lambda(name):
    # these dirs on the path mirror the lambda zip, where every bundled lib sits at the zip root
    # beside main.py: main.py's own dir, the module root (movements/availability/capacity/
    # catalog_rules), and modules/rules (the engine, bundled from outside the module).
    # the lambda's OWN dir last, so it shadows: manage_stock's op bodies are sibling modules
    # beside its main.py, the way the zip flattens them
    for d in (LAMBDAS_DIR, MODULE_DIR, RULES_DIR, LAMBDAS_DIR / name):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    for mod_name in list(sys.modules):
        if mod_name in ("_helpers", "movements", "availability", "capacity", "catalog_rules", "stock_rules", "rules", "instances",
                        "create_item", "update_stock", "get_stock") \
                or mod_name.startswith("lambda_inventory_"):
            del sys.modules[mod_name]
    path = LAMBDAS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"lambda_inventory_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# the wave-2 merge: the old verbs are ops on two tools. `load_tool` keeps each test reading as
# the verb it exercises while every call goes through the real op router in main.py.
_TOOL_OPS = {"create_item": ("manage_stock", "create_item"), "update_stock": ("manage_stock", "move"),
             "get_stock": ("manage_stock", "get"), "get_availability": ("reserve", "availability")}


class _OpTool:
    def __init__(self, name):
        fat, op = _TOOL_OPS.get(name, (name, None))
        self.mod, self.op = load_lambda(fat), op

    def __getattr__(self, k):
        return getattr(self.mod, k)

    def handler(self, event, context=None):
        body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
        if self.op:
            body = {"op": self.op, **body}
        return self.mod.handler({"body": json.dumps(body)}, context)


def load_tool(name):
    return _OpTool(name)


@contextlib.contextmanager
def scratch_env():
    name = "anon"
    for frame in inspect.stack()[1:]:
        if frame.function.startswith("test_"):
            name = f"{Path(frame.filename).stem}__{frame.function}"
            break
    out_dir = REPO_ROOT / "out" / name
    logs_dir = REPO_ROOT / "logs" / name
    for d in (out_dir, logs_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from helpers.localaws import make_table

    from helpers.localaws import books, seed_registry

    # `books()` is accounting's substrate — a COGS or receipt entry dispatches in-process to the
    # real post_journal_entry. ONE schema table carries the chart AND inventory's field registry.
    bk = books(name)
    seed_registry(bk["SCHEMA_TABLE"], "item_fields")
    overrides = {
        **bk,
        "ITEMS_TABLE":          make_table("inventory-items"),
        "MOVEMENTS_TABLE":      make_table("inventory-movements"),
        "RULE_INSTANCES_TABLE": make_table("rules-instances"),
        "RULES_PARAMS_TABLE":   make_table("rules-params"),
        "LOCAL_LOGS":           str(logs_dir),
        "LEDGER_INCEPTION":     "2020-01",   # these fixtures carry their own historical dates
    }
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    prior_lambda = os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
    try:
        yield out_dir, logs_dir
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if prior_lambda is not None:
            os.environ["AWS_LAMBDA_FUNCTION_NAME"] = prior_lambda
