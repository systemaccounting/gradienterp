"""upload.sh puts what zip.sh or docker.sh --build made, with stubbed AWS clients.

Two source keys keep an upload from moving what a signup builds from: `release/source.zip`, the
CodeBuild projects' own location, takes only a committed tree; `source.zip` takes any, and the
operator's builds name it. A lambda or BFF upload goes through the one put `push` makes, and refuses
a `.build` zip the tree no longer builds to, since its provenance names the tree."""

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
import apply  # noqa: E402
import deploy  # noqa: E402


class S3:
    """Keys to (body, version); every put and annotation recorded."""

    class exceptions:
        NoSuchKey = KeyError

    def __init__(self):
        self.objects, self.puts, self.annotations = {}, [], []

    def head_object(self, Bucket, Key):
        body, version = self.objects[(Bucket, Key)]
        return {"ETag": f'"{hashlib.md5(body).hexdigest()}"', "VersionId": version}

    def get_object_attributes(self, Bucket, Key, ObjectAttributes):
        body, version = self.objects[(Bucket, Key)]
        return {"VersionId": version, "Checksum": {"ChecksumSHA256": deploy.sha256_b64_bytes(body)}}

    def put_object(self, Bucket, Key, Body, **kw):
        version = f"ver{len(self.puts) + 1}"
        self.puts.append({"Bucket": Bucket, "Key": Key, **kw})
        self.objects[(Bucket, Key)] = (Body, version)
        return {"VersionId": version}

    def put_object_annotation(self, **kw):
        self.annotations.append(kw)


@contextlib.contextmanager
def _repo():
    """A scratch REPO for deploy's module-level paths, with one Python lambda in it."""
    root = Path(tempfile.mkdtemp())
    fn = root / "modules" / "x" / "lambdas" / "y"
    fn.mkdir(parents=True)
    (fn / "main.py").write_text("def handler(e, c):\n    return 1\n")
    saved = deploy.REPO
    deploy.REPO = str(root)
    try:
        yield root
    finally:
        deploy.REPO = saved
        shutil.rmtree(root)


def _source_zip(root, dirty):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("source.json", json.dumps({"commit": "abc123", "dirty": dirty, "dirs": []}))
        z.writestr("config.json", "{}")
    (root / ".build").mkdir(exist_ok=True)
    (root / ".build" / "source.zip").write_bytes(buf.getvalue())


def _refused(fn):
    try:
        fn()
    except SystemExit as e:
        return str(e)
    raise AssertionError("expected a refusal")


def test_a_release_of_an_uncommitted_tree_is_refused_and_puts_nothing():
    with _repo() as root:
        _source_zip(root, dirty=True)
        s3 = S3()
        assert "uncommitted" in _refused(lambda: deploy.upload_source(s3, release=True))
        assert s3.puts == []


def test_a_committed_release_goes_to_the_projects_own_key_with_its_commit():
    with _repo() as root:
        _source_zip(root, dirty=False)
        s3 = S3()
        deploy.upload_source(s3, release=True)
        [put] = s3.puts
        assert (put["Bucket"], put["Key"]) == (deploy.SOURCE_BUCKET, "release/source.zip")
        assert put["Tagging"] == "commit=abc123&dirty=false"


def test_any_tree_goes_to_source_zip_and_an_unchanged_one_puts_nothing():
    with _repo() as root:
        _source_zip(root, dirty=True)
        s3 = S3()
        first = deploy.upload_source(s3, release=False)
        assert [p["Key"] for p in s3.puts] == ["source.zip"] and s3.puts[0]["Tagging"].endswith("dirty=true")
        assert deploy.upload_source(s3, release=False) == first and len(s3.puts) == 1


def test_a_lambda_puts_once_with_its_provenance_and_then_is_current():
    with _repo():
        src = "modules/x/lambdas/y"
        blob = deploy.build_artifact(src)
        (Path(deploy.REPO) / ".build" / "lambdas" / "modules" / "x" / "lambdas").mkdir(parents=True)
        (Path(deploy.REPO) / ".build" / "lambdas" / f"{src}.zip").write_bytes(blob)
        s3 = S3()
        deploy.upload_lambdas(s3, [src], "notes here")
        assert [p["Key"] for p in s3.puts] == [f"{src}.zip"]
        prov = json.loads(next(a for a in s3.annotations if a["AnnotationName"] == "provenance")["AnnotationPayload"])
        assert prov["zip_sha256"] == deploy.sha256_b64_bytes(blob) and prov["src_dir"] == src
        assert any(a["AnnotationName"] == "release" and a["AnnotationPayload"] == b"notes here" for a in s3.annotations)
        deploy.upload_lambdas(s3, [src], "")
        assert len(s3.puts) == 1, "an unchanged zip is already the bucket's"


def test_a_zip_the_tree_no_longer_builds_to_is_refused():
    with _repo() as root:
        src = "modules/x/lambdas/y"
        (root / ".build" / "lambdas" / "modules" / "x" / "lambdas").mkdir(parents=True)
        (root / ".build" / "lambdas" / f"{src}.zip").write_bytes(deploy.build_artifact(src))
        (root / src / "main.py").write_text("def handler(e, c):\n    return 2\n")
        s3 = S3()
        why = _refused(lambda: deploy.upload_lambdas(s3, [src], ""))
        assert "zip.sh lambda modules/x/lambdas/y" in why and s3.puts == []


def test_the_next_image_tag_is_one_past_the_highest():
    class ECR:
        def describe_images(self, **kw):
            return {"imageDetails": [{"imageTags": ["v7"]}, {"imageTags": ["v12", "latest"]}, {}]}
    assert deploy.latest_image_tag(ECR()) == 12


def test_a_build_the_operator_starts_names_its_source_and_never_the_projects_own():
    class CB:
        def start_build(self, **kw):
            self.kw = kw
            return {"build": {"id": "p:1"}}

        def batch_get_builds(self, ids):
            return {"builds": [{"currentPhase": "COMPLETED", "buildComplete": True, "buildStatus": "SUCCEEDED", "logs": {}}]}

    cb = CB()

    class S3Head:
        def head_object(self, Bucket, Key):
            return {"VersionId": f"latest-of-{Key}"}

    class Op:
        def client(self, name):
            return S3Head() if name == "s3" else cb

    with contextlib.redirect_stdout(io.StringIO()):
        apply._run_build(Op(), "g", "1", "apply", "us-east-1")
    assert cb.kw["sourceLocationOverride"] == f"{deploy.SOURCE_BUCKET}/source.zip"
    assert cb.kw["sourceVersion"] == "latest-of-source.zip", "the latest is named, so the build records it"
    with contextlib.redirect_stdout(io.StringIO()):
        apply._run_build(Op(), "g", "1", "apply", "us-east-1", source_version="abc")
    assert cb.kw["sourceVersion"] == "abc"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all upload tests passed")
