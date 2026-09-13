"""fixture — the harness every demo fixture is built on.

A demo's claim is a claim about data, so the fixture is part of the demo, not setup for it. This
module gives each one the same three verbs, the same CLI, and the same safety properties:

    import fixture as fx
    fx.run(name="03 · labor", seed=seed, reset=reset, check=check)

    .venv/bin/python <demo dir>/seed.py            # reset, then seed, then check
    .venv/bin/python <demo dir>/seed.py --reset    # teardown only
    .venv/bin/python <demo dir>/seed.py --check    # is this demo recordable right now?

`--check` exits NONZERO when the demo's premise isn't in the data, so it can gate a recording
instead of being read and believed.


## the one rule: no side doors

Every fixture bug this tooling has produced was the same bug — the fixture touched state by a path
the system itself doesn't use, so the system couldn't see it and nothing could undo it:

  - Demo 01's exports were put in the cabinet with `aws s3 cp`. That writes the bytes but not the
    caption annotation, and `manage_storage op=find` matches on captions — so a 2.9 MB POS export
    with a `served_by` column sat in the bucket while the agent reported "no POS export on file"
    and dropped the demo's best beat.
  - Demo 03's teardown released bookings whose `source` began `schedule:`. The agent named its own
    source `schedule-foh-2026-07-27-2026-08-07`, so the reset matched nothing, printed a cheerful
    `released 0`, exited zero, and left all 38 reservations standing.
  - Demo 03's seed omitted `entry_id`, which `manage_labor put` then auto-generates — so seeding twice
    silently doubled the history and turned the shift lead's 5.4 days/week into 10.8.

So:

  **SEEDING writes through the same tools a real tenant uses.** `seed_dev.call(...)`,
  `fx.file_document(...)`. If the agent has to find it, it must be filed the way a person files it.

  **TEARDOWN deletes rows directly and then re-reads to prove it worked.** This is the deliberate
  asymmetry, and it mirrors `scripts/reset_dev.py` (which empties tables directly) against
  `scripts/seed_dev.py` (which invokes lambdas). A reset is not modelling anything a tenant does —
  it is returning to a known state — and going through the tools makes it guess at strings the
  AGENT chose. Delete on the key you control, then verify.

  **Never report success by counting calls.** Count what's left.


## scope, not history

A reset cannot work from a list of what the seed wrote, because the interesting writes are the
AGENT's: a take ends with bookings made, invoices drafted, documents filed. Those are invisible to
any seed-shaped bookkeeping.

Define the reset over the STATE SURFACE the demo touches instead — every time entry for these
workers, every movement on items under this prefix, every document under this path. The agent writes
into the same surface, so its writes come out for free.
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import seed_dev as s                                    # noqa: E402 — the lambda-invoke primitive

# The demo tenant. Hardcoded for the same reason `reset_dev.py` hardcodes its account — one place to
# reassign if demos ever move off gradienterp.
UPLOADS_BUCKET = "gerp-agent-gradienterp-uploads-867637277314"

_ddb = None


def table(name):
    """A DDB Table in the customer account. `name` is the bare suffix — `movements`, not the
    full `gerp-inventory-gradienterp-movements`."""
    global _ddb
    if _ddb is None:
        _ddb = s.session_for_account().resource("dynamodb")
    return _ddb.Table(name)


def _scan(tbl, keep):
    rows, kw = [], {}
    while True:
        page = tbl.scan(**kw)
        rows += [r for r in page.get("Items", []) if keep(r)]
        if not page.get("LastEvaluatedKey"):
            return rows
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def purge(table_name, key_attrs, keep, what):
    """Delete every row a fixture owns, then re-read to prove it. Returns the number deleted.

    `keep(row) -> bool` is the SCOPE — it must describe the state surface (an item prefix, a set of
    worker ids), never a value the agent chose. `key_attrs` is the table's key schema, e.g.
    `("item_id", "mv_sk")`."""
    tbl = table(table_name)
    rows = _scan(tbl, keep)
    with tbl.batch_writer() as batch:
        for r in rows:
            batch.delete_item(Key={k: r[k] for k in key_attrs})
    left = _scan(tbl, keep)
    if left:
        raise SystemExit(f"  FAILED to clear {what}: {len(left)} row(s) remain in {table_name}. "
                         f"The demo would record against dirty state — inspect before recording.")
    print(f"  cleared {len(rows):>4} {what}")
    return len(rows)


def fanout(fn, items, workers=8, what="call"):
    """Run `fn` over `items` concurrently, raising on the first failure.

    Seeding goes through the real lambdas, which is the point — and also means a few hundred round
    trips at ~200ms each. Concurrency keeps that honest write path from making the fixture something
    you avoid re-running. Kept modest: the aim is a fixture that finishes, not a load test."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        out = list(pool.map(fn, items))
    print(f"  wrote   {len(out):>4} {what}")
    return out


# ── the cabinet ───────────────────────────────────────────────────────────────────────────────────

