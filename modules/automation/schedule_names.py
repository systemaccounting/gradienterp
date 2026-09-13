"""Schedule naming, shared by the tools that write, the ones that read, and the scripts
that have to line a schedule up with the thing it is about (`ctx.subject`).

A schedule's NAME carries which script it runs and what it runs on, because listing has to
answer that without a `GetSchedule` per entry — `ListSchedules` returns only the target ARN,
and every automation targets the same runner, so a thousand entries would otherwise be a
thousand extra calls. Per-subject scheduling is a script-shape choice a firm can make, so a
thousand is a real number.

    auto-<script stem>-<subject>

Both parts are sanitized to `[0-9a-zA-Z_.]` — hyphens included — so the two separators are
the only hyphens and `split("-")` is unambiguous. Scheduler allows 64 characters of
`[0-9a-zA-Z-_.]`.
"""

import re

PREFIX = "auto"
MAX_NAME = 64
MAX_STEM = 24
MAX_SUBJECT = MAX_NAME - len(PREFIX) - 2 - MAX_STEM  # the two hyphens


def slug(text: str, limit: int) -> str:
    """Anything outside the safe set becomes `_`, so no part can contain a separator."""
    out = re.sub(r"[^0-9a-zA-Z_.]", "_", (text or "").strip())
    out = re.sub(r"_{2,}", "_", out).strip("_")
    return out[:limit] or "x"


def stem_of(script: str) -> str:
    """'dunning.py' -> 'dunning'."""
    return slug(script.rsplit(".", 1)[0], MAX_STEM)


def name_for(script: str, subject: str = "") -> str:
    return f"{PREFIX}-{stem_of(script)}-{slug(subject, MAX_SUBJECT)}"


def parse(name: str) -> dict:
    """Read a schedule name back. Anything not ours returns an empty stem, so a listing that
    somehow sees a foreign schedule reports it rather than guessing at it."""
    parts = (name or "").split("-")
    if len(parts) != 3 or parts[0] != PREFIX:
        return {"stem": "", "subject": "", "name": name}
    return {"stem": parts[1], "subject": parts[2], "name": name}
