"""A person works an alarm task (scripts/investigate.py): the collector's content parses into the
fields the read needs; the prompt carries the task, the lines the query returned, the queue's
head when the task names one, and the module's docs found by the function's name; the write-back
goes through the door with `investigated_by: local` and all three fields or none."""

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

CONTENT = """[alarm][westwood-c40fd8] stock_move_failed in gerp-purchasing-westwood-c40fd8-on_po_received
alarm: gerp-westwood-c40fd8-error-lines
state changed: 2026-09-08T04:00:00+0000
reason: Threshold Crossed
function: gerp-purchasing-westwood-c40fd8-on_po_received
gerp: westwood-c40fd8
account: 222165865776
window: 2026-09-08T03:45:00+00:00 to 2026-09-08T04:00:30+00:00
count in window: 3
category: dependency
log group: /aws/lambda/gerp-purchasing-westwood-c40fd8-on_po_received
query: fields @timestamp, message | filter kind = "stock_move_failed"
lines:
- 2026-09-08T03:59:10Z main.py:_move:66 po_id=po-1, item_id=1#beans"""


def _load():
    spec = importlib.util.spec_from_file_location("investigate", REPO / "scripts" / "investigate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_collectors_content_parses_into_the_read():
    mod = _load()
    f = mod.parse(CONTENT)
    assert f["account"] == "222165865776" and f["log group"].endswith("on_po_received")
    assert f["query"].startswith("fields @timestamp") and f["function"].startswith("gerp-purchasing-")
    assert f["lines"] == ["2026-09-08T03:59:10Z main.py:_move:66 po_id=po-1, item_id=1#beans"]
    assert "queue" not in f


def test_the_prompt_carries_the_task_the_lines_the_queue_and_the_module():
    mod = _load()
    calls = []
    mod.door = lambda op, body: (calls.append((op, body)) or {"task": {"task_id": "t-9", "content": CONTENT + "\nqueue: https://sqs/q"}})

    class Session:
        def client(self, name):
            if name == "logs":
                return type("L", (), {"start_query": staticmethod(lambda **kw: {"queryId": "q1"}),
                                      "get_query_results": staticmethod(lambda **kw: {"status": "Complete", "results": [
                                          [{"field": "@timestamp", "value": "2026-09-08 03:59:10"}, {"field": "message", "value": "stock move failed"}, {"field": "@ptr", "value": "x"}]]})})()
            return type("S", (), {"receive_message": staticmethod(lambda **kw: {"Messages": [{"Body": '{"po_id": "po-1"}', "Attributes": {"ApproximateReceiveCount": "3"}}]})})()
    opened = {}
    mod.reader = lambda account, region="": opened.update(account=account, region=region) or Session()
    text = mod.prompt("t-9")
    assert calls == [("get", {"task_id": "t-9"})]
    assert opened == {"account": "222165865776", "region": ""}, "an older task with no region line reads in the operator's region"
    assert "stock_move_failed in gerp-purchasing" in text
    assert '"message": "stock move failed"' in text and "@ptr" not in text
    assert '"po_id": "po-1"' in text and '"ApproximateReceiveCount": "3"' in text
    assert "### modules/purchasing/AGENTS.md" in text, "the module's docs, found by the function's name"
    assert "--finding" in text and "--root-cause" in text and "--proposed-fix" in text


def test_the_read_opens_in_the_tasks_region():
    """A task filed for a gerp in Ireland carries `region: eu-west-1`; the session that runs the
    query is opened there, where the log group is."""
    mod = _load()
    content = CONTENT.replace("account: 222165865776", "account: 832348493159\nregion: eu-west-1")
    assert mod.parse(content)["region"] == "eu-west-1"
    mod.door = lambda op, body: {"task": {"task_id": "t-9", "content": content}}
    opened = {}

    class Session:
        def client(self, name):
            return type("L", (), {"start_query": staticmethod(lambda **kw: {"queryId": "q1"}),
                                  "get_query_results": staticmethod(lambda **kw: {"status": "Complete", "results": []})})()
    mod.reader = lambda account, region="": opened.update(account=account, region=region) or Session()
    mod.prompt("t-9")
    assert opened == {"account": "832348493159", "region": "eu-west-1"}


def test_the_write_back_names_local_and_takes_all_three():
    mod = _load()
    calls = []
    mod.door = lambda op, body: (calls.append((op, body)) or {"task": {"task_id": "t-9", **body["updates"]}})
    out = mod.write_back("t-9", "3 lines", "the item is missing", "create it on receipt")
    assert calls == [("update", {"task_id": "t-9", "updates": {"investigated_by": "local", "finding": "3 lines",
                                                                 "root_cause": "the item is missing", "proposed_fix": "create it on receipt"}})]
    assert out["task"]["investigated_by"] == "local"
    sys.argv = ["investigate", "t-9", "--finding", "only one"]
    try:
        mod.main()
    except SystemExit as e:
        assert "all three" in str(e)
    else:
        raise AssertionError("a partial write-back was accepted")


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all investigate tests passed")
