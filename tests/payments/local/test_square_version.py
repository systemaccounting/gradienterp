"""Every Square call names a released API version, and one place holds it. configure_webhook sent
the placeholder "SQUARE_API_VERSION" as `Square-Version` and as the subscription's `api_version`;
Square refuses that, so Square setup never worked."""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "payments"))
import square_api  # noqa: E402

CALLERS = ("modules/payments/lambdas/configure_webhook/provider_square.py",
           "modules/payments/lambdas/payment_links/test_square.py")


def test_the_version_is_a_released_date():
    assert re.fullmatch(r"20\d\d-\d\d-\d\d", square_api.VERSION)


def test_every_square_caller_reads_the_one_version():
    for path in CALLERS:
        src = (REPO / path).read_text()
        assert "import square_api" in src and "square_api.VERSION" in src, path
        assert "SQUARE_API_VERSION" not in src, path
        assert not re.search(r'API_VERSION = "20', src), f"{path} carries its own literal"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all square version tests passed")
