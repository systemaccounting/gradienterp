"""No log group keeps logs longer than the Privacy Notice says request logs are kept.

`docs/PRIVACY.md` states the period; `config.json` `LOG_RETENTION_DAYS` is what terraform reads. Every
`retention_in_days` and `log_retention_days` in terraform, and every `log_retention_days` variable's
default, is a number at most that period or one of the names that carry it. Any other source fails
here by location, to be checked by hand. Text check, no terraform.
"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

ASSIGNMENT = re.compile(r"^\s*(retention_in_days|log_retention_days)\s*=\s*([^#\n]+)", re.M)
VARIABLE = re.compile(r'variable\s+"log_retention_days"\s*\{(.*?)\n\}', re.S)
DEFAULT = re.compile(r"default\s*=\s*([^#\n]+)")
# the names a retention value may come through; each resolves to LOG_RETENTION_DAYS or a checked number
CARRIERS = {"local.config.LOG_RETENTION_DAYS", "var.log_retention_days", "local.log_retention_days"}


def _terraform_files():
    for root in ("prod", "modules"):
        for tf in (REPO / root).rglob("*.tf"):
            if ".terraform" not in tf.parts:
                yield tf


def _privacy_days():
    text = (REPO / "docs" / "PRIVACY.md").read_text()
    m = re.search(r"request logs\*\* are kept for (\d+) days", text)
    assert m, "docs/PRIVACY.md no longer states how long request logs are kept"
    return int(m.group(1))


def _offence(expr, limit):
    """Why `expr` can keep logs past `limit`, or "" when it can't."""
    expr = expr.strip()
    for n in re.findall(r"\b\d+\b", expr):
        if int(n) > limit:
            return f"{n} days"
    names = set(re.findall(r"\b(?:local|var)\.[\w.]+", expr))
    if not names and not re.fullmatch(r"\d+", expr):
        return f"`{expr}` is not a number or a known name"
    unknown = names - CARRIERS
    return f"`{', '.join(sorted(unknown))}` is not a name that carries LOG_RETENTION_DAYS" if unknown else ""


def test_the_configured_retention_is_within_the_privacy_notice():
    config = json.loads((REPO / "config.json").read_text())
    assert config["LOG_RETENTION_DAYS"] <= _privacy_days()


def test_no_terraform_log_retention_exceeds_the_configured_retention():
    limit = json.loads((REPO / "config.json").read_text())["LOG_RETENTION_DAYS"]
    offenders, seen = [], 0
    for tf in _terraform_files():
        text = tf.read_text()
        for key, expr in ASSIGNMENT.findall(text):
            seen += 1
            why = _offence(expr, limit)
            if why:
                offenders.append(f"{tf.relative_to(REPO)}: {key} = {expr.strip()} ({why})")
        for body in VARIABLE.findall(text):
            m = DEFAULT.search(body)
            why = _offence(m.group(1), limit) if m else ""
            if why:
                offenders.append(f"{tf.relative_to(REPO)}: variable log_retention_days default ({why})")
    assert seen > 50, f"found {seen} retention settings; the pattern no longer matches terraform"
    assert not offenders, f"log retention past {limit} days:\n  " + "\n  ".join(offenders)


def test_a_longer_retention_is_caught():
    assert _offence("400", 90) == "400 days"
    assert _offence("try(local.config.LOG_RETENTION_DAYS, 365)", 90) == "365 days"
    assert "not a name" in _offence("var.keep_forever", 90)
    assert _offence("try(local.config.LOG_RETENTION_DAYS, 90)", 90) == ""
    assert _offence("var.log_retention_days", 90) == ""


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all log retention tests passed")
