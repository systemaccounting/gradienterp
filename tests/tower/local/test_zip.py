"""zip.sh builds every zip a deploy makes: the working tree for CodeBuild and the workflows, a fleet
function's bytes, the owner web app's bundle.

The source zip is git's view of the tree, so it can't carry what a push to the public repo couldn't
(`.env`, state, tfvars), and it has to survive what that listing gets wrong: a tracked file deleted
from the tree, a symlink, and the lock files of the roots CodeBuild applies, which resolve providers
fresh. The real repo's zip is held to what `tower-per-customer` reads out of it."""

import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BASH = shutil.which("bash")
TEMPLATE_DIRS = (REPO / "prod" / "init_customer", REPO / "prod" / "per_customer", REPO / "prod" / "hub")

sys.path.insert(0, str(REPO / "scripts"))
import deploy  # noqa: E402


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def _scratch():
    """A git repo holding zip.sh, with one of each case the listing gets wrong."""
    root = Path(tempfile.mkdtemp())
    (root / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "zip.sh", root / "scripts" / "zip.sh")
    for rel, text in {"tracked.txt": "t", "gone.txt": "g", ".gitignore": ".env\n.build/\n",
                      "prod/per_customer/.terraform.lock.hcl": "lock", "prod/tower/.terraform.lock.hcl": "lock",
                      "prod/tower/main.tf": "locals {}\n"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / "link").symlink_to("tracked.txt")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "first")
    (root / ".env").write_text("SECRET=1")
    (root / "untracked.txt").write_text("u")
    (root / "gone.txt").unlink()
    return root


def _zip_source(root, *args):
    return subprocess.run([BASH, str(root / "scripts" / "zip.sh"), "source", *args],
                          capture_output=True, text=True, timeout=60)


def test_the_source_zip_is_the_tree_git_sees():
    root = _scratch()
    try:
        out = _zip_source(root, "--dirs", "prod/tower")
        assert out.returncode == 0, out.stderr
        with zipfile.ZipFile(root / ".build" / "source.zip") as z:
            names = set(z.namelist())
            assert {"tracked.txt", "untracked.txt", "link", "prod/tower/.terraform.lock.hcl"} <= names, names
            assert ".env" not in names, "a gitignored file never rides"
            assert "gone.txt" not in names, "a tracked file deleted from the tree is left out"
            assert "prod/per_customer/.terraform.lock.hcl" not in names, "CodeBuild's roots resolve providers fresh"
            assert stat.S_ISLNK(z.getinfo("link").external_attr >> 16), "a symlink goes in as a link"
            meta = json.loads(z.read("source.json"))
            assert z.read("deploy-dirs.txt").decode() == "prod/tower\n"
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        assert meta == {"commit": head, "dirty": True, "dirs": ["prod/tower"]}, meta

        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "everything")
        assert _zip_source(root).returncode == 0
        with zipfile.ZipFile(root / ".build" / "source.zip") as z:
            assert json.loads(z.read("source.json"))["dirty"] is False
            assert z.read("deploy-dirs.txt") == b"", "no --dirs, nothing to deploy"
    finally:
        shutil.rmtree(root)


def test_an_unchanged_tree_zips_to_the_same_bytes():
    root = _scratch()
    try:
        assert _zip_source(root).returncode == 0
        first = (root / ".build" / "source.zip").read_bytes()
        assert _zip_source(root).returncode == 0
        assert (root / ".build" / "source.zip").read_bytes() == first
    finally:
        shutil.rmtree(root)


def test_outside_a_git_work_tree_the_source_zip_is_refused():
    root = Path(tempfile.mkdtemp())
    try:
        (root / "scripts").mkdir()
        shutil.copy(REPO / "scripts" / "zip.sh", root / "scripts" / "zip.sh")
        out = _zip_source(root)
        assert out.returncode != 0 and "not a git work tree" in out.stderr, out
        assert not (root / ".build" / "source.zip").exists()
    finally:
        shutil.rmtree(root)


