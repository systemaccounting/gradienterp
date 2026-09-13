import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from aws import client as _aws, table as _table, log as alog

log = logging.getLogger()


# Resolved at call time so a harness can point at a scratch table between cases.
def pending_table():
    return _table(os.environ["PENDING_TABLE"])


def ledger_table():
    return _table(os.environ["LEDGER_TABLE"])


def registry_table():
    return _table(os.environ["SCHEMA_TABLE"])


def settings_table():
    return _table(os.environ["SETTINGS_TABLE"])


def _customer_id() -> str:
    return os.environ.get("CUSTOMER_ID", "local")


def _openly_operated() -> bool:
    """Current publication consent, read PER INVOKE, never cached.

    It is consent, and consent is a reference: a cold-start cache is a snapshot of a mutable fact,
    so a gerp turning publication off kept stamping `true` until the container recycled — and the
    `gerp-publication` rule routes flagged events to a firehose ARCHIVE, which is unrecoverable.
    One GetItem against the settings table is the price of that being right."""
    row = settings_table().get_item(
        Key={"gerp_id": _customer_id(), "sk": "GERP#openly_operated"}
    ).get("Item") or {}
    return bool(row.get("value", False))


# ─── chart-of-accounts predicate ───
#
# is_account(name) checks that an account name is in the customer's registry
# (canonical seed + extensions added via add_classification → extend_schema).
# Source: per-customer registry DDB, queried once at cold start and cached.
#
# Defends against module-version-skew (payments / inventory / labor emitting
# names the customer's registry doesn't know yet) and agent hallucination.
#
# It used to return True unconditionally outside Lambda, so an account name no registry
# knew passed every local test and 400'd in production. It reads the registry now.

_KNOWN_ACCOUNTS = None


def _load_chart_of_accounts():
    global _KNOWN_ACCOUNTS
    if _KNOWN_ACCOUNTS is not None:
        return

    names = set()
    kwargs = {
        "KeyConditionExpression": "#r = :r",
        "ExpressionAttributeNames": {"#r": "registry"},
        "ExpressionAttributeValues": {":r": "chart_of_accounts"},
    }
    while True:
        resp = registry_table().query(**kwargs)
        for item in resp.get("Items", []):
            names.add(item["name"])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    _KNOWN_ACCOUNTS = names


def is_account(name: str) -> bool:
    _load_chart_of_accounts()
    return name in _KNOWN_ACCOUNTS


# ─── what KIND of account it is, read from the chart ───
#
# An account's kind is a property of the ACCOUNT, so it is looked up, never taken from the caller.
# CASH is an asset because the chart says so; nothing that posts to it gets an opinion. This is why
# no rule — canonical or one a firm wrote — can book cash as revenue: not because a check catches it,
# but because nobody asks. The registry is already read at cold start, so it costs a dict lookup.

_ACCOUNT_KINDS = None


def _load_account_kinds():
    global _ACCOUNT_KINDS
    if _ACCOUNT_KINDS is not None:
        return
    kinds = {}
    kwargs = {
        "KeyConditionExpression": "#r = :r",
        "ExpressionAttributeNames": {"#r": "registry"},
        "ExpressionAttributeValues": {":r": "chart_of_accounts"},
    }
    while True:
        resp = registry_table().query(**kwargs)
        for item in resp.get("Items", []):
            kinds[item["name"]] = str(item["bucket"]).upper()
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    _ACCOUNT_KINDS = kinds


def account_kind(name: str) -> str | None:
    """The kind of account `name` is, per the chart — ASSET | LIABILITY | EQUITY | REVENUE | EXPENSE.
    None for an account the chart doesn't know (a reconcile-booked merchant slug), which is what
    routes an entry to pending for the owner to classify."""
    _load_account_kinds()
    return _ACCOUNT_KINDS.get(name)


