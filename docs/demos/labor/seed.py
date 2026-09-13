"""demo 03 fixture — the FOH shift history and crew availability the agent measures before it acts.

    .venv/bin/python docs/demos/labor/seed.py            # reset, seed, check
    .venv/bin/python docs/demos/labor/seed.py --reset    # teardown only
    .venv/bin/python docs/demos/labor/seed.py --check    # is this demo recordable right now?

The verbs, the safety properties and the no-side-doors rule are `docs/demos/fixture.py`.

WHY THIS IS HERE AND NOT IN scripts/. `scripts/seed_dev.py` + `reset_dev.py` are the GENERAL toolkit
and their job is supporting integration tests. Demo fixtures aren't used by any test today, so they
live with the demo that needs them. This file borrows `seed_dev.call`, the lambda-invoke primitive,
which is the genuinely reusable half.

WHAT IT FIXES. Without it the labor table is empty, so "schedule the FOH for the next 2 weeks" has
no history to reason from. In the first take the agent went looking, found demo 01's
`labor-hours-2026-ytd.csv` in the cabinet, and scheduled off that — producing a roster around a
worker who did not exist in the ERP and mixing the BOH baker into a FOH schedule.

Those two datasets are different things and stay different:

  demo 01's CSVs     third-party exports a cafe RECEIVES — POS vendor, delivery app, payroll. They
                     legitimately disagree with the books; that's the point, they carry planted
                     findings.
  this file          the ERP's OWN records. What the business knows about its own staff.

Demo 03's claim is that the agent measures the books before it acts, so the history has to be IN the
books. It reads demo 01's POS export as a SOURCE (who rang which sale) — see `check`.

THE VARIATION IS DELIBERATE. A flat history makes "schedule the FOH" clerical and the demo says
nothing. These five have distinguishable records, so a scheduler that reads them can justify itself:

  ava-reyes    the rush-hour producer  — opens, works the 6-2 peak, highest sales-per-hour
  ben-osei     steady mid-shifts       — reliable, lower peak throughput
  deo-park     the shift lead          — genuinely runs ~5.4 days/wk, the number the agent should
                                         notice if a proposal pushes him past it
  cora-vance   BOH baker               — early bakes, NOT front of house; a FOH schedule that
                                         includes her is a mistake the fixture can catch
  mira-kwon    the second strong earner — mids and weekends; demo 01's POS export attributes ~30% of
                                         sales to her, so her history has to exist or the two fixtures
                                         contradict each other (see NOTE below)

NOTE — THE TWO FIXTURES HAVE TO AGREE. Demo 01's POS export carries `served_by`, and demo 03's agent
reads it to rank the crew by revenue per hour. Mira was originally seeded with NO shift history as a
"new hire" while that export credited her with $61k of sales, and the agent — correctly — stopped to
flag the contradiction instead of scheduling. That is a good answer to a question this demo is not
asking. The demo's claim is that an agent schedules the front of house and hours of managerial work
disappear; anything that makes it pause is a fixture bug, not a beat.
"""
import datetime as dt
import random
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # docs/demos — the shared harness
import fixture as fx                            # noqa: E402 — the shared harness (§ no side doors)

LA = ZoneInfo("America/Los_Angeles")
WEEKS = 10
SHIFT_PREFIX = "1#shift-"                       # the capacity items; also the teardown's scope

# History has to run up to the day of the take. Pinning a date means a re-record weeks later seeds a
# crew whose last shift was a month ago, and the agent — correctly — reasons about a stale pattern or
# says it has nothing recent. That reads as a bad answer and is a bad fixture.
END = dt.datetime.now(LA).date()
START = END - dt.timedelta(weeks=WEEKS)

# worker → (role, days per week, usual shift, note)
CREW = {
    "ava-reyes":  ("barista",    5.0, (6, 14),  "opens; works the rush"),
    "ben-osei":   ("barista",    4.5, (10, 18), "mid + close"),
    "deo-park":   ("shift_lead", 5.4, (7, 15),  "lead; the highest pace on the crew"),
    "cora-vance": ("baker",      5.0, (4, 12),  "BOH bake — not front of house"),
    "mira-kwon":  ("barista",    4.0, (10, 18), "mids + weekends; the other strong earner"),
}

# The PEOPLE, not just their shifts. The contact registry carries `first_name`/`last_name` (person)
# and `role`/`hourly_rate` (employee) and this fixture never set them, so every take opened with the
# agent reporting "no roles on the contacts" and then inferring who works the floor from shift hours —
# a narration, a hedge, and a guess, all for a fact the schema has a field for. "Who works the floor —
# the roles say it" is only true if something says them.
PEOPLE = {                                       # worker → (first, last, hourly rate)
    "ava-reyes":  ("Ava",  "Reyes", 19.00),
    "ben-osei":   ("Ben",  "Osei",  18.00),
    "deo-park":   ("Deo",  "Park",  24.00),
    "cora-vance": ("Cora", "Vance", 22.00),
    "mira-kwon":  ("Mira", "Kwon",  18.50),
}
EVERYONE = list(CREW)

