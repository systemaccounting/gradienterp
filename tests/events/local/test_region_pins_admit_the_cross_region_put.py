"""An addressed event is put on the recipient hub's bus, in the recipient's region, by a lambda
in the sender's account. The sender's account sits in a customers OU whose region pin denies
every region-aware action outside its own region; the put crosses regions on purpose, so the pin
excepts `events:PutEvents`. The first put from gradienterp to the Irish gerp was refused by the
pin, after the directory had resolved the bus.

Every pin reads the one exceptions list, so a pin cannot lose the exception on its own."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MANAGEMENT = REPO / "prod" / "platform" / "management"


def _exceptions():
    body = (MANAGEMENT / "main.tf").read_text()
    m = re.search(r"region_pin_exceptions\s*=\s*\[(.*?)\]", body, re.S)
    assert m, "main.tf defines local.region_pin_exceptions"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_the_pins_except_put_events():
    assert "events:PutEvents" in _exceptions()


def test_every_pin_reads_the_one_list():
    pins = re.findall(r'data "aws_iam_policy_document" "(scp_region_pin\w*)" \{(.*?)\n\}',
                      (MANAGEMENT / "main.tf").read_text() + (MANAGEMENT / "regions.tf").read_text(), re.S)
    names = {n for n, _ in pins}
    assert {"scp_region_pin", "scp_region_pin_region", "scp_region_pin_platform"} <= names, names
    for name, body in pins:
        assert "not_actions = local.region_pin_exceptions" in body, f"{name} carries its own list"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all region pin tests passed")
