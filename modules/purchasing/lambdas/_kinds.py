"""The failure kinds this module's lambdas raise, declared once (aws.Kind)."""

from aws import Kind

STOCK_MOVE_FAILED = Kind("stock_move_failed", "stock move failed", "dependency", ids=("po_id", "item_id"))
