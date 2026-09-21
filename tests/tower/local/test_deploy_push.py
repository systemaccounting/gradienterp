"""deploy.sh points live functions and runtimes at what a bucket or registry holds, with stubbed AWS.

`push --deploy` builds nothing, the BFF included; a push builds and puts a changed function once and
updates it once; `--all` reaches each gerp through its own profile and pushes the BFF once; and an
image deploy reaches a gerp in its own region, from that region's copy of the image — Dublin's
runtime runs the eu-west-1 copy, and a session with no region is denied in us-east-1."""

import contextlib
import io
import shutil
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
import deploy  # noqa: E402


class Waiter:
    def wait(self, **kw):
        pass


class Lambda:
    def __init__(self, sha="OLD"):
        self.sha, self.updates = sha, []

    def get_function_configuration(self, FunctionName):
        return {"CodeSha256": self.sha}

    def update_function_code(self, **kw):
        self.updates.append(kw)

    def get_waiter(self, name):
        return Waiter()


class S3:
    class exceptions:
        NoSuchKey = KeyError

    def __init__(self, objects=None):
        self.objects, self.puts = dict(objects or {}), []

    def get_object_attributes(self, Bucket, Key, ObjectAttributes):
        version, sha = self.objects[Key]
        return {"VersionId": version, "Checksum": {"ChecksumSHA256": sha}}

    def put_object(self, Bucket, Key, Body, **kw):
        version = f"ver{len(self.puts) + 1}"
        self.puts.append(Key)
        self.objects[Key] = (version, deploy.sha256_b64_bytes(Body))
        return {"VersionId": version}

    def put_object_annotation(self, **kw):
        pass


class Session:
    def __init__(self, clients, region="us-east-1"):
        self.clients, self.region_name = clients, region

    def client(self, name, **kw):
        return self.clients[name]


@contextlib.contextmanager
def _patched(**names):
    saved = {n: getattr(deploy, n) for n in names}
    for n, v in names.items():
        setattr(deploy, n, v)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        for n, v in saved.items():
            setattr(deploy, n, v)


def _no_build(*a, **kw):
    raise AssertionError("--deploy must not build")


def test_the_bff_deploy_builds_nothing_and_takes_the_artifact():
    s3, lam = S3({deploy.BFF_KEY: ("bffv9", "NEW")}), Lambda("OLD")
    with _patched(_boto=lambda p: Session({"s3": s3, "lambda": lam}), build_webapp=_no_build):
        deploy._push_webapp(Namespace(operator_profile="op", deploy=True, notes=""))
    assert lam.updates == [{"FunctionName": deploy.BFF_FN, "S3Bucket": deploy.BUCKET,
                            "S3Key": deploy.BFF_KEY, "S3ObjectVersion": "bffv9"}]
    assert s3.puts == []


def test_a_changed_function_is_put_once_and_updated_once():
    root = Path(tempfile.mkdtemp())
    src = "modules/x/lambdas/y"
    (root / src).mkdir(parents=True)
    (root / src / "main.py").write_text("def handler(e, c):\n    return 1\n")
    s3, lam = S3(), Lambda("OLD")
    try:
        with _patched(REPO=str(root), _boto=lambda p: Session({"s3": s3}), fleet=lambda s: {"fn-y": src}):
            deploy._push_fleet(Namespace(operator_profile="op", deploy=False, notes=""),
                               Session({"lambda": lam}), [src])
        assert s3.puts == [f"{src}.zip"]
        assert [u["S3ObjectVersion"] for u in lam.updates] == ["ver1"]
    finally:
        shutil.rmtree(root)


def test_push_deploy_updates_a_function_without_building_it():
    s3, lam = S3({"modules/x/lambdas/y.zip": ("ver4", "NEW")}), Lambda("OLD")
    with _patched(_boto=lambda p: Session({"s3": s3}), fleet=lambda s: {"fn-y": "modules/x/lambdas/y"},
                  build_artifact=_no_build):
        deploy._push_fleet(Namespace(operator_profile="op", deploy=True, notes=""),
                           Session({"lambda": lam}), ["modules/x/lambdas/y"])
    assert s3.puts == [] and [u["S3ObjectVersion"] for u in lam.updates] == ["ver4"]


