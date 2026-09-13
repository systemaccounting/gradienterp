"""Every Stripe request pins its API version.

Run: `bash scripts/test.sh --module payments`.

A call that sends no `Stripe-Version` runs against the account's DASHBOARD default, so someone
clicking upgrade there changes live payment behavior with no deploy and no diff. These tests read the
source rather than exercising a handler: the failure they guard is a call site added later that
forgets the header, which no functional test would notice — the request succeeds, it just succeeds
against a version nobody chose."""
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "modules/payments"))
import stripe_api  # noqa: E402

CALLERS = sorted(p for p in (REPO_ROOT / "modules").rglob("*.py")
                 if "api.stripe.com" in p.read_text())


def test_there_are_call_sites_to_check():
    """A rglob that silently matched nothing would make every test below vacuously pass."""
    assert len(CALLERS) >= 6, [str(p) for p in CALLERS]   # six since the test-payment adapter went to Stripe's own tools (modules/mcp)


def test_every_stripe_caller_sends_the_version_header():
    missing = [str(p.relative_to(REPO_ROOT)) for p in CALLERS
               if "Stripe-Version" not in p.read_text()]
    assert not missing, f"these reach api.stripe.com without pinning a version: {missing}"


def test_nobody_hardcodes_a_version_literal():
    """Seven literals drift; the first one left behind gets a 200 carrying a shape its parser
    half-understands."""
    literal = re.compile(r'"20\d\d-\d\d-\d\d\.[a-z]+"')
    offenders = []
    for p in CALLERS:
        for hit in literal.findall(p.read_text()):
            offenders.append(f"{p.relative_to(REPO_ROOT)}: {hit}")
    assert not offenders, f"use stripe_api.VERSION instead: {offenders}"


def test_the_pinned_version_is_well_formed():
    assert re.fullmatch(r"20\d\d-\d\d-\d\d\.[a-z]+", stripe_api.VERSION), stripe_api.VERSION


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
