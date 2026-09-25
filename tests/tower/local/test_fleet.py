"""fleet.py (issue #57): the fleet deploy in pieces, with stubbed AWS. The snapshot taken once is
the target every move uses; only a function whose sha differs moves, to the snapshot's version id;
a src_dir outside the snapshot and a src_dir another gerp carries and this one does not are `left`; one
function's failure is a line and an exit code, not the fleet's end; and no script under scripts/
learns where it runs."""
import contextlib
import io
import re
import sys
from argparse import Namespace
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
import deploy  # noqa: E402
import fleet  # noqa: E402


class Waiter:
    def wait(self, **kw):
        pass


class Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kw):
        return self.pages


class Lambda:
    def __init__(self, shas, refuse=()):
        self.shas, self.refuse, self.updates = shas, set(refuse), []

    def get_paginator(self, name):
        items = [{"FunctionName": n, "CodeSha256": s} for n, s in self.shas.items()]
        return Paginator([{"Functions": items[:1]}, {"Functions": items[1:]}])

    def update_function_code(self, **kw):
        if kw["FunctionName"] in self.refuse:
            raise RuntimeError("ResourceConflictException: update in progress")
        self.updates.append(kw)

    def get_waiter(self, name):
        return Waiter()


class Tagging:
    def __init__(self, fns):
        self.fns = fns

    def get_resources(self, **kw):
        return {"ResourceTagMappingList": [{"ResourceARN": f"arn:aws:lambda:us-east-1:1:function:{n}",
                                            "Tags": [{"Key": deploy.TAG_KEY, "Value": s}]} for n, s in self.fns.items()],
                "PaginationToken": ""}


class S3:
    class exceptions:
        NoSuchKey = KeyError

    def __init__(self, objects):
        self.objects = dict(objects)

    def get_paginator(self, name):
        return Paginator([{"Contents": [{"Key": k} for k in self.objects]}])

    def get_object_attributes(self, Bucket, Key, ObjectAttributes):
        version, sha = self.objects[Key]
        return {"VersionId": version, "Checksum": {"ChecksumSHA256": sha}}


class DDB:
    def scan(self, **kw):
        return {"Items": [{"gerp_id": {"S": "a"}, "aws_account_id": {"S": "1"}},
                          {"gerp_id": {"S": "b"}, "aws_account_id": {"S": "2"}, "region": {"S": "us-east-1"}}]}


class Session:
    def __init__(self, clients, region="us-east-1"):
        self.clients, self.region_name = clients, region

    def client(self, name, **kw):
        return self.clients[name]


def _run(fn, args, stdin=""):
    out, saved = io.StringIO(), sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out):
            fn(args)
    finally:
        sys.stdin = saved
    return [line.split("\t") for line in out.getvalue().splitlines()]


@contextlib.contextmanager
def _boto(sessions):
    saved = deploy._boto
    deploy._boto = lambda profile: sessions[profile]
    try:
        yield
    finally:
        deploy._boto = saved


SNAPSHOT = {"modules/x/lambdas/one.zip": ("v-one-2", "SHA-ONE-2"), "modules/x/lambdas/two.zip": ("v-two-1", "SHA-TWO-1"),
            "prod/tower/lambdas/bill.zip": ("v-bill", "SHA-BILL")}


