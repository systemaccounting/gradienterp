"""tower's seven functions source their code from the operator's artifact bucket like every
module's (issue #38): the bundle is scripts/deploy.py's, the import graph as the manifest; no
`archive_file` names files by hand; the bucket is prod/platform/operator's, read by tower by name."""
import io
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
import deploy  # noqa: E402

TOWER = REPO / "prod" / "tower"
FNS = ["bill_customer", "close_account", "cognito_post_confirmation", "notify_owner", "provision_customer",
       "update_business_info", "update_owner_email"]


def _tf():
    return "\n".join(p.read_text() for p in sorted(TOWER.glob("*.tf")))


def test_each_tower_function_bundles_with_every_local_import():
    for fn in FNS:
        names = zipfile.ZipFile(io.BytesIO(deploy.build_artifact(f"prod/tower/lambdas/{fn}"))).namelist()
        assert {"main.py", "aws.py"} <= set(names), (fn, names)
    # a sibling module under prod/tower/lambdas/ rides on the import alone: the resolver's second
    # tier is the lambdas root, so a shared file there needs no listing anywhere
    for fn in ("cognito_post_confirmation", "notify_owner", "close_account"):
        names = zipfile.ZipFile(io.BytesIO(deploy.build_artifact(f"prod/tower/lambdas/{fn}"))).namelist()
        assert "metrics_post.py" in names, (fn, names)


def test_tower_functions_are_on_the_bucket_and_no_archive_is_listed_by_hand():
    tf = _tf()
    assert 'data "archive_file"' not in tf
    for fn in FNS:
        block = re.search(rf'module "{fn}" \{{.*?\n\}}\n', tf, flags=re.S)
        assert block, fn
        assert re.search(rf'artifact_key\s*=\s*"prod/tower/lambdas/{fn}.zip"', block.group(0)), fn
        assert re.search(r"artifact_bucket\s*=\s*local.artifact_bucket", block.group(0)), fn
        assert "source_code_hash" not in block.group(0), fn


def test_the_bucket_is_the_operator_roots_and_tower_reads_it_by_name():
    operator = (REPO / "prod" / "platform" / "operator" / "artifacts.tf").read_text()
    assert 'resource "aws_s3_bucket" "artifacts"' in operator
    assert 'resource "aws_s3_bucket_versioning" "artifacts"' in operator
    tf = _tf()
    assert 'resource "aws_s3_bucket" "artifacts"' not in tf
    assert 'data "aws_s3_bucket" "artifacts"' in tf and "bucket = local.artifact_bucket" in tf
    assert "aws_s3_bucket_versioning.artifacts" not in tf, "nothing in tower depends on a resource it no longer holds"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all packaging tests passed")
