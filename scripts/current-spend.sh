#!/usr/bin/env bash
# What AWS has cost this month, org-wide and by service.
#
#   bash scripts/current-spend.sh            month to date
#   bash scripts/current-spend.sh 2026-07    a whole month, finished or not
#
# Cost Explorer lives in the MANAGEMENT account (consolidated billing sees every linked account),
# so this runs on the default profile rather than operator-org. Figures are UnblendedCost and lag
# real usage by up to a day — the last day of a month-to-date read is usually still filling in.
# Each Cost Explorer call is a cent, and a run makes one per account that cost anything.
set -euo pipefail
cd "$(dirname "$0")/.."

MONTH="${1:-}"
exec .venv/bin/python - "$MONTH" <<'PY'
import boto3, datetime, sys

month = sys.argv[1] if len(sys.argv) > 1 else ""
today = datetime.date.today()
if month:
    y, m = (int(x) for x in month.split("-"))
    start = datetime.date(y, m, 1)
    end = datetime.date(y + (m == 12), (m % 12) + 1, 1)
    # End is exclusive: through today means tomorrow, which also keeps the 1st of the month valid
    end = min(end, today + datetime.timedelta(days=1)) if (y, m) == (today.year, today.month) else end
else:
    start, end = today.replace(day=1), today + datetime.timedelta(days=1)

session = boto3.Session(profile_name="default")
ce = session.client("ce", region_name="us-east-1")
period = {"Start": start.isoformat(), "End": end.isoformat()}
# the account's name beside its id, from the organization
names = {a["Id"]: a["Name"] for p in session.client("organizations").get_paginator("list_accounts").paginate()
         for a in p["Accounts"]}


def cost(group_key, extra_filter=None):
    kw = {"TimePeriod": period, "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"],
          "GroupBy": [{"Type": "DIMENSION", "Key": group_key}]}
    if extra_filter:
        kw["Filter"] = extra_filter
    got = ce.get_cost_and_usage(**kw)["ResultsByTime"]
    rows = [] if not got else [(g["Keys"][0], float(g["Metrics"]["UnblendedCost"]["Amount"]))
                               for g in got[0]["Groups"]]
    return sorted(rows, key=lambda r: -r[1])


accounts = cost("LINKED_ACCOUNT")
total = sum(a for _, a in accounts)
days = (end - start).days or 1
print(f"\n{start} .. {end}   ({days} day{'s' * (days != 1)})\n")
print(f"  {'TOTAL':<46} ${total:>9,.2f}")
if not month or (start.year, start.month) == (today.year, today.month):
    print(f"  {'run rate, this month':<46} ${total / days * 30:>9,.2f}")
print()

# only the accounts worth a line; the rest round to nothing and just make noise
for acct, amt in accounts:
    if amt < 0.01:
        continue
    print(f"  {names.get(acct, ''):<24} {acct:<21} ${amt:>9,.2f}")
    services = cost("SERVICE", {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [acct]}})
    for svc, s_amt in services:
        if s_amt >= 0.01:
            print(f"      {svc:<42} ${s_amt:>9,.2f}")
    print()
PY