# A person's time is CAPACITY INVENTORY — the same model as a room or a bay: an item with an
# `availability_rule` whose meter is `net = default − scheduled`. Without these nothing says when
# someone is WILLING to work and a scheduler has only the historical pattern, which is how an early
# take put the shift lead on all fourteen days. A pattern is not consent.
#
# BYHOUR is the LOCAL hour. The capacity engine expands the recurrence on the business's own calendar
# (`from_rrule(..., zone=clock.zone())`), so "available 7am" is 7am where the cafe is, every week of
# the year — the instant moves across a daylight-saving change and the wall clock does not.
AVAILABILITY = {                                # worker → (BYDAY, local start hour, hours)
    "ava-reyes":  ("MO,TU,WE,TH,FR", 6, 8),
    "ben-osei":   ("WE,TH,FR,SA,SU", 10, 8),
    "deo-park":   ("SA,SU,MO,TU,WE", 7, 8),     # a lead who opens most days, off Thu/Fri
    "cora-vance": ("MO,TU,WE,TH,FR", 4, 8),
    "mira-kwon":  ("TH,FR,SA,SU", 10, 8),
}


def _ms(d):
    return int(d.timestamp() * 1000)


def _shifts():
    """[(worker_id, role, start, end)] across the window, at each worker's own pace.

    Seeded per worker rather than globally so adding or reordering CREW doesn't reshuffle everyone
    else's history — the numbers in `check` stay put across edits to this file."""
    out = []
    for wid, (role, per_week, (sh, eh), _note) in CREW.items():
        rng = random.Random(f"{wid}:{WEEKS}")
        d = START
        while d < END:
            n = int(per_week) + (1 if rng.random() < per_week % 1 else 0)
            for off in sorted(rng.sample(range(7), n)):
                day = d + dt.timedelta(days=off)
                if day >= END:
                    continue
                jitter = max(0, rng.choice([0, 0, 0, -1, 1]))       # the odd early/late shift
                out.append((wid, role,
                            dt.datetime(day.year, day.month, day.day, sh + jitter, 0, tzinfo=LA),
                            dt.datetime(day.year, day.month, day.day, eh + jitter, 0, tzinfo=LA)))
            d += dt.timedelta(days=7)
    return sorted(out, key=lambda r: r[2])


# ── seed ──────────────────────────────────────────────────────────────────────────────────────────

def _availability():
    for wid, (days, hour, hours) in AVAILABILITY.items():
        try:
            fx.s.call("inventory", "manage_stock", {
                "op": "create_item",
                "item_id": f"{SHIFT_PREFIX}{wid}",
                "name": f"{wid} shift availability",
                "unit": "shift", "unit_cost": 0,
                "availability_rule": f"FREQ=WEEKLY;BYDAY={days};BYHOUR={hour};BYMINUTE=0;BYSECOND=0",
                "availability_duration": hours * 3600,
            })
        except SystemExit as e:
            if "409" not in str(e):               # already there is fine; anything else is not
                raise
    print(f"  wrote   {len(AVAILABILITY):>4} capacity item(s)")


def _put_shift(row):
    wid, role, a, b = row
    return fx.s.call("labor", "manage_labor", {
        "op": "put",
        "entity": "time_entry", "worker_id": wid, "role": role,
        # DETERMINISTIC id. Omit it and the labor put composes `<started_at>#<location>#<uuid>`, so a
        # second seed appends a parallel history instead of overwriting — 198 entries become 396,
        # the lead's 5.4 days/wk reads 10.8, and nothing anywhere reports a problem. `run()` resets
        # before every seed, and this makes that belt-and-braces rather than the only guard.
        "entry_id": f"{_ms(a)}#1#{wid}",
        "started_at": _ms(a), "ended_at": _ms(b), "status": "closed",
    })


def _people():
    """Contact + labor worker rows, so roles and names are STATED rather than inferred."""
    for wid, (first, last, rate) in PEOPLE.items():
        role = CREW[wid][0]
        fx.s.call("contacts", "manage_contacts", {
            "op": "put",
            "contact_id": wid, "entity_type": "person", "is_employee": True,
            "first_name": first, "last_name": last,
            "role": role, "hourly_rate": rate, "employment_status": "active",
        })
        fx.s.call("labor", "manage_labor", {"op": "put", "entity": "worker", "contact_id": wid,
                                         "role": role, "rate": rate, "classification": "W-2"})
    print(f"  wrote   {len(PEOPLE):>4} contact + worker record(s)")