def test_push_all_reaches_each_gerp_through_its_own_profile_and_the_bff_once():
    seen, fleets, bff = [], [], []

    def target(args):
        seen.append((args.gerp, args.profile))
        return args.gerp

    rows = [{"gerp_id": "a", "account": "1", "region": "us-east-1"},
            {"gerp_id": "b", "account": "2", "region": "eu-west-1"}]
    with _patched(_boto=lambda p: None, _active_rows=lambda op: rows, _target_session=target,
                  _push_webapp=lambda args: bff.append(1), _push_fleet=lambda args, s, d: fleets.append(s)):
        deploy.cmd_push(Namespace(dirs=None, all=True, profile=None, gerp="gradienterp",
                                  operator_profile="op", deploy=False, notes=""))
    assert seen == [("a", None), ("b", None)] and fleets == ["a", "b"] and bff == [1]
    try:
        deploy.cmd_push(Namespace(dirs=None, all=True, profile="p", gerp="g", operator_profile="op",
                                  deploy=False, notes=""))
    except SystemExit as e:
        assert "gerp-<id>" in str(e)
    else:
        raise AssertionError("--all with --profile names one profile for every gerp")


def test_an_image_deploy_reaches_a_gerp_in_its_own_region_from_that_regions_copy():
    class DDB:
        def scan(self, **kw):
            return {"Items": [{"gerp_id": {"S": "dublin"}, "aws_account_id": {"S": "832"},
                               "region": {"S": "eu-west-1"}}]}

    class STS:
        def assume_role(self, **kw):
            return {"Credentials": {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": "T"}}

    [(gerp, account, region, ses)] = list(deploy._gerp_sessions(Session({"dynamodb": DDB(), "sts": STS()})))
    assert (gerp, region, ses.region_name) == ("dublin", "eu-west-1", "eu-west-1")

    digest = "sha256:abc"
    uri = deploy.image_uri("eu-west-1", digest)
    assert ".dkr.ecr.eu-west-1.amazonaws.com/agentcore@sha256:abc" in uri

    class Ctl:
        updated = False

        def list_agent_runtimes(self):
            return {"agentRuntimes": [{"agentRuntimeId": "r", "agentRuntimeName": "agentcore_dublin"}]}

        def get_agent_runtime(self, **kw):
            return {"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": uri}}}

        def update_agent_runtime(self, **kw):
            Ctl.updated = True

    with contextlib.redirect_stdout(io.StringIO()):
        deploy._update_runtimes(Ctl(), digest, "dublin", "eu-west-1")
    assert not Ctl.updated, "a runtime already on the region's copy of the digest is left alone"


def test_an_image_deploy_with_a_tag_moves_onto_that_tag_and_not_the_top_of_ecr():
    """The deploy workflow builds once and moves each gerp in its own job; by the time a job starts,
    another push could have moved the top of ECR."""
    class ECR:
        def describe_images(self, repositoryName, imageIds=None, **kw):
            if imageIds:
                return {"imageDetails": [{"imageDigest": f"sha256:{imageIds[0]['imageTag']}"}]}
            return {"imageDetails": [{"imageTags": ["v128"]}, {"imageTags": ["v129"]}]}

    moved = []
    with _patched(_boto=lambda p: Session({"ecr": ECR()}),
                  _gerp_sessions=lambda op, only=None: iter([("westwood", "2", "us-east-1", Session({"bedrock-agentcore-control": None}))]),
                  image_replicated=lambda op, region, digest: True,
                  _update_runtimes=lambda ctl, digest, label, region: moved.append(digest)):
        deploy.cmd_image(Namespace(operator_profile="op", no_build=True, tag="v128", all=False, gerp="westwood"))
        assert moved == ["sha256:v128"]
        deploy.cmd_image(Namespace(operator_profile="op", no_build=True, tag=None, all=False, gerp="westwood"))
        assert moved[-1] == "sha256:v129", "without a tag, the top of ECR"
        try:
            deploy.cmd_image(Namespace(operator_profile="op", no_build=False, tag="v128", all=False, gerp="westwood"))
        except SystemExit as e:
            assert "--no-build" in str(e)
        else:
            raise AssertionError("--tag without --no-build names a build it won't make")