def _referenced_modules():
    """Every local module source the CodeBuild templates refer to, and the sources those refer to."""
    out, todo, seen = set(), [t for d in TEMPLATE_DIRS for t in d.glob("*.tf")], set()
    while todo:
        tf = todo.pop()
        if tf in seen:
            continue
        seen.add(tf)
        for src in re.findall(r'source\s*=\s*"(\.\./[^"]+)"', tf.read_text()):
            target = (tf.parent / src).resolve()
            out.add(str(target.relative_to(REPO)))
            todo += list(target.glob("*.tf"))
    return out


def test_the_repos_source_zip_holds_what_codebuild_reads():
    """`terraform init` in CodeBuild fails on a module the zip lacks ('Unreadable module directory'),
    and `file()` fails on a missing repo-root file (config.json, 2026-09-04)."""
    out = subprocess.run([BASH, str(REPO / "scripts" / "zip.sh"), "source"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    with zipfile.ZipFile(REPO / ".build" / "source.zip") as z:
        names = z.namelist()
    dirs = {str(Path(n).parent) for n in names}
    for need in sorted(_referenced_modules()) + [str(d.relative_to(REPO)) for d in TEMPLATE_DIRS]:
        assert need in dirs, f"{need} is referenced by a CodeBuild template but not in the source zip"
    for f in (".codebuild/per-customer.yml", ".codebuild/hub.yml", "scripts/sync_playbooks.sh", "config.json"):
        assert f in names, f"{f} missing from the source zip"
    for d in TEMPLATE_DIRS:
        for tf in d.glob("*.tf"):
            for f in re.findall(r'file\("\$\{path\.module\}/\.\./\.\./([^"/]+)"\)', tf.read_text()):
                assert f in names, f"{tf.name} reads repo-root {f}, which the source zip does not carry"
    for kb in (REPO / "modules").glob("**/kb.md"):
        rel = str(kb.relative_to(REPO))
        if "node_modules" not in rel:
            assert rel in names, f"{rel} is a guide the per-customer build syncs, but not in the source zip"
    for root in ("prod/per_customer", "prod/init_customer", "prod/hub"):
        assert f"{root}/.terraform.lock.hcl" not in names


def test_a_lambda_zip_is_the_bytes_push_builds():
    src = "modules/payments/lambdas/ingest_stripe"
    out = subprocess.run([BASH, str(REPO / "scripts" / "zip.sh"), "lambda", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert (REPO / ".build" / "lambdas" / f"{src}.zip").read_bytes() == deploy.build_artifact(src)


def test_a_node_lambda_without_node_modules_is_refused():
    root = Path(tempfile.mkdtemp())
    try:
        (root / "scripts").mkdir()
        for f in ("zip.sh", "deploy.py"):
            shutil.copy(REPO / "scripts" / f, root / "scripts" / f)
        shutil.copy(REPO / "config.json", root / "config.json")
        fn = root / "modules" / "x" / "lambdas" / "y"
        fn.mkdir(parents=True)
        (fn / "package.json").write_text("{}")
        (fn / "package-lock.json").write_text("{}")
        out = subprocess.run([BASH, str(root / "scripts" / "zip.sh"), "lambda", "modules/x/lambdas/y"],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode != 0 and "npm ci" in out.stderr, out
        assert not (root / ".build" / "lambdas" / "modules/x/lambdas/y.zip").exists()
    finally:
        shutil.rmtree(root)


def test_the_bff_zip_is_the_bytes_push_builds():
    out = subprocess.run([BASH, str(REPO / "scripts" / "zip.sh"), "bff"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    blob = (REPO / ".build" / "bff.zip").read_bytes()
    assert blob == deploy.build_webapp()
    with zipfile.ZipFile(REPO / ".build" / "bff.zip") as z:
        names = set(z.namelist())
    assert "main.py" in names and {f"web/{f}" for f in deploy.BFF_FILES} <= names


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all zip tests passed")