def seed():
    _people()
    _availability()
    fx.fanout(_put_shift, _shifts(), what=f"time entr(ies) over {WEEKS} weeks ({START} → {END})")


# ── reset ─────────────────────────────────────────────────────────────────────────────────────────

def reset():
    """Scoped to the state surface, so the AGENT's writes come out with the seed's.

    A take ends with the crew's fortnight reserved — the demo's own claim is that `reserve` makes the
    schedule real. Those movements are the agent's, under sources it named itself, and no
    seed-shaped bookkeeping knows about them."""
    fx.release_capacity(SHIFT_PREFIX)
    fx.clear_time_entries(EVERYONE)


# ── check ─────────────────────────────────────────────────────────────────────────────────────────

def check():
    now = dt.datetime.now(LA)
    fortnight = now + dt.timedelta(days=14)

    entries = []
    for wid in EVERYONE:
        entries += fx.s.call("labor", "manage_labor",
                             {"op": "query", "entity": "time_entry", "worker_id": wid, "limit": 500}).get("items", [])

    days, hours = {}, {}
    for r in entries:
        a = dt.datetime.fromtimestamp(int(r["started_at"]) / 1000, dt.timezone.utc).astimezone(LA)
        b = dt.datetime.fromtimestamp(int(r["ended_at"]) / 1000, dt.timezone.utc).astimezone(LA)
        days.setdefault(r["worker_id"], set()).add(a.date())
        hours[r["worker_id"]] = hours.get(r["worker_id"], 0) + (b - a).total_seconds() / 3600

    # The story is "the agent measures first", so what must hold is that the records are READABLE and
    # DISTINGUISHABLE — not an exact row count, which would break on any tuning of this file.
    for wid in CREW:
        rate = len(days.get(wid, ())) / WEEKS
        fx.require(3.5 <= rate <= 6.5,
                   f"{wid:12} {len(days.get(wid, ())):3} days {hours.get(wid, 0):6.0f} hrs "
                   f"{rate:.1f}/wk ({CREW[wid][0]}, {CREW[wid][3]})")
    fx.require(max(len(days.get(w, ())) for w in CREW) == len(days.get("deo-park", ())),
               "deo-park has the highest pace on the crew (the number a proposal can overrun)")
    for wid, (first, last, _rate) in PEOPLE.items():
        c = fx.s.call("contacts", "manage_contacts", {"op": "get", "contact_id": wid})
        c = c.get("contact") or c.get("item") or c
        fx.require(c.get("role") == CREW[wid][0] and c.get("first_name") == first,
                   f"{wid:12} contact states {first} {last}, role={CREW[wid][0]} — not inferred from hours")

    fx.require(days.get("mira-kwon"),
               "mira-kwon HAS history — demo 01's POS export credits her with ~30% of sales, and a "
               "crew member with revenue but no shifts makes the agent stop and flag the mismatch "
               "instead of scheduling")

    # Availability, read through the agent's own tool: what it will actually see, not what's stored.
    for wid in AVAILABILITY:
        free, util = fx.capacity_free(f"{SHIFT_PREFIX}{wid}", now.isoformat(), fortnight.isoformat())
        fx.require(free and util == 0,
                   f"{wid:12} {len(free):2} free window(s) over the fortnight, {util:.0%} booked")

    # The take ends by RESERVING, so a re-run starts fully booked unless reset ran. What matters is
    # that nothing STANDS, not that the log is empty: a book-then-cancel cycle leaves a −1 and its
    # mirror, which net to zero and consume nothing. Assert the net, and report the row count as
    # context — a log that keeps growing across takes is worth noticing, but it isn't a blocker.
    rows = [r for r in fx.table("gerp-inventory-gradienterp-movements").scan().get("Items", [])
            if str(r.get("item_id", "")).startswith(SHIFT_PREFIX)]
    net = sum(int(r["delta"]) for r in rows)
    fx.require(net >= 0, f"nothing standing on the crew ({len(rows)} movement row(s), net {net})")

    # Demo 01's POS export carries `served_by`, which is what turns "rank the baristas by revenue per
    # hour" from a stated limitation into a live computation. Not this fixture's to seed — but the
    # beat is absent without it, and absent QUIETLY, so say so here rather than discovering it in a
    # take. A bare `aws s3 cp` upload does not count: `find` matches on captions.
    pos = fx.s.call("storage", "manage_storage",
                    {"op": "find", "prefix": "uploads/", "query": "POS"}).get("documents", [])
    fx.require(pos, "a POS export is FILED and findable (demo 01's fixture) — without it the agent "
                    "cannot rank by revenue/hour and the demo loses its strongest beat")


if __name__ == "__main__":
    fx.run("demo 03 · labor — schedule the FOH for the next 2 weeks", seed, reset, check)
