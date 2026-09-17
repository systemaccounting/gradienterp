"""labor pay_run — the worker's pay run.

For a worker and a pay period, sum the gross wages accrued that period (the WAGES_PAYABLE
credits the close-handler posted, keyed by dimensions.worker_id), run the rule instances
that MATCH that worker, and post the resulting withholding entry.

    instances.at(instances.PAY_RUN, worker_id)
      → 0090#us_federal   us_federal   (the W-4)
        0100#fica_ss      rate_posting  6.2% capped at cap:"ss_wage_base"  → FICA_PAYABLE
        0101#fica_medicare rate_posting 1.45% uncapped                    → FICA_PAYABLE
        0110#ca_pit       ca_pit       (the DE-4)
        0120#sdi          rate_posting  1.3% → CA_SDI_PAYABLE
        0200#futa …       the employer taxes (DR PAYROLL_TAX_EXPENSE)

**The match is the dispatch.** There is no rule set on the worker and no per-worker list of names:
what a worker owes IS the rows that match them, in `n` order. A worker no rows match withholds
nothing. `run_instances` resolves each instance's `rule` by name across the bundled rule
libraries (general_rules + payroll_rules) and returns the flat effects.

**Platform tables are read as of the period.** A rule instance is current config (which rules a
worker owes, their W-4); a run period is frozen in the ledger, so the instance keeps no history. Only
the platform tables the rules read (`params.layered(..., as_of)`) are dated — a new tax year appends
and both years coexist, so June computed in August still uses June's tables.

This RECLASSIFIES the wage liability — it moves the employee tax share out of WAGES_PAYABLE
into the tax payables and books the employer match. It does NOT pay anyone: settling the
remaining net to cash (DR WAGES_PAYABLE / CR CASH) + the ACH is treasury's generic "pay a
payable", not labor's job.

Idempotent per (worker_id, period): the posted entry carries a deterministic entryId
(payrun-<worker>-<period>) AND a deterministic timestamp (the period start). Accounting dedups
on (pk, sk) where sk embeds the timestamp, so BOTH have to be stable for the rerun to no-op —
entryId alone isn't enough. The gross is the sum of WAGES_PAYABLE *credits* (the accruals); the
withholding's own DR WAGES_PAYABLE leg is a debit, so it never inflates a later run's gross.

The ledger and the rule instances are real tables the test seeds; post_journal_entry is the real
accounting handler, dispatched in-process when not in Lambda (modules/aws/aws.py).
"""

import datetime
import json
import os
import time
from decimal import Decimal

import rules           # the engine (run_instances/lineitems) — modules/rules/rules.py
import instances       # the rule-instance store — modules/rules/instances.py
import params          # the platform store (GENERAL rows) — modules/rules/params.py
import general_rules   # rate_posting — the general rules, owned by nobody
import payroll_rules   # labor's rules (wage_accrual, us_federal, ca_pit)
import metric_rules   # modules/metrics: record_metric — a moment as a product event, by a row

from boto3.dynamodb.conditions import Key

import journal
from aws import client as _aws, table as _table, log

RULE_LIBS = [general_rules, payroll_rules, metric_rules]

POST_JOURNAL_ENTRY_FN = os.environ.get("POST_JOURNAL_ENTRY_FN", "")


def ledger_table():
    return _table(os.environ["LEDGER_TABLE"])


def worker_table():
    """None when the fleet has no worker table configured — the caller falls back."""
    name = os.environ.get("WORKER_TABLE")
    return _table(name) if name else None


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


# ─── pay period ───

def _current_period() -> str:
    """The pay period we are in, on the BUSINESS'S calendar. A run kicked off on the last evening of
    a month would otherwise be stamped with next month's period — the UTC date has already rolled
    over while the business is still in the old month — and the entry's deterministic id
    (payrun-<worker>-<period>) would put it in the wrong period permanently."""
    import clock
    return clock.month_key(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))


def _months_before(period: str):
    """The YYYY-MM partitions for the same year, before `period` (Jan..prev).
    Used to total YTD SS wages for the Social-Security wage-base cap."""
    year, month = period.split("-")
    return [f"{year}-{m:02d}" for m in range(1, int(month))]


def _period_end(period: str) -> str:
    """A YYYY-MM-DD upper bound on the period, for reading the platform tables as-of
    (`params.layered`). `-31` is a lexicographic bound, not a real date — a tax table effective
    mid-period is in force for it, one effective next month is not."""
    return f"{period}-31"


