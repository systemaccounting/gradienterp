"""What a failure record is (modules/aws/aws.py): one JSON object with `kind` (a declared Kind
or the slug of a fixed message), `category` (the Kind's, or classified from the exception),
`error`/`error_type`, the ids named as the tables name them, `gerp_id` and `function` from the
env, bound ids on every line of an invocation, and a `Failure` whose fields a generic catch
writes whole. Under LOG_STRICT a field outside the vocabulary raises."""

import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "modules" / "aws"))
os.environ["LOG_STRICT"] = "1"
import aws  # noqa: E402
from aws import log, Kind, Failure, bind, classify, stream_batch  # noqa: E402

K = Kind("stock_move_failed", "stock move failed", "dependency", ids=("po_id", "item_id"))


def _line(fn):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return json.loads(buf.getvalue().splitlines()[0])


def test_a_declared_kind_and_a_slugged_message_both_key_the_record():
    a = _line(lambda: log.error(K, po_id="po-1", item_id="1#beans"))
    assert a["kind"] == "stock_move_failed" and a["category"] == "dependency"
    assert a["message"] == "stock move failed" and a["po_id"] == "po-1"
    b = _line(lambda: log.error("distribution income not posted", thread="t1"))
    assert b["kind"] == "distribution_income_not_posted" and "category" not in b
    assert b["thread_id"] == "t1" and "thread" not in b, "thread is a LogRecord word; the wire name is thread_id"


def test_an_exception_is_classified_and_named():
    from botocore.exceptions import ClientError

    def go():
        try:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetItem")
        except Exception:
            log.exception("read failed", key="k")
    r = _line(go)
    assert r["category"] == "permission" and r["error_type"] == "ClientError" and "AccessDenied" in r["error"]
    assert classify(TimeoutError()) == "timeout" and classify(KeyError("x")) == "data"
    assert classify(ClientError({"Error": {"Code": "ParameterNotFound"}}, "Get")) == "config"


def test_a_failure_carries_its_record_through_a_generic_catch():
    def one(rec):
        raise Failure(K, po_id="po-2", item_id="i", status=502)
    buf = io.StringIO()
    with redirect_stdout(buf):
        out = stream_batch({"Records": [{"eventID": "e1", "dynamodb": {"SequenceNumber": "7"}}]}, one)
    r = json.loads(buf.getvalue().splitlines()[0])
    assert out["batchItemFailures"] == [{"itemIdentifier": "7"}]
    assert r["kind"] == "stock_move_failed" and r["po_id"] == "po-2" and r["status"] == 502 and r["sequence"] == "7"
    assert r["raised_at"].startswith("test_log_record.py:one:"), "the raise site, since location names the catch"
    e = Failure(K, po_id="p", item_id="i", status=404)
    assert e.status == 404 and str(e) == "stock move failed"


def test_bound_ids_ride_every_line_of_the_invocation():
    bind(po_id="po-9")
    try:
        assert _line(lambda: log.info("routed", detail_type="x"))["po_id"] == "po-9"
    finally:
        bind()
    assert "po_id" not in _line(lambda: log.info("routed", detail_type="x"))


def test_the_vocabulary_is_checked_where_it_is_declared_and_used():
    for bad in (dict(kind="Not Snake", message="m", category="data"),
                dict(kind="ok", message="m", category="weird"),
                dict(kind="ok", message="m", category="data", ids=("no_such_id",))):
        try:
            Kind(**bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Kind accepted {bad}")
    try:
        Failure(K, nope=1)
    except TypeError:
        pass
    else:
        raise AssertionError("a Failure took a field its kind does not declare")
    try:
        log.info("x", invented_field=1)
    except KeyError:
        pass
    else:
        raise AssertionError("strict mode let an unknown field through")
    assert not aws.IDS & aws.VALUES


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all log-record tests passed")
