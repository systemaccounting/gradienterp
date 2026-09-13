"""export_gerp — write the firm's records out so they can be taken away.

Closing a gerp deletes it, and the gerp is where the books live. A firm has to keep its accounting
records for years, so "you may cancel anytime" is only true if leaving hands them back. This is what
hands them back.

**Deliberately long and explicit, one block per module, not a clever table scanner.** The repo is
public. Someone who knows warehousing should be able to read `_export_shipping` below, see that it
dumps raw while `_export_inventory` orders movements by date, and send a patch — without running
anything. A generic scanner would produce thirty raw dumps and give that reader nothing to disagree
with. It also throws away exactly the knowledge worth having: invoices joined to their lines, the
ledger as a real CSV an accountant can open.

    exports/<timestamp>/
      manifest.json              what ran, when, row counts per file
      README.md                  what each file is
      accounting/ledger.csv      flat, for a spreadsheet
      accounting/*.jsonl         ledger, balances, pending — the retention-obligation set
      invoicing/invoices.jsonl   each invoice with its lines attached
      contacts/  inventory/  …   one directory per module
      storage/…                  the documents, original keys

JSONL because it is lossless and greppable; the ledger ALSO as CSV because the person who most needs
an export is an accountant and an accountant opens a spreadsheet.

**An export with no arguments takes everything.** No selection is no restriction, and quietly
omitting part of someone's books is the one failure an export cannot have. `include` NARROWS it.

The tiers in `_TABLES` are advice for that narrowing, not a filter applied behind anyone's back.
BOOKS is what the firm must keep. EXHAUST is the platform's own operational residue — webhook logs,
dead letters, agent chat turns, the inbound event record — theirs too, but large, dull, and the
first thing to drop when someone wants a smaller copy. `tests/export` asserts every table in the
account has a verdict.

Why it exists: modules/export/README.md. How it is built: modules/export/AGENTS.md.

Event:
    {}                                         everything the firm owns — the usual case
    {"include": ["contacts", …]}               narrow to these tables only
    {"credentials_only": true}                 re-issue against the latest export, no new copy
    {"resume": "<export_id>"}                  continue a run that ran out of time

Returns 200 with credentials when every unit is done, 202 with a `resume` payload when not.
"""

import csv
import io
import json
import logging
import os
import time
import uuid
from decimal import Decimal

from aws import client as _aws, resource as _aws_resource
from aws import json_default as _json_default
from aws import log as alog

log = logging.getLogger()
log.setLevel(logging.INFO)

GERP = os.environ["CUSTOMER_ID"]
BUCKET = os.environ["STORAGE_BUCKET"]
PREFIX = os.environ.get("EXPORT_PREFIX", "exports")
# the role a customer's machine reads the export with. Assumed under a FIXED session name so the
# org trail can log data events for this identity alone (modules/export/AGENTS.md § auditable).
READER_ROLE_ARN = os.environ.get("EXPORT_READER_ROLE_ARN", "")
READER_SESSION = "gerp-export"
# One hour, not twelve: a Lambda runs AS a role, so assuming another role is role CHAINING, which
# STS caps at 3600s regardless of the role's MaxSessionDuration. Which is fine — `aws s3 sync`
# resumes, and the agent issues another when asked. Expiry is a sentence, not a support request.
READER_TTL = 3600
# the work list: one row per unit, stamped as each finishes. It is also the ANSWER to "what am I
# taking" — the plan is the scope, so there is no standing preference table beside it and nothing
# that can go stale between when it was set and when it runs. See § the job.
JOBS_TABLE = os.environ.get("EXPORT_JOBS_TABLE", "")

# ── the policy ──────────────────────────────────────────────────────────────
#
# Every table in the gerp, and what happens to it. A table has to be a DECISION: defaulting an
# unknown one to "include" grows the pile silently, defaulting it to "skip" loses someone's records
# silently. The suffix is what follows `<prefix>-<module>-<gerp>-`, or the bare module name where
# the table has no suffix.

# Every table gets a verdict. Three of these are ADVICE — everything the firm owns is exported
# unless someone narrows it — and only EXCLUDED changes what happens.
BOOKS = "books"              # what the firm created or must keep
EXHAUST = "exhaust"          # theirs too, but large and dull: the first thing to drop for a smaller copy
EXCLUDED = "excluded"        # not their data at all — never exported, narrowing or not
SPLIT = "split"              # some rows theirs, some not — needs a row filter, see _row_is_theirs