def file_document(local_path, key, title, note="", tags=(), occurred_at=""):
    """Put a document in the cabinet the way a person does: upload the bytes, then CAPTION it
    through `manage_storage op=file`.

    The caption is the whole point. `op=find` lists by prefix and matches `query`/`tag` against the
    caption annotation, so an object written with a bare `aws s3 cp` is present, readable by exact
    key, and **invisible to every search the agent actually performs**."""
    body = Path(local_path).read_bytes()
    s.session_for_account().client("s3").put_object(
        Bucket=UPLOADS_BUCKET, Key=key, Body=body,
        ContentType="text/csv" if key.endswith(".csv") else "application/octet-stream")
    s.call("storage", "manage_storage", {
        "op": "file", "key": key, "title": title, "note": note,
        "tags": list(tags), **({"occurred_at": occurred_at} if occurred_at else {})})
    return key


def unfile(prefix, owns=()):
    """Remove every document under `prefix`, captions included, and prove the cabinet is empty there.

    Everything under the prefix goes, including documents the AGENT filed during a take — that is
    the scope and it is the point. But a prefix is a blast radius, so every key is PRINTED, and any
    key the fixture doesn't recognise (`owns`) is called out. A teardown that quietly destroys
    something nobody meant to seed is the same failure as one that quietly destroys nothing.

    Goes through `op=delete`, so a `retained` document (legal/PII) is refused rather than erased. A
    fixture must not be able to delete what the retention rule protects."""
    docs = s.call("storage", "manage_storage", {"op": "find", "prefix": prefix}).get("documents", [])
    owns, refused = set(owns), []
    for d in docs:
        mark = " " if not owns or d["key"].rsplit("/", 1)[-1] in owns else "  ← not this fixture's"
        print(f"    - {d['key']}{mark}")
        if s.call("storage", "manage_storage",
                  {"op": "delete", "key": d["key"]}).get("status") == "refused":
            refused.append(d["key"])
    if refused:
        raise SystemExit(f"  REFUSED to delete retained document(s): {refused}. Move them out of "
                         f"{prefix} by hand; a fixture will not destroy retained records.")
    left = s.call("storage", "manage_storage", {"op": "find", "prefix": prefix}).get("documents", [])
    if left:
        raise SystemExit(f"  FAILED to clear the cabinet under {prefix}: {len(left)} left.")
    print(f"  cleared {len(docs):>4} document(s) under {prefix}")
    return len(docs)


# ── capacity ──────────────────────────────────────────────────────────────────────────────────────

def release_capacity(item_prefix):
    """Return every capacity item under `item_prefix` to fully free.

    A capacity meter is `net = default − scheduled`, folded from an append-only log. After a take
    where the agent reserved a fortnight, the crew reads as fully booked; the next take then
    correctly reports nobody is free and the demo shows nothing on screen while erroring nowhere.

    Deletes the movement rows rather than appending cancels. Cancelling is the tenant-facing verb
    and needs the booking's `source`, which the AGENT names and names differently every take — that
    is the string this teardown must not depend on. Deleting also returns the log to exactly its
    seeded length instead of growing a ±1 pair per take."""
    return purge("gerp-inventory-gradienterp-movements", ("item_id", "mv_sk"),
                 lambda r: str(r.get("item_id", "")).startswith(item_prefix),
                 f"capacity movement(s) on {item_prefix}*")


def capacity_free(item_id, start_iso, end_iso):
    """(free_windows, utilization) — read back through the agent's own tool, so a check verifies
    what the agent will see rather than what the table contains."""
    r = s.call("inventory", "reserve",
               {"op": "availability", "item_id": item_id, "from": start_iso, "to": end_iso})
    return r.get("free") or [], float(r.get("utilization") or 0)


# ── labor ─────────────────────────────────────────────────────────────────────────────────────────

def clear_time_entries(worker_ids):
    """Every time entry for these workers. Keyed on the worker set — the scope — so entries the
    AGENT wrote during a take come out alongside the seeded ones."""
    ids = set(worker_ids)
    return purge("gerp-labor-gradienterp-time-entries", ("worker_id", "entry_id"),
                 lambda r: r.get("worker_id") in ids, "time entr(ies)")


# ── the check phase ───────────────────────────────────────────────────────────────────────────────

_failures = []


def require(ok, detail):
    """Assert a precondition the demo's STORY depends on. Collected, not raised, so one run reports
    every problem rather than the first."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {detail}")
    if not ok:
        _failures.append(detail)


def run(name, seed, reset, check):
    """The CLI. `--seed` is the default and ALWAYS resets first.

    Seeding on top of existing data is the mistake worth designing out: the writes are additive and
    nothing complains, so the fixture's own numbers — days per week, revenue, hours — silently
    double while every script still exits zero."""
    p = argparse.ArgumentParser(description=f"demo fixture: {name}")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--reset", action="store_true", help="tear down; write nothing")
    g.add_argument("--check", action="store_true", help="report whether the demo is recordable now")
    a = p.parse_args()

    print(f"\n{name}")
    if a.check:
        check()
    elif a.reset:
        reset()
    else:
        print("reset:")
        reset()
        print("seed:")
        seed()
        print("check:")
        check()

    if _failures:
        print(f"\nNOT RECORDABLE — {len(_failures)} precondition(s) failed:")
        for f in _failures:
            print(f"  - {f}")
        raise SystemExit(1)
    if a.check:
        print("\nrecordable")
    print()
