"""The inbox router: which inbound events reach a handler, which wake the agent, and the one it
waits for.

A `<kind>.proposed` is stamped by `agreements/apply_inbound`, which also runs the firm's
PROPOSAL#<kind> rules and answers `decided`. The router calls it synchronously and pokes the
agent only when nothing was decided — so a proposal a rule answers costs no model turn, and the
agent never wakes before its mirror row exists. Everything else keeps the fire-and-forget shape:
handler async, poke where POKE_ALSO says.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "aws"))


class FakeLambda:
    def __init__(self, decided=None, error=False):
        self.calls, self.decided, self.error = [], decided, error

    def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803
        self.calls.append((FunctionName, InvocationType, json.loads(Payload)))
        if InvocationType == "RequestResponse":
            if self.error:
                return {"FunctionError": "Unhandled", "Payload": _Body(b"{}")}
            return {"Payload": _Body(json.dumps({"applied": "t", "decided": self.decided}).encode())}
        return {"StatusCode": 202}


class _Body:
    def __init__(self, b): self._b = b
    def read(self): return self._b


def _router(lam):
    os.environ["ROUTES"] = json.dumps({"po.proposed": "apply_inbound", "po.accepted": "apply_inbound", "shipment.sent": "apply_shipment"})
    os.environ["POKE_FN"] = "poke_agent"
    os.environ["POKE_ALSO"] = json.dumps(["po.proposed", "offer.accepted"])
    spec = importlib.util.spec_from_file_location("router", REPO / "modules" / "inbox" / "lambdas" / "router" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.lam = lam
    return mod


def _record(dt):
    return {"Records": [{"eventName": "INSERT", "dynamodb": {"NewImage": {
        "detail_type": {"S": dt}, "thread": {"S": "t"}, "from_gerp": {"S": "westwood-c40fd8"}}}}]}


def test_a_proposal_a_rule_answered_wakes_nobody():
    lam = FakeLambda(decided="accept")
    _router(lam).handler(_record("po.proposed"), None)
    assert [(f, t) for f, t, _ in lam.calls] == [("apply_inbound", "RequestResponse")]


def test_a_proposal_nothing_decided_is_stamped_first_then_poked():
    lam = FakeLambda(decided=None)
    _router(lam).handler(_record("po.proposed"), None)
    assert [(f, t) for f, t, _ in lam.calls] == [("apply_inbound", "RequestResponse"), ("poke_agent", "Event")], \
        "the stamp lands before the agent is woken"


def test_a_failed_stamp_still_wakes_the_agent():
    """apply_inbound raising is its own alarm (the lambda_errors path); the proposal is not lost
    to silence — the agent reads the inbox row it was poked for."""
    lam = FakeLambda(error=True)
    _router(lam).handler(_record("po.proposed"), None)
    assert [(f, t) for f, t, _ in lam.calls] == [("apply_inbound", "RequestResponse"), ("poke_agent", "Event")]


def test_the_other_stamps_keep_the_async_shape():
    lam = FakeLambda()
    r = _router(lam)
    r.handler(_record("po.accepted"), None)          # routed, not in POKE_ALSO: handler only
    r.handler(_record("offer.accepted"), None)       # unrouted here but in POKE_ALSO: poke
    r.handler(_record("shipment.sent"), None)        # routed, mechanical: handler only
    r.handler(_record("quote.requested"), None)      # unrouted: poke
    assert [(f, t) for f, t, _ in lam.calls] == [
        ("apply_inbound", "Event"), ("poke_agent", "Event"), ("apply_shipment", "Event"), ("poke_agent", "Event")]


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all router tests passed")