def _year_month(timestamp_ms: int) -> str:
    """Derive DDB hash key 'YYYY-MM' from a ms-epoch timestamp."""
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def _to_epoch_ms(ts) -> int:
    """Normalize a caller timestamp to epoch-millis — the schema advertises 'ISO 8601 or epoch-millis',
    so accept both (a receipt date like '2026-06-14' arrives ISO). Epoch-millis passes through; an ISO
    date/datetime parses (naive treated as UTC); None/empty → now. Deterministic, so a resubmit with the
    same input yields the same sk (idempotency)."""
    if ts is None or ts == "":
        return int(time.time() * 1000)
    if isinstance(ts, (int, float)):
        return int(ts)
    s = str(ts).strip()
    if s.isdigit():
        return int(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# Which dimension keys describe the TRANSACTION and which name a PERSON. The registry is the
# source (`modules/schemas/data/ledger_fields.json`, bucket `dimensions`); this constant is the
# same list, resolved at import so a hot write path never reads DDB. Keep them in step — the
# registry is what a public reader consults, this is what the writer files by.
_PUBLISHABLE_DIMS = frozenset({
    "location", "job", "task", "period", "instrument_id", "rule",
    "started_at", "ended_at", "issuer", "holder", "role",
})


def _split_dimensions(dimensions):
    """One caller-supplied map -> (dims, dims_private).

    `dims` holds the keys that describe the transaction; a public reader publishes it wholesale.
    `dims_private` holds the rest — a person reference (`worker_id`, `created_by`) or a key nobody
    has classified yet. UNKNOWN GOES PRIVATE: a caller adding a dimension cannot publish it by
    accident, and the cost of getting it wrong is a missing slice someone reports rather than a
    leak nobody notices.

    Both are stamped on the row. Internal readers (the sliced statements) match against the union,
    so nothing loses the ability to slice by worker; only the PUBLIC projection narrows."""
    dims, private = {}, {}
    for key, value in (dimensions or {}).items():
        (dims if key in _PUBLISHABLE_DIMS else private)[key] = value
    return dims, private


def _economic_counters(line_items):
    """Economic-index signals stamped on the event so the platform's *dumb* counter lambda can apply
    them with no accounting knowledge (translate at the boundary — the module that knows what revenue
    is sets the key + magnitude; the platform just does the arithmetic). Each is {op, key, magnitude}.
    revenue = credit legs to revenue accounts (accrual — recognized when the entry is booked);
    expense = debit legs to expense accounts, cost of goods sold included — the two the economy's
    margin is computed from. A new signal is a new list entry here; the counter lambda never changes."""
    def _sum(side, account_type):
        return sum((Decimal(str(li["amount"])) for li in line_items
                    if li.get("side") == side and (li.get("accountType") or "").upper() == account_type), Decimal(0))
    counters = []
    revenue, expense = _sum("CREDIT", "REVENUE"), _sum("DEBIT", "EXPENSE")
    if revenue > 0:
        counters.append({"op": "add", "key": "revenue", "magnitude": float(revenue)})
    if expense > 0:
        counters.append({"op": "add", "key": "expense", "magnitude": float(expense)})
    return counters


def _emit_journal_entry_posted(entry_id, timestamp_ms, origin, line_items):
    detail = {
        "schema_version": 1,
        "openly_operated": _openly_operated(),
        "customer_id": _customer_id(),
        "entry_id": entry_id,
        "posted_at_ms": timestamp_ms,
        "origin": origin,
        "line_items": line_items,
    }
    counters = _economic_counters(line_items)  # economic-index signals; platform ADDs them (dumb)
    if counters:
        detail["counters"] = counters  # only stamp when there's a signal → only these route to the counter lambda

    try:
        _aws("events").put_events(Entries=[{
            "EventBusName": os.environ["OP_EVENT_BUS_ARN"],
            "Source": "accounting",
            "DetailType": "journal_entry.posted",
            "Detail": json.dumps(detail),
        }])
    except Exception:
        alog.exception("journal_entry.posted not published; the entry stands", entry_id=entry_id)
        return False
    return True


def _decompose_to_pairs(line_items):
    """Decompose a balanced N-leg entry into N-1 balanced debit/credit pair rows.
    Each output dict has {debit_account, debit_account_type, credit_account,
    credit_account_type, amount}. Greedy matching: consume smallest overlap
    between the next debit and next credit.

    **Where a leg came from rides per SIDE.** A leg a rule produced carries the `rule_key` of the instance
    that produced it, and the pairing is greedy — one pay-run entry holds legs from eight different
    rules, so a pair row's debit and its credit routinely come from different ones. Collapsing them
    into a single row-level key would be a lie, so the row keeps `debit_rule_key` / `credit_rule_key`
    separately, and `rule_exec_id` as the union of both legs' executions (which is what ties the row
    back to the objects those rules read and created)."""
    debits = [[li["account"], li["accountType"], Decimal(str(li["amount"])), li.get("rule_key"), li.get("rule_exec_id") or []]
              for li in line_items if li["side"] == "DEBIT"]
    credits = [[li["account"], li["accountType"], Decimal(str(li["amount"])), li.get("rule_key"), li.get("rule_exec_id") or []]
               for li in line_items if li["side"] == "CREDIT"]
    pairs = []
    while debits and credits:
        d = debits[0]
        c = credits[0]
        amt = min(d[2], c[2])
        exec_ids = list(dict.fromkeys([*(d[4] or []), *(c[4] or [])]))
        pairs.append({
            "debit_account": d[0], "debit_account_type": d[1],
            "credit_account": c[0], "credit_account_type": c[1],
            "amount": float(amt),
            **({"debit_rule_key": d[3]} if d[3] else {}),
            **({"credit_rule_key": c[3]} if c[3] else {}),
            **({"rule_exec_id": exec_ids} if exec_ids else {}),
        })
        d[2] -= amt
        c[2] -= amt
        if d[2] == 0:
            debits.pop(0)
        if c[2] == 0:
            credits.pop(0)
    return pairs


def handler(event, context):
    # Callers invoke this function; none reaches it over HTTP. An event from API Gateway is someone
    # on the internet posting to the books, and is refused before the body is read.
    if (event.get("requestContext") or {}).get("http"):
        return {"statusCode": 403, "headers": {"content-type": "application/json"},
                "body": json.dumps({"error": "post_journal_entry is invoked, not called over HTTP"})}
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event
    line_items = body["lineItems"]
    memo = body.get("memo", "")
    source = body.get("source", "")
    # optional {worker_id, role, ...} carried onto each ledger/pending row so downstream can do
    # per-dimension sums (payroll runs, W-2/941 boxes). Omitted by most callers; additive.
    #
    # It arrives as ONE map mixing classes — `location`/`job`/`period` describe the transaction,
    # `worker_id`/`created_by` name a PERSON — which left a public reader unable to publish any of
    # it without hand-picking keys. Callers still pass one `dimensions` map; the SPLIT happens here
    # (see `_split_dimensions`), so a reader publishes `dims` wholesale and can never reach a
    # person by forgetting a key.
    dimensions = body.get("dimensions")
    # the location backstop: EVERY entry is attributed from day one ("1" = main by doctrine —
    # a constant, never a settings read; the ledger is append-only so attribution is
    # now-or-never). Keyed on the location KEY being absent, not on dimensions being None —
    # callers like treasury pass a dims dict with no location.
    if not (dimensions or {}).get("location"):
        dimensions = {**(dimensions or {}), "location": "1"}
    dims, dims_private = _split_dimensions(dimensions)
    timestamp = _to_epoch_ms(body.get("timestamp"))  # ISO 8601 or epoch-millis → epoch-millis int (schema promises both)

    # An account's KIND comes from the chart, not from whoever is posting. A caller says WHICH account
    # and WHICH side; what that account IS — asset, liability, revenue — is the chart's answer, so a
    # stated accountType is overwritten with it. CASH cannot be booked as revenue and a collected tax
    # cannot be recognised as income: not because a check rejects the claim, but because the claim is
    # not consulted.
    #
    # A leg with NO accountType is left alone — that blank is not ignorance of the kind, it is the
    # ingest path saying "the owner should confirm which account this belongs to" (a Stripe charge the
    # transform guessed at SALES_REVENUE), and it is what routes the entry to pending. Filling it here
    # would post straight to the ledger and skip the review.
    for li in line_items:
        if li.get("accountType"):
            kind = account_kind(li.get("account"))
            if kind:
                li["accountType"] = kind

    # amounts are magnitudes — the DEBIT/CREDIT side encodes direction. A non-positive
    # amount is always a bug: a negative leg flips its account's normal balance AND still
    # passes the debits==credits check below (e.g. both legs −0.33 are "balanced"),
    # silently corrupting balances. This caught the Square-payout sign bug. Reject loudly.
    bad_amounts = [li for li in line_items if Decimal(str(li["amount"])) <= 0]
    if bad_amounts:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": "line item amounts must be positive (the DEBIT/CREDIT side encodes direction)",
                "badLineItems": [{"account": li.get("account"), "amount": li["amount"]} for li in bad_amounts],
            }),
        }

    # reject unknown account names on FULLY-CLASSIFIED entries (accountType
    # present on every line — meaning the entry is about to land in the ledger).
    # entries with any missing accountType go to pending below for owner/agent
    # classification; add_classification → extend_schema on the way back
    # makes any newly-introduced account names real before they hit this check.
    fully_classified = all("accountType" in li for li in line_items)
    if fully_classified:
        unknown = sorted({li["account"] for li in line_items if not is_account(li["account"])})
        if unknown:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "error": "unknown account names; not in customer's registry",
                    "unknownAccounts": unknown,
                }),
            }

    # kirchhoff test: entry must sum to zero in account-space
    total_debits = sum(Decimal(str(li["amount"])) for li in line_items if li["side"] == "DEBIT")
    total_credits = sum(Decimal(str(li["amount"])) for li in line_items if li["side"] == "CREDIT")

    if total_debits != total_credits:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": "debits do not equal credits",
                "totalDebits": str(total_debits),
                "totalCredits": str(total_credits),
            }),
        }

    entry_id = body.get("entryId", str(uuid.uuid4()))

    # if any line item is missing account_type, queue to pending
    classified = all("accountType" in li for li in line_items)

    if not classified:
        pending_item = {
            "entry_id": entry_id,
            "timestamp": timestamp,
            "line_items": json.dumps(line_items),
            "memo": memo,
            "source": source,
            **({"dims": dims} if dims else {}),
            **({"dims_private": dims_private} if dims_private else {}),
        }
        pending_table().put_item(Item=pending_item)

        return {
            "statusCode": 202,
            "body": json.dumps({
                "entryId": entry_id,
                "timestamp": timestamp,
                "status": "pending_classification",
            }),
        }

    pairs = _decompose_to_pairs(line_items)

    timestamp_int = int(timestamp)
    pk = _year_month(timestamp_int)
    # zero-pad the timestamp in sk so lexicographic order = numeric order.
    # 20 digits is far-future-proof (covers through year 5138 in millis).
    sk_ts = f"{timestamp_int:020d}"
    table = ledger_table()

    for i, p in enumerate(pairs):
        item = {
            "pk": pk,
            "sk": f"{sk_ts}#{entry_id}#{i}",
            "entry_id": entry_id,
            "timestamp_ms": timestamp_int,
            "debit_account": p["debit_account"],
            "debit_account_type": p["debit_account_type"],
            "credit_account": p["credit_account"],
            "credit_account_type": p["credit_account_type"],
            "amount": Decimal(str(p["amount"])),
            "memo": memo,
            "source": source,
            **({"dims": dims} if dims else {}),
            **({"dims_private": dims_private} if dims_private else {}),
            # which rule instance produced each side, and the executions behind them
            **({"debit_rule_key": p["debit_rule_key"]} if p.get("debit_rule_key") else {}),
            **({"credit_rule_key": p["credit_rule_key"]} if p.get("credit_rule_key") else {}),
            **({"rule_exec_id": p["rule_exec_id"]} if p.get("rule_exec_id") else {}),
        }
        try:
            kwargs = {"Item": item}
            if i == 0:
                # idempotency is keyed on the full (pk, sk) — and sk =
                # <timestamp>#<entry_id>#<i>. So a resubmission only no-ops if it
                # repeats BOTH entry_id and timestamp; entry_id alone won't dedup
                # across a fresh now(). Callers that need idempotency pass a
                # deterministic timestamp with their entry_id (close-handler:
                # ended_at; pay_run: the period start). The condition on i==0
                # skips the remaining pair rows so we never partial-write.
                kwargs["ConditionExpression"] = "attribute_not_exists(pk)"
            table.put_item(**kwargs)
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            if i == 0:
                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "entryId": entry_id,
                        "timestamp": timestamp,
                        "lineItems": line_items,
                        "memo": memo,
                        "source": source,
                        "duplicate": True,
                    }),
                }
            raise

    event_published = _emit_journal_entry_posted(
        entry_id=entry_id,
        timestamp_ms=int(timestamp),
        origin=source,
        line_items=line_items,
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "entryId": entry_id,
            "timestamp": timestamp,
            "lineItems": line_items,
            "memo": memo,
            "source": source,
            "event_published": event_published,
        }),
    }