_TABLES = {
    # books — the retention-obligation set
    "accounting-ledger":       BOOKS,
    "accounting-balances":     BOOKS,
    "accounting-pending":      BOOKS,
    # what they sold and bought
    "invoicing-invoices":      BOOKS,
    "invoicing-invoice-lines": BOOKS,
    "invoicing-transitions":   BOOKS,
    "invoicing-agreements":    BOOKS,
    "purchasing-orders":       BOOKS,
    "agreements":              BOOKS,
    "treasury-agreements":     BOOKS,
    "shipping":                BOOKS,
    # who they deal with, what they hold, who worked
    "contacts":                BOOKS,
    "inventory-items":         BOOKS,
    "inventory-movements":     BOOKS,
    "labor-time-entries":      BOOKS,
    "labor-worker":            BOOKS,
    "labor-worker-legal":      BOOKS,
    # the rest of their own records
    "calendar-events":         BOOKS,
    "notes":                   BOOKS,
    "tasks":                   BOOKS,
    "assets":                  BOOKS,
    # how they run
    "settings":                BOOKS,
    "rules-instances":         BOOKS,

    # the platform's operational residue. Still theirs, and a dispute can turn on any of it — it is
    # just the first thing anyone would drop to make an export smaller.
    "payments-webhook-log":    EXHAUST,   # a dedup ledger of every webhook; the books hold the result
    "payments-dlq":            EXHAUST,   # failures
    "payments-dlq-bodies":     EXHAUST,   # raw provider payloads — where other people's PII concentrates
    "inbox-inbound":           EXHAUST,   # grows with what OTHERS send, most of it unactioned by design
    "agent-chats":             EXHAUST,   # turns; what mattered became a task, an invoice, a rule
    "agent-email":             EXHAUST,   # inbound-mail dedup for the agent's email door

    # not the customer's data
    "schema":                  EXCLUDED,     # the canonical registry, identical in every gerp
    "export-jobs":             EXCLUDED,     # this export's own work list — it changes while the
                                             # run reads it, so it cannot describe the run taking it

    # the only table whose rows differ in whose they are
    "rules-params":            SPLIT,        # pk=GENERAL is the law; pk=<contact_id> is the firm's
}


def _row_is_theirs(suffix, row):
    """The SPLIT filter. `rules-params` holds the legal requirements (bracket tables, wage bases,
    seeded from canonical and identical everywhere) under `pk = GENERAL`, and the firm's own
    employer-wide params under a contact_id. Only the second is theirs to take."""
    if suffix == "rules-params":
        return row.get("pk") != "GENERAL"
    return True


class _Encoder(json.JSONEncoder):
    """DynamoDB numbers arrive as Decimal, which json refuses. Integral values write as ints so a
    row count does not come back as 41.0."""

    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        if isinstance(o, (bytes, bytearray)):
            return o.decode(errors="replace")
        if isinstance(o, set):
            return sorted(o)
        return super().default(o)


# ── reading ─────────────────────────────────────────────────────────────────

def _table_name(suffix):
    """`accounting-ledger` → `gerp-accounting-<gerp>-ledger`; `contacts` → `gerp-contacts-<gerp>`.

    The gerp id sits in the MIDDLE of a deployed table name, after the module and before the rest,
    so the suffix has to be split rather than concatenated.
    """
    prefix = os.environ.get("STACK_PREFIX", "gerp")
    gerp = GERP.replace("_", "-")
    module, _, rest = suffix.partition("-")
    return f"{prefix}-{module}-{gerp}-{rest}" if rest else f"{prefix}-{module}-{gerp}"


MISSING = object()   # a table that is not in this gerp at all — distinct from one that is empty


def _scan(suffix):
    """Every row, or MISSING if the table does not exist.

    A gerp need not have every module deployed, and an export that dies because one table is absent
    is worse than one that reports what it found. Absent is NOT the same as empty, though, and the
    manifest records which — a file of zero rows says the firm has no contacts, and no file at all
    says nobody looked.
    """
    from botocore.exceptions import ClientError
    table = _aws_resource("dynamodb").Table(_table_name(suffix))
    rows, kwargs = [], {}
    while True:
        try:
            page = table.scan(**kwargs)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return MISSING
            raise
        rows.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return rows
        kwargs["ExclusiveStartKey"] = last


# ── writing ─────────────────────────────────────────────────────────────────