def _worker_location(worker_id, period) -> str:
    """The worker's home location ordinal for the withholding entry's dims. One entry = one
    location for all legs (dims are entry-level), so a multi-location worker's withholding lands
    on their home location — the accrual entries already split by where each shift happened.
    Best-effort: any read failure, or no worker row, falls to "1"."""
    try:
        t = worker_table()
        if t is not None:
            rows = t.query(KeyConditionExpression=Key("contact_id").eq(worker_id),
                           Limit=1).get("Items", [])
            return str((rows[0] if rows else {}).get("location") or "1")
    except Exception as e:  # noqa: BLE001
        log.warning("worker contact unreadable, location falls to 1", worker_id=worker_id,
                    period=period, error=str(e))
    return "1"


def _period_start_ms(period: str) -> str:
    """Epoch-ms of the first instant of the period (YYYY-MM-01T00:00:00Z). Used as
    the entry's timestamp so it (a) lands in accounting's `period` month partition
    and (b) is STABLE across re-runs — accounting keys idempotency on (pk, sk) and
    sk embeds this timestamp, so a deterministic value is what makes the rerun
    no-op instead of double-withholding."""
    year, month = period.split("-")
    dt = datetime.datetime(int(year), int(month), 1, tzinfo=datetime.timezone.utc)
    return str(int(dt.timestamp() * 1000))


# ─── ledger reads (accounting's per-month partitions, filtered by worker) ───

def _row_dims(row) -> dict:
    """A ledger row's dimensions, whichever field they landed in — `dims` (describes the
    transaction), `dims_private` (names a person), or the pre-split `dimensions`."""
    return {**(row.get("dimensions") or {}),
            **(row.get("dims") or {}),
            **(row.get("dims_private") or {})}


def _wages_payable_credits(worker_id: str, period: str) -> Decimal:
    """Sum the WAGES_PAYABLE credits posted to `period` for this worker — i.e. the
    gross the close-handler accrued that month. Accrual rows are CR WAGES_PAYABLE;
    a withholding's DR WAGES_PAYABLE leg is a debit and is excluded, so re-running
    payroll doesn't re-tax already-withheld wages."""
    total = Decimal(0)
    for row in _ledger_partition(period):
        if row.get("credit_account") != "WAGES_PAYABLE":
            continue
        # worker_id is a PERSON reference, so post_journal_entry files it under `dims_private`,
        # not `dims` — and never under `dimensions`, which is the pre-split field. Reading one
        # field matched nothing on a real posted row, so every worker's accrued gross read as 0.
        if _row_dims(row).get("worker_id") != worker_id:
            continue
        total += Decimal(str(row.get("amount", 0)))
    return total


def _ledger_partition(period: str):
    """Yield the ledger pair-rows in one YYYY-MM partition."""
    kwargs = {"KeyConditionExpression": Key("pk").eq(period)}
    while True:
        resp = ledger_table().query(**kwargs)
        yield from resp.get("Items", [])
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return
        kwargs["ExclusiveStartKey"] = lek


def _worker_rows(worker_id: str) -> list:
    """The worker's rows, one per role."""
    t = worker_table()
    if t is None:
        return []
    return t.query(KeyConditionExpression=Key("contact_id").eq(worker_id)).get("Items", [])


def _cutover_ytd(worker_id: str, period: str) -> Decimal:
    """Pre-platform wages for a migrated worker. The migration walk stores what the OLD system
    paid this person Jan 1 → cutover on a worker row (`ytd_wages_at_cutover` + `cutover_date`);
    those wages consumed the SS/FUTA/SUTA wage bases but sit in nobody's ledger here, so runs in
    the cutover's own calendar year add them to the YTD the caps read. Later years read zero.
    Per-person, set on ONE role row — the first row carrying it wins."""
    year = period.split("-")[0]
    for row in _worker_rows(worker_id):
        amt = row.get("ytd_wages_at_cutover")
        if amt is not None and str(row.get("cutover_date") or "")[:4] == year:
            return Decimal(str(amt))
    return Decimal(0)


def _ytd_gross_wages(worker_id: str, period: str) -> Decimal:
    """Gross wages year-to-date *before* this period — the prior months'
    WAGES_PAYABLE credits, plus the pre-cutover wages a migrated worker's row
    carries for the cutover year. Feeds every wage-base cap a `rate_posting`
    instance declares (`cap` + `consumed: "gross_wages"`): the SS base, the
    $7,000 unemployment base."""
    ledger = sum((_wages_payable_credits(worker_id, m) for m in _months_before(period)), Decimal(0))
    return ledger + _cutover_ytd(worker_id, period)


