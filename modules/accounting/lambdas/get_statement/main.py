"""get_statement — every ledger read behind one tool.

`statement` picks the fold: trial_balance / income / balance_sheet read straight off the ledger;
`balances` integrates activity into the balances checkpoint (and fires the CSV suite + treasury's
distribution on completion, as compute_balances always did). `write_csvs: true` on a read also
writes the statement suite to S3 and merges the per-statement links into the answer.

Each statement's body lives in its own sibling module, moved in unchanged from the tool that used
to carry it. A payload with `periodEnd` and no `statement` is the machine path — the reporting
cron and the balances completion-invoke send it — and runs the suite writer directly.
"""

import json
import time

import compute_balances
import generate_report
import get_balance_sheet
import get_income_statement
import get_trial_balance

STATEMENTS = {
    "trial_balance": get_trial_balance.handler,
    "income": get_income_statement.handler,
    "balance_sheet": get_balance_sheet.handler,
    "balances": compute_balances.handler,
}


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def handler(event, context):
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else dict(event)
    statement = (body.pop("statement", "") or "").strip()

    if not statement and body.get("periodEnd"):
        # the machine path: the reporting cron's input and compute's completion-invoke carry
        # {periodEnd, ...} and want the suite written, nothing read
        return generate_report.handler(body, context)

    if statement not in STATEMENTS:
        return {"statusCode": 400, "body": json.dumps({
            "error": "statement is required: trial_balance, income, balance_sheet or balances"})}

    write_csvs = bool(body.pop("write_csvs", False))
    out = STATEMENTS[statement](body, context)

    # `balances` already fires the suite writer on completion; a sync second write would only
    # repeat it. The flag applies to the three reads.
    if write_csvs and statement != "balances" and out.get("statusCode") == 200:
        period_end = body.get("periodEnd") or (body.get("range") or {}).get("end") \
            or body.get("asOf") or _now_iso()
        report = generate_report.handler({"periodEnd": period_end}, context)
        if report.get("statusCode") == 200:
            merged = json.loads(out["body"])
            merged["statements"] = json.loads(report["body"])["statements"]
            out = {"statusCode": 200, "body": json.dumps(merged)}
    return out