class _Out:
    """Collects files and their row counts, then writes them. The manifest is built from what was
    actually written, not from what was meant to be — an export nobody can check is not evidence."""

    def __init__(self, base):
        self.base = base
        self.files = {}
        self.missing = []      # tables this gerp does not have
        self.s3 = _aws("s3")

    def rows_or_note(self, suffix, rows):
        """MISSING → note it and answer None, so a caller can skip without inventing an empty file."""
        if rows is MISSING:
            self.missing.append(suffix)
            return None
        return rows

    def jsonl(self, path, rows):
        body = "".join(json.dumps(r, cls=_Encoder, sort_keys=True) + "\n" for r in rows)
        self._put(path, body.encode(), "application/x-ndjson")
        self.files[path] = len(rows)

    def text(self, path, body, content_type="text/plain"):
        self._put(path, body.encode(), content_type)

    def _put(self, path, body, content_type):
        self.s3.put_object(Bucket=BUCKET, Key=f"{self.base}/{path}", Body=body,
                           ContentType=content_type)


# ── the download scripts ────────────────────────────────────────────────────
#
# Static templates, not generated per export. They are code that runs on the customer's machine
# holding a credential: in a public repo someone can read them before running them and disagree in
# a PR, which a freshly-composed script cannot offer. Two substitutions, fixed structure.
#
# `sync` and not `cp`, because it resumes — it skips what is already on disk, so a credential
# expiring mid-download is a pause rather than a failure. Re-run after re-issuing and it continues.

_SH = """#!/usr/bin/env bash
# Download your gradientERP export.
#
#   bash download.sh [destination-directory]
#
# The credentials are IN this file — nothing to paste, nothing to configure. They read this one
# export prefix and nothing else, and they last an hour from when this was written (%(expires)s).
#
# Resumable: re-run it and it picks up where it stopped. If the credentials expire part-way, ask
# your gerp for a fresh link and run the new file — nothing already downloaded is fetched twice.
set -euo pipefail

export AWS_ACCESS_KEY_ID=%(key)s
export AWS_SECRET_ACCESS_KEY=%(secret)s
export AWS_SESSION_TOKEN=%(token)s

DEST="${1:-gradienterp-export}"
SRC="s3://%(bucket)s/%(base)s"

echo "export:      $SRC"
echo "destination: $DEST"
echo "files:       %(files)s  documents: %(documents)s"
echo

aws s3 sync "$SRC" "$DEST"

echo
echo "done — $DEST"
echo "start with README.md, or accounting/ledger.csv in a spreadsheet."
"""

_PS1 = """# Download your gradientERP export.
#
#   .\\download.ps1 [destination-directory]
#
# The credentials are IN this file — nothing to paste, nothing to configure. They read this one
# export prefix and nothing else, and they last an hour from when this was written (%(expires)s).
#
# Resumable: re-run it and it picks up where it stopped. If the credentials expire part-way, ask
# your gerp for a fresh link and run the new file — nothing already downloaded is fetched twice.
param([string]$Dest = "gradienterp-export")
$ErrorActionPreference = "Stop"

$env:AWS_ACCESS_KEY_ID = "%(key)s"
$env:AWS_SECRET_ACCESS_KEY = "%(secret)s"
$env:AWS_SESSION_TOKEN = "%(token)s"

$Src = "s3://%(bucket)s/%(base)s"

Write-Host "export:      $Src"
Write-Host "destination: $Dest"
Write-Host "files:       %(files)s  documents: %(documents)s"
Write-Host ""

aws s3 sync $Src $Dest

Write-Host ""
Write-Host "done - $Dest"
Write-Host "start with README.md, or accounting/ledger.csv in a spreadsheet."
"""


def _mint_reader_credentials():
    """An hour of read on this gerp's exports, under a fixed session name.

    Fixed because the org trail filters data events on `userIdentity.arn`, and an assumed-role arn
    is `…:assumed-role/<role>/<session>` — advanced event selectors offer no substring match, so a
    per-export session name would make the matcher inexpressible. Which export was downloaded comes
    from the object key on the event, not from the session.
    """
    if not READER_ROLE_ARN:
        return None
    creds = _aws("sts").assume_role(
        RoleArn=READER_ROLE_ARN, RoleSessionName=READER_SESSION, DurationSeconds=READER_TTL,
    )["Credentials"]
    return {
        "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
        "AWS_SESSION_TOKEN": creds["SessionToken"],
        "expires_at": creds["Expiration"].isoformat(),
    }