# ─── post_journal_entry (cross-module) — mirrors the close-handler ───

def post_journal_entry(payload: dict) -> str:
    """Invoke accounting's post_journal_entry; the entryId it assigned.

    Raises `journal.Refused` when accounting declines — an unknown account, an unbalanced
    entry, a non-positive amount. A parked (202, pending classification) entry is NOT a
    refusal; it comes back with its entryId like any other."""
    return journal.post(payload, POST_JOURNAL_ENTRY_FN)


def _is_number(s) -> bool:
    """True if `s` is a numeric literal (a firm-set cap), False if it names something (a wage base)."""
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


# ─── the run ───

def _run_one(worker_id: str, period: str) -> dict:
    gross = _wages_payable_credits(worker_id, period)
    if gross <= 0:
        return {"worker_id": worker_id, "period": period, "skipped": "no accrued wages"}

    as_of = _period_end(period)
    matched = instances.at(instances.PAY_RUN, worker_id)
    if not matched:
        return {"worker_id": worker_id, "period": period, "gross": gross,
                "skipped": "no rule instances match this worker"}

    # Layer each instance's param over the rule's PLATFORM values as-of the period: the brackets come
    # from the GENERAL rows the canonical seed maintains, the W-4 / DE-4 from the worker's own rows.
    # The instance is current config (no dating — a run period is frozen in the ledger); the as-of is
    # only for the platform values, where a new tax year appends and both years must coexist.
    #
    # A `cap` that NAMES a platform quantity (`"ss_wage_base"`) rather than a number is a wage base —
    # the law sets it, so it is resolved here from the platform store as-of the period, not baked on
    # the instance. A numeric cap (a firm's own ceiling) is a literal and passes through untouched. A
    # named cap the store doesn't know raises, rather than silently withholding with no ceiling.
    resolved = []
    for inst in matched:
        p = params.layered(inst["rule"], inst.get("param"), as_of)
        cap = p.get("cap")
        if isinstance(cap, str) and not _is_number(cap):
            base = params.quantity(cap, as_of)
            if base is None:
                raise RuntimeError(
                    f"the '{cap}' wage base is not in the platform store for {as_of} — "
                    "the rule-params seed hasn't landed. A wage base is not guessable.")
            p = {**p, "cap": base}
        resolved.append({**inst, "param": p})
    matched = resolved

    ctx = {"gross": gross, "ytd": {"gross_wages": _ytd_gross_wages(worker_id, period)},
           "worker_id": worker_id, "period": period}
    effects = rules.run_instances(ctx, matched, modules=RULE_LIBS)
    if not effects:
        return {"worker_id": worker_id, "period": period, "gross": gross,
                "skipped": "the matching instances produced nothing this period"}

    entry_id = f"payrun-{worker_id}-{period}"
    payload = {
        "lineItems": effects,
        "memo": f"payroll withholding {worker_id} {period} on gross {gross}",
        "source": entry_id,
        "dimensions": {"worker_id": worker_id, "period": period,
                       "location": _worker_location(worker_id, period)},
        # entryId + a period-stable timestamp together make the re-run no-op:
        # accounting dedups on (pk, sk) and sk = <timestamp>#<entryId>#<i>, so both
        # must be deterministic for the same (worker, period). Never double-withholds.
        "entryId": entry_id,
        "timestamp": _period_start_ms(period),
    }
    journal_entry_id = post_journal_entry(payload)
    return {
        "worker_id": worker_id,
        "period": period,
        "gross": gross,
        "instances": [i["name"] for i in matched],
        "journal_entry_id": journal_entry_id,
    }


def handler(event, context):
    """Agent-tool / cron entrypoint. event: {worker_id, period?}. period defaults
    to the current YYYY-MM."""
    body = event.get("body")
    if isinstance(body, str):
        event = json.loads(body)
    elif isinstance(body, dict):
        event = body

    worker_id = event.get("worker_id")
    if not worker_id:
        return {"statusCode": 400, "body": json.dumps({"error": "worker_id is required"})}
    period = event.get("period") or _current_period()

    result = _run_one(worker_id, period)
    return {"statusCode": 200, "body": json.dumps(result, cls=_DecimalEncoder)}
