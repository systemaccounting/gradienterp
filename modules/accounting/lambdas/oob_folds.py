"""Shared by accounting's public reads (`oob_financials`, `oob_metrics`): the published gate and
the trailing-window ledger rows. Bundled into each lambda by the import graph."""

from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key


def published(ddb, settings_table, gerp_id):
    """`GERP#openly_operated` off the gerp's own settings row — one rule, checked at every exit."""
    row = ddb.Table(settings_table).get_item(Key={"gerp_id": gerp_id, "sk": "GERP#openly_operated"}).get("Item") or {}
    return bool(row.get("value", False))


def window_rows(ddb, ledger_table, window_days, now=None):
    """Every ledger pair row in the trailing window, oldest first. The window spans a few month
    partitions; each is queried and filtered to the exact cutoff."""
    now = now or datetime.now(timezone.utc)
    cutoff_ms = int((now - timedelta(days=window_days)).timestamp() * 1000)
    months = sorted({(now - timedelta(days=n)).strftime("%Y-%m") for n in range(window_days)})
    table = ddb.Table(ledger_table)
    rows = []
    for m in months:
        for r in table.query(KeyConditionExpression=Key("pk").eq(m)).get("Items", []):
            if int(r.get("timestamp_ms", 0)) >= cutoff_ms:
                rows.append(r)
    rows.sort(key=lambda r: int(r.get("timestamp_ms", 0)))
    return rows


def downsample(series, n=30):
    """At most n points, evenly spaced — the sparkline's resolution."""
    return series if len(series) <= n else [series[round(i * (len(series) - 1) / (n - 1))] for i in range(n)]