def _write_scripts(out, base, files, documents, creds):
    """The scripts, with the credentials baked in, and a presigned link to each.

    The credentials live IN the file rather than being pasted into a conversation: the chat then
    carries a link instead of a secret, and the person runs one thing with nothing to configure.

    Presigned so fetching the script needs no credentials of its own — otherwise reaching the
    download instructions would require the credentials the instructions exist to deliver.

    Signed with the READER credentials, not the lambda's: a presigned URL cannot outlive the
    credential that signed it, and the reader's hour is exactly the life the script's own contents
    have. Signing with the lambda's session would produce a link that expires on its own schedule.
    """
    fill = {"bucket": BUCKET, "base": base, "files": files, "documents": documents,
            "key": (creds or {}).get("AWS_ACCESS_KEY_ID", ""),
            "secret": (creds or {}).get("AWS_SECRET_ACCESS_KEY", ""),
            "token": (creds or {}).get("AWS_SESSION_TOKEN", ""),
            "expires": (creds or {}).get("expires_at", "")}
    out.text("download.sh", _SH % fill, "text/x-shellscript")
    out.text("download.ps1", _PS1 % fill, "text/plain")
    return {name: _presign(f"{base}/{name}", creds) for name in ("download.sh", "download.ps1")}


def _presign(key, creds):
    """A link anyone holding it can fetch, valid as long as the credentials inside the file are.

    SigV4 is forced: the bucket is SSE-KMS and boto3's default signing produces a URL S3 rejects
    with InvalidArgument — a failure that only shows up when someone clicks the link.
    """
    if not creds:
        return None
    import boto3
    from botocore.config import Config
    session = boto3.Session(
        aws_access_key_id=creds["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=creds["AWS_SECRET_ACCESS_KEY"],
        aws_session_token=creds["AWS_SESSION_TOKEN"],
    )
    s3 = session.client("s3", config=Config(signature_version="s3v4"))
    return s3.generate_presigned_url("get_object",
                                     Params={"Bucket": BUCKET, "Key": key},
                                     ExpiresIn=READER_TTL)


def _latest_export():
    """The most recent COMPLETE export, for re-issuing credentials without re-exporting.

    Completeness is the manifest, which is written last. A run that times out mid-way leaves table
    files and no manifest — and handing someone credentials to that is worse than telling them
    there is no export, because it looks like their books and is missing whatever came after the
    minute it died. An unfinished prefix is invisible here.
    """
    from botocore.exceptions import ClientError
    s3 = _aws("s3")
    page = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{PREFIX}/", Delimiter="/")
    for prefix in sorted((p["Prefix"].rstrip("/") for p in page.get("CommonPrefixes", [])),
                         reverse=True):
        try:
            s3.head_object(Bucket=BUCKET, Key=f"{prefix}/manifest.json")
            return prefix
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                continue   # no manifest means the run did not finish
            alog.error("manifest head failed", prefix=prefix, error=str(e))
            raise
    return None


# ── one writer per shape ────────────────────────────────────────────────────
#
# Long on purpose. Each knows its own shape, and that knowledge is the whole reason not to write a
# generic scanner. Each is also a UNIT of work — addressable by name, run independently, resumable.

def _write_ledger(out, suffix):
    rows = _scan(suffix)
    if rows is MISSING:
        return None
    out.jsonl("accounting/ledger.jsonl", rows)
    _ledger_csv(out, rows)
    return len(rows)


def _ledger_csv(out, rows):
    """The ledger, flat, one row per LEG — which is what a general ledger looks like and what a
    spreadsheet expects. The stored row carries BOTH sides (`debit_account` + `credit_account`), so
    each expands into a debit line and a credit line.

    The person who most needs an export is an accountant, and an accountant opens a spreadsheet, not
    thirty JSONL files. Anything a row carries beyond these columns is in the .jsonl beside it.
    """
    cols = ["date", "entry_id", "account", "account_type", "debit", "credit", "memo", "source",
            "location"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in sorted(rows, key=lambda x: str(x.get("sk") or "")):
        base = {
            "date": _date(r.get("timestamp_ms")),
            "entry_id": _flat(r.get("entry_id")),
            "memo": _flat(r.get("memo")),
            "source": _flat(r.get("source")),
            "location": _flat((r.get("dimensions") or {}).get("location")),
        }
        amount = _flat(r.get("amount"))
        if r.get("debit_account"):
            w.writerow({**base, "account": r["debit_account"],
                        "account_type": _flat(r.get("debit_account_type")),
                        "debit": amount, "credit": ""})
        if r.get("credit_account"):
            w.writerow({**base, "account": r["credit_account"],
                        "account_type": _flat(r.get("credit_account_type")),
                        "debit": "", "credit": amount})
    out.text("accounting/ledger.csv", buf.getvalue(), "text/csv")


def _date(ms):
    """Epoch millis → a date a spreadsheet parses. Blank rather than a guess when absent."""
    if ms in (None, ""):
        return ""
    return time.strftime("%Y-%m-%d", time.gmtime(int(ms) / 1000))


def _flat(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, cls=_Encoder, sort_keys=True)
    return "" if v is None else v


def _write_invoices(out, suffix):
    """Invoices with their lines attached. The lines live in their own table keyed by invoice_id —
    correct for querying, useless to a human handed two files to rejoin by hand.

    This unit consumes BOTH tables, which is why `invoicing-invoice-lines` is never planned on its
    own (`_CONSUMED_BY`) — two units writing one file would race, and the join needs both anyway.
    """
    invoices = _scan("invoicing-invoices")
    if invoices is MISSING:
        return None
    lines = _scan("invoicing-invoice-lines")
    by_invoice = {}
    for ln in (lines if lines is not MISSING else []):
        by_invoice.setdefault(ln.get("invoice_id"), []).append(ln)
    for inv in invoices:
        inv["lines"] = sorted(by_invoice.get(inv.get("invoice_id"), []),
                              key=lambda l: str(l.get("sk", "")))
    out.jsonl("invoicing/invoices.jsonl", invoices)
    return len(invoices)


def _write_movements(out, suffix):
    """Date order: the movement log IS the stock history, and out of order it is a pile."""
    rows = _scan(suffix)
    if rows is MISSING:
        return None
    out.jsonl("inventory/movements.jsonl", sorted(rows, key=lambda r: str(r.get("mv_sk") or "")))
    return len(rows)


def _write_plain(out, suffix):
    """Tables with nothing to say about their own shape. Most of them."""
    rows = _scan(suffix)
    if rows is MISSING:
        return None
    kept = [r for r in rows if _row_is_theirs(suffix, r)]
    out.jsonl(_PATHS[suffix], kept)
    return len(kept)


# where each table lands. One line each; the exceptions above are the ones with something to say.
_PATHS = {
    "accounting-ledger":       "accounting/ledger.jsonl",
    "accounting-balances":     "accounting/balances.jsonl",
    "accounting-pending":      "accounting/pending.jsonl",
    "invoicing-invoices":      "invoicing/invoices.jsonl",
    "invoicing-transitions":   "invoicing/transitions.jsonl",
    "invoicing-agreements":    "invoicing/agreements.jsonl",
    "purchasing-orders":       "purchasing/orders.jsonl",
    "agreements":              "agreements/agreements.jsonl",
    "treasury-agreements":     "treasury/agreements.jsonl",
    "shipping":                "shipping/shipping.jsonl",
    "contacts":                "contacts/contacts.jsonl",
    "inventory-items":         "inventory/items.jsonl",
    "inventory-movements":     "inventory/movements.jsonl",
    "labor-time-entries":      "labor/time-entries.jsonl",
    "labor-worker":            "labor/workers.jsonl",
    "labor-worker-legal":      "labor/worker-legal.jsonl",
    "calendar-events":         "calendar/events.jsonl",
    "notes":                   "notes/notes.jsonl",
    "tasks":                   "tasks/tasks.jsonl",
    "assets":                  "assets/assets.jsonl",
    "settings":                "settings/settings.jsonl",
    "rules-instances":         "rules/instances.jsonl",
    "rules-params":            "rules/params.jsonl",
    "payments-webhook-log":    "payments/webhook-log.jsonl",
    "payments-dlq":            "payments/dlq.jsonl",
    "payments-dlq-bodies":     "payments/dlq-bodies.jsonl",
    "inbox-inbound":           "inbox/inbound.jsonl",
    "agent-chats":             "agent/chats.jsonl",
    "agent-email":             "agent/email.jsonl",
}

_WRITERS = {
    "accounting-ledger":   _write_ledger,
    "invoicing-invoices":  _write_invoices,
    "inventory-movements": _write_movements,
}

# a table another unit already reads — never planned on its own
_CONSUMED_BY = {"invoicing-invoice-lines": "invoicing-invoices"}


def _copy_documents(out, keys):
    """A batch of the filing cabinet, copied key-for-key.

    Parallel because this is the throughput wall: one API call per document at ~50ms is ~20/sec.
    Batched into units so a firm with 200k receipts is many short invocations rather than one that
    cannot finish.
    """
    from concurrent.futures import ThreadPoolExecutor
    s3 = _aws("s3")

    def _copy(key):
        s3.copy_object(Bucket=BUCKET, Key=f"{out.base}/storage/{key}",
                       CopySource={"Bucket": BUCKET, "Key": key})

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(_copy, keys))
    return len(keys)


# AgentCore writes this at browser creation to prove it can reach the recordings prefix — the
# service's probe, not a document, and without this line the first export of every gerp carries it
_SERVICE_PROBES = {"browse-recordings/BrowserRecordingTestFile"}


def _document_keys():
    """Every document, skipping our own prefix — or export two contains export one and export
    three contains both — and the service's probe objects."""
    s3 = _aws("s3")
    keys, token = [], None
    while True:
        kwargs = {"Bucket": BUCKET}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        keys.extend(o["Key"] for o in page.get("Contents", [])
                    if not o["Key"].startswith(f"{PREFIX}/") and o["Key"] not in _SERVICE_PROBES)
        token = page.get("NextContinuationToken")
        if not token:
            return keys


# ── the job: planned, stamped, resumable ────────────────────────────────────
#
# Lambda caps at 900s and a large gerp will exceed it. The answer is not a bigger timeout — there
# isn't one — it is to stop treating the export as one indivisible act.
#
# PLAN before executing: one row per unit of work, written under the export id before anything
# moves. That list is also the answer to "how big is this", available before the first byte rather
# than discovered by dying.
#
# STAMP DONE, never started. A row with `done_at` is finished; anything else is pending, and it does
# not matter whether it never ran, is running now, or died halfway through a write. No leases, no
# locks, no distinguishing crashed from slow — resume asks one question, and redoing a unit that was
# secretly nearly finished costs one unit.
#
# The manifest is written when zero rows are unstamped, which makes it an honest completion signal
# rather than a proxy for one.

DOC_BATCH = int(os.environ.get("EXPORT_DOC_BATCH", "200"))
# stop with time to spare: the manifest, the scripts and the stamp of the last unit all have to
# land, and a unit killed mid-write is a unit redone.
RESERVE_MS = int(os.environ.get("EXPORT_RESERVE_MS", "60000"))


def _jobs():
    return _aws_resource("dynamodb").Table(JOBS_TABLE)


def _plan(export_id, want):
    """One row per unit. Tables another unit already reads are not planned separately."""
    units = [f"table#{s}" for s in sorted(want) if s not in _CONSUMED_BY]
    keys = _document_keys()
    batches = [keys[i:i + DOC_BATCH] for i in range(0, len(keys), DOC_BATCH)]
    table = _jobs()
    with table.batch_writer() as batch:
        for unit in units:
            batch.put_item(Item={"export_id": export_id, "unit": unit})
        for n, chunk in enumerate(batches):
            batch.put_item(Item={"export_id": export_id, "unit": f"docs#{n:05d}",
                                 "keys": chunk})
    return len(units) + len(batches)


def _narrowing(counts):
    """The tables this export took, or None if it took everything.

    Read off the plan, and compared against what a FULL plan would have produced — which is not
    every entry in `_TABLES`, since a table another unit already reads (`_CONSUMED_BY`) is never
    planned on its own. Comparing against the raw policy marks every complete export as narrowed.
    """
    took = sorted(n for u in counts for k, _, n in [u.partition("#")] if k == "table")
    full = {s for s, v in _TABLES.items() if v != EXCLUDED and s not in _CONSUMED_BY}
    return took if len(took) < len(full) else None


def _units(export_id):
    """Every planned unit for this export, done and pending alike."""
    from boto3.dynamodb.conditions import Key
    out, kwargs = [], {"KeyConditionExpression": Key("export_id").eq(export_id)}
    table = _jobs()
    while True:
        page = table.query(**kwargs)
        out.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _stamp(export_id, unit, count):
    """Done, with what it wrote. `count=None` records a table this gerp does not have — absent is
    not empty, and the manifest has to be able to say which."""
    _jobs().update_item(
        Key={"export_id": export_id, "unit": unit},
        UpdateExpression="SET done_at = :t, row_count = :c",
        ExpressionAttributeValues={":t": int(time.time() * 1000),
                                   ":c": -1 if count is None else count},
    )


def _run_unit(out, row):
    """One unit. Returns the row count, or None for a table this gerp does not have.

    **A redone unit overwrites, and that is only safe because every write here is a WHOLE object.**
    `out.jsonl`/`out.text` are one `put_object`; documents are one `copy_object` each. S3 has no
    partial object, so a unit killed mid-flight left either nothing or a complete file, and redoing
    it writes identical bytes. Any writer that ever appends, or builds an object in pieces, breaks
    resume — quietly, by leaving a file that is neither the old one nor the new one.

    What is NOT idempotent is the data underneath. Units finished in the first pass read the tables
    as they were then; units finished on resume read them as they are later. A contact deleted
    between the two leaves an export that was never true at any single moment. Small, and mostly
    theoretical for a closure export where nobody is still transacting — but a resumed export is
    not a point-in-time snapshot, and should not be described as one.
    """
    unit = row["unit"]
    kind, _, name = unit.partition("#")
    if kind == "docs":
        return _copy_documents(out, [str(k) for k in row.get("keys") or []])
    writer = _WRITERS.get(name, _write_plain)
    return writer(out, name)


def _remaining_ms(context):
    try:
        return context.get_remaining_time_in_millis()
    except Exception:  # noqa: BLE001 — no context locally; nothing is about to time out
        return 10 ** 9


# ── the run ─────────────────────────────────────────────────────────────────

def _wanted(event):
    """Which tables this run covers, at PLAN time.

    Everything that is theirs, unless `include` names a subset — then just those. No selection is
    no restriction, so the default is the whole firm and asking for less is the deliberate act.
    EXCLUDED is never theirs to take and cannot be named into the set.

    BOOKS / EXHAUST are advice for someone narrowing, not a filter this applies.

    There is no standing preference table. The plan rows ARE the scope, decided per export and
    visible before anything runs — so a choice cannot go stale between being set and being used,
    which is the failure that a saved "skip the ledger" would cause on the one export that is
    nobody's second chance. An agent showing someone what will be taken is showing them these rows.
    """
    everything = {s for s, v in _TABLES.items() if v != EXCLUDED}
    asked = [n for n in (event.get("include") or [])]
    unknown = sorted(n for n in asked if n not in everything)
    if unknown:
        # a name this export does not know is refused, never dropped: "ledger" for
        # "accounting-ledger" would otherwise hand someone a copy of their books with the books
        # left out, and the manifest would say so only to a reader who checked. The caller gets
        # the names it can use and asks again.
        raise _Refused({"error": f"unknown table(s) in include: {', '.join(unknown)}",
                        "tables": sorted(everything)})
    return set(asked) or everything


class _Refused(Exception):
    def __init__(self, body):
        super().__init__(body["error"])
        self.body = body


def handler(event, context):
    try:
        return _handle(event, context)
    except _Refused as e:
        return {"statusCode": 400, "body": json.dumps(e.body)}
    except Exception as e:  # noqa: BLE001
        alog.exception("export failed", error=str(e))
        return {"statusCode": 502, "body": json.dumps({"error": f"export failed: {e}"})}


def _handle(event, context):
    event = event if isinstance(event, dict) else {}
    if isinstance(event.get("body"), str):
        event = json.loads(event["body"])

    # "that link stopped working" and "get me my data" are the same request to whoever is asking,
    # so they are the same tool. This half re-issues against the export that already exists rather
    # than making another copy of the books to hand over the same thing.
    if event.get("credentials_only"):
        base = _latest_export()
        if not base:
            return {"statusCode": 404, "body": json.dumps(
                {"error": "no export yet — run one first"})}
        # this branch exists because a credential expired mid-download, so the SCRIPTS are
        # rewritten with fresh ones rather than a bare credential being handed over. The export
        # itself is untouched — no second copy of the books to deliver the same thing.
        creds = _mint_reader_credentials()
        manifest = json.loads(_aws("s3").get_object(
            Bucket=BUCKET, Key=f"{base}/manifest.json")["Body"].read() or b"{}")
        links = _write_scripts(_Out(base), base, len(manifest.get("tables") or {}),
                               manifest.get("documents", 0), creds)
        return {"statusCode": 200, "body": json.dumps({
            "export": f"s3://{BUCKET}/{base}",
            "download": links,
            "expires_at": (creds or {}).get("expires_at"),
        }, default=_json_default)}

    # resume, or start. An export id IS its prefix, so resuming needs nothing but that.
    export_id = (event.get("resume") or "").strip()
    if export_id:
        units = _units(export_id)
        if not units:
            return {"statusCode": 404, "body": json.dumps(
                {"error": f"no planned export {export_id}"})}
    else:
        started = int(time.time() * 1000)
        # timestamp so exports sort and read as dates, plus a token so two in the same second are
        # two exports. Sharing an id means sharing a prefix AND a plan: the second run reads the
        # first's job rows, finds them stamped, and reports someone else's work as its own.
        export_id = "{}-{}".format(
            time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime(started / 1000)),
            uuid.uuid4().hex[:6],
        )
        planned = _plan(export_id, _wanted(event))
        units = _units(export_id)
        log.info(f"planned {planned} units for {export_id}")

    base = f"{PREFIX}/{export_id}"
    out = _Out(base)

    # do what fits, stamping as it goes. Stopping early is a normal outcome, not a failure —
    # the caller resumes and the work already stamped is not redone.
    pending = [u for u in units if not u.get("done_at")]
    ran = 0
    for row in pending:
        if _remaining_ms(context) < RESERVE_MS:
            break
        try:
            count = _run_unit(out, row)
        except Exception as e:  # noqa: BLE001
            alog.error("export unit failed", export_id=export_id, unit=row["unit"], error=str(e))
            raise
        _stamp(export_id, row["unit"], count)
        ran += 1

    units = _units(export_id)
    left = [u for u in units if not u.get("done_at")]
    if left:
        # deliberately no credentials: there is nothing complete to hand anyone yet
        return {"statusCode": 202, "body": json.dumps({
            "export": f"s3://{BUCKET}/{base}",
            "export_id": export_id,
            "status": "incomplete",
            "units_done": len(units) - len(left),
            "units_left": len(left),
            "resume": {"resume": export_id},
        })}

    # every unit is stamped, so the manifest is a fact rather than a proxy for one
    counts = {u["unit"]: int(u.get("row_count", 0)) for u in units}
    tables = {}
    missing = []
    documents = 0
    for unit, count in counts.items():
        kind, _, name = unit.partition("#")
        if kind == "docs":
            documents += max(count, 0)
        elif count < 0:
            missing.append(name)
        else:
            tables[_PATHS.get(name, f"{name}.jsonl")] = count

    manifest = {
        "gerp_id": GERP,
        "exported_at_iso": export_id,
        "tables": dict(sorted(tables.items())),
        "documents": documents,
        "units": len(units),
        # the plan rows ARE the scope, so what was taken is read back off them rather than
        # re-derived from the event — which a resumed run no longer has.
        "narrowed_to": _narrowing(counts),
        # not in this gerp at all — a module that is not deployed. Recorded because "no file"
        # and "a file with no rows" mean different things and only one of them is an answer.
        "not_present": sorted(missing),
        "excluded": sorted(s for s, v in _TABLES.items() if v == EXCLUDED),
    }
    out.text("manifest.json", json.dumps(manifest, indent=2) + "\n", "application/json")
    out.text("README.md", _readme(manifest), "text/markdown")

    # each export carries its own scripts, pinned to its own prefix — self-identifying, so which
    # export a script pulls is never a question
    creds = _mint_reader_credentials()
    links = _write_scripts(out, base, len(tables), documents, creds)

    return {"statusCode": 200, "body": json.dumps({
        "export": f"s3://{BUCKET}/{base}",
        "export_id": export_id,
        "status": "complete",
        "files": len(tables),
        "rows": sum(tables.values()),
        "documents": documents,
        "units": len(units),
        # LINKS, not credentials. The credentials are inside the script the link fetches, so a
        # conversation carries a URL rather than a secret and the person runs one thing with
        # nothing to paste. Both expire with the credentials they contain.
        "download": links,
        "expires_at": (creds or {}).get("expires_at"),
    }, default=_json_default)}