def test_a_dirs_value_that_arrived_as_one_word_is_its_words():
    """`push --dirs "a b"` (zsh's unsplit $var) is two dirs; `--dirs a b` is the same two."""
    assert deploy._flat([deploy._words("modules/a/lambdas/x modules/b/lambdas/y")]) == ["modules/a/lambdas/x", "modules/b/lambdas/y"]
    assert deploy._flat([deploy._words("modules/a/lambdas/x"), deploy._words("modules/b/lambdas/y")]) == ["modules/a/lambdas/x", "modules/b/lambdas/y"]
    assert deploy._flat(["positional", "strings"]) == ["positional", "strings"] and deploy._flat([]) is None


def test_a_pull_requests_image_is_pushed_under_its_own_tag_and_westwood_moves_onto_it():
    """image-check.yaml: a pull request's image goes to ECR as pr-<n>-<sha>, outside the vNN line, and
    the named gerp's runtime moves onto that digest — the vNN counter never advances for it."""
    class ECR:
        def describe_images(self, repositoryName, imageIds=None, **kw):
            if imageIds:
                return {"imageDetails": [{"imageDigest": f"sha256:{imageIds[0]['imageTag']}"}]}
            raise AssertionError("a named tag never reads the vNN line")

    moved, docker = [], []
    with _patched(_boto=lambda p: Session({"ecr": ECR()}),
                  _docker=lambda args, profile: docker.append(args),
                  _gerp_sessions=lambda op, only=None: iter([("westwood", "2", "us-east-1", Session({"bedrock-agentcore-control": None}))]),
                  image_replicated=lambda op, region, digest: True,
                  _update_runtimes=lambda ctl, digest, label, region: moved.append(digest)):
        deploy.cmd_image(Namespace(operator_profile="op", no_build=False, tag="pr-20-abcdef0", all=False, gerp="westwood"))
    assert docker[0] == ["--build"]
    assert docker[1][0] == "--push" and docker[1][1].endswith(":pr-20-abcdef0")
    assert moved == ["sha256:pr-20-abcdef0"]

def test_a_tower_dir_pushes_in_the_operator_account_names_no_gerp_and_takes_no_all():
    """tower's functions live in the operator account, outside every gerp's tag query: a tower dir
    goes through the operator session, a gerp dir through the gerp's, a bare push leaves tower
    alone, and --all is refused since there is one tower."""
    calls, asked = [], []

    def target(args):
        asked.append(args.gerp)
        return f"session:gerp-{args.gerp}"

    with _patched(_boto=lambda p: f"session:{p}", _target_session=target, _push_webapp=lambda args: None,
                  _push_fleet=lambda args, s, d: calls.append((s, d))):
        deploy.cmd_push(Namespace(dirs=["prod/tower/lambdas/notify_owner", "modules/x/lambdas/y"], all=False,
                                  profile=None, gerp="gradienterp", operator_profile="op", deploy=False, notes=""))
        assert calls == [("session:op", ["prod/tower/lambdas/notify_owner"]), ("session:gerp-gradienterp", ["modules/x/lambdas/y"])]
        calls.clear(); asked.clear()
        deploy.cmd_push(Namespace(dirs=["prod/tower/lambdas/notify_owner"], all=False, profile=None,
                                  gerp="gradienterp", operator_profile="op", deploy=False, notes=""))
        assert calls == [("session:op", ["prod/tower/lambdas/notify_owner"])] and asked == [], "no gerp is named or checked"
        calls.clear()
        try:
            deploy.cmd_push(Namespace(dirs=["prod/tower/lambdas/notify_owner"], all=True, profile=None,
                                      gerp="gradienterp", operator_profile="op", deploy=False, notes=""))
            raise AssertionError("--all with a tower dir must refuse")
        except SystemExit as e:
            assert "operator account" in str(e) and calls == []


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all deploy push tests passed")