def test_the_pieces_print_their_lines_and_the_join_names_every_state():
    import os
    import tempfile
    s3 = S3(SNAPSHOT)
    lam_a = Lambda({"gerp-x-a-one": "SHA-ONE-1", "gerp-x-a-two": "SHA-TWO-1", "gerp-y-a-odd": "SHA-ODD"})
    sessions = {"operator-org": Session({"s3": s3, "dynamodb": DDB()}),
                "gerp-a": Session({"lambda": lam_a, "resourcegroupstaggingapi": Tagging({"gerp-x-a-one": "modules/x/lambdas/one", "gerp-x-a-two": "modules/x/lambdas/two", "gerp-y-a-odd": "modules/y/lambdas/odd"})})}
    with _boto(sessions):
        snap = _run(fleet.listzipversions, Namespace(operator_profile="operator-org"))
        assert snap == [["modules/x/lambdas/one", "v-one-2", "SHA-ONE-2"], ["modules/x/lambdas/two", "v-two-1", "SHA-TWO-1"], ["prod/tower/lambdas/bill", "v-bill", "SHA-BILL"]]
        assert _run(fleet.listgerps, Namespace(operator_profile="operator-org")) == [["a", "1", "us-east-1"], ["b", "2", "us-east-1"]]
        conf = _run(fleet.listzipfnsconf, Namespace(gerp="a", profile=None))
        assert conf == [["a", "gerp-x-a-one", "modules/x/lambdas/one", "SHA-ONE-1"], ["a", "gerp-x-a-two", "modules/x/lambdas/two", "SHA-TWO-1"], ["a", "gerp-y-a-odd", "modules/y/lambdas/odd", "SHA-ODD"]]
        fd, path = tempfile.mkstemp(prefix="fleet-snapshot-")
        os.close(fd)
        Path(path).write_text("".join("\t".join(r) + "\n" for r in snap))
        rows = _run(fleet.status, Namespace(snapshot=path, dirs=None), stdin="".join("\t".join(r) + "\n" for r in conf))
        assert rows == [["a", "gerp-x-a-one", "behind", "modules/x/lambdas/one.zip", "v-one-2", "SHA-ONE-2"],
                        ["a", "gerp-x-a-two", "in-sync"],
                        ["a", "gerp-y-a-odd", "left", "no artifact for modules/y/lambdas/odd"]], "behind carries the snapshot's version; the tower key is nobody's"
        assert _run(fleet.status, Namespace(snapshot=path, dirs=["modules/x/lambdas/two"]), stdin="".join("\t".join(r) + "\n" for r in conf)) == [["a", "gerp-x-a-two", "in-sync"]]
        rows = _run(fleet.status, Namespace(snapshot=path, dirs=None),
                    stdin="a\tgerp-x-a-one\tmodules/x/lambdas/one\tSHA-ONE-2\nb\tgerp-x-b-one\tmodules/x/lambdas/one\tSHA-ONE-2\nb\tgerp-x-b-two\tmodules/x/lambdas/two\tSHA-TWO-1\n")
        assert rows == [["a", "gerp-x-a-one", "in-sync"], ["b", "gerp-x-b-one", "in-sync"], ["b", "gerp-x-b-two", "in-sync"],
                        ["a", "-", "left", "no function for modules/x/lambdas/two"]], "a src_dir b carries and a does not; the retired tower zip says nothing"


def test_the_snapshot_is_the_target_and_only_what_differs_moves():
    """A version put after the snapshot is not taken: update moves to the version id on its line."""
    s3 = S3(SNAPSHOT)
    lam = Lambda({"gerp-x-a-one": "SHA-ONE-1", "gerp-x-a-two": "SHA-TWO-1"})
    sessions = {"operator-org": Session({"s3": s3}), "gerp-a": Session({"lambda": lam})}
    with _boto(sessions):
        s3.objects["modules/x/lambdas/one.zip"] = ("v-one-3", "SHA-ONE-3")   # lands between the snapshot and the moves
        rows = _run(fleet.update, Namespace(operator_profile="operator-org"),
                    stdin="a\tgerp-x-a-one\tbehind\tmodules/x/lambdas/one.zip\tv-one-2\tSHA-ONE-2\na\tgerp-x-a-two\tin-sync\n")
    assert rows == [["a", "gerp-x-a-one", "moved", "v-one-2"]]
    assert lam.updates == [{"FunctionName": "gerp-x-a-one", "S3Bucket": deploy.BUCKET, "S3Key": "modules/x/lambdas/one.zip", "S3ObjectVersion": "v-one-2"}], \
        "the snapshot's version, never the bucket's latest; the in-sync line moves nothing"


def test_one_functions_failure_is_a_line_and_the_exit_and_the_others_move():
    s3 = S3(SNAPSHOT)
    lam = Lambda({}, refuse={"gerp-x-a-one"})
    sessions = {"operator-org": Session({"s3": s3}), "gerp-a": Session({"lambda": lam})}
    err = io.StringIO()
    with _boto(sessions), contextlib.redirect_stderr(err):
        try:
            rows = _run(fleet.update, Namespace(operator_profile="operator-org"),
                        stdin="a\tgerp-x-a-one\tbehind\tmodules/x/lambdas/one.zip\tv-one-2\tSHA-ONE-2\na\tgerp-x-a-two\tbehind\tmodules/x/lambdas/two.zip\tv-two-1\tSHA-TWO-1\n")
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("a failed move must fail the exit")
    assert [kw["FunctionName"] for kw in lam.updates] == ["gerp-x-a-two"], "the other function moved"
    assert "failed: a gerp-x-a-one" in err.getvalue()


def test_no_script_learns_where_it_runs_and_the_laptop_pipe_is_the_pieces_in_order():
    for f in sorted((REPO / "scripts").glob("*.py")) + sorted((REPO / "scripts").glob("*.sh")):
        assert "GITHUB_ACTIONS" not in f.read_text(), f"{f.name} branches on the runner"
    sh = (REPO / "scripts" / "deploy.sh").read_text()
    pipe = sh[sh.index("fleet.py listzipversions"):sh.index("fleet.py update")]
    order = [m for m in re.findall(r"fleet\.py (\w+)", pipe)]
    assert order == ["listzipversions", "listgerps", "listzipfnsconf", "status"], "the snapshot first, then the gerps, their functions, the join"
    assert "xargs -P 10" in pipe and "--report" in sh


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all fleet tests passed")