def _readme(manifest):
    lines = [
        f"# {manifest['gerp_id']} — export {manifest['exported_at_iso']}",
        "",
        "Your records, as they stood when this ran. Every `.jsonl` file is one row per line,",
        "exactly as stored. `accounting/ledger.csv` is the same ledger flattened for a spreadsheet.",
        "`storage/` holds your documents under their original names.",
        "",
        "## what is here",
        "",
    ]
    for path, count in manifest["tables"].items():
        lines.append(f"- `{path}` — {count} rows")
    lines += ["", f"- `storage/` — {manifest['documents']} documents", ""]
    if manifest.get("narrowed_to"):
        lines += [
            "## this export was narrowed",
            "",
            "Someone asked for these tables specifically, so the rest of your records are NOT in",
            "this copy. Ask for an export with nothing named and you get all of it.",
            "",
            *(f"- {t}" for t in manifest["narrowed_to"]),
            "",
        ]
    lines += [
        "## what is never here",
        "",
        "`schema` — gradientERP's field definitions, identical in every instance, and none of it",
        "yours. It is described in this file instead.",
        "",
        "Rows of `rules-params` keyed `GENERAL` — tax bracket tables and wage bases, what the law",
        "sets rather than what you did. Your own rule params are included.",
        "",
    ]
    return "\n".join(lines)
