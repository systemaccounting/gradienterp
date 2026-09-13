"""A gerp's functions take their packages from the artifact bucket in the gerp's region.

`deploy.sh push` writes the us-east-1 bucket; S3 replication carries the object to every other
region's (`gerp-artifacts-<operator>-<region>`, prod/tower regions.tf). The push then updates the
function from the region's bucket, and only once the replica holds the checksum it wrote: the
same key with an older checksum is the previous push still there, and an update-function-code
against it would deploy old code and report success.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _run(code):
    out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'scripts'); import deploy;" + code],
                         cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-800:]
    return out.stdout.strip()


def test_the_first_region_keeps_the_bare_bucket_and_every_other_is_suffixed():
    assert _run("print(deploy.bucket_for('us-east-1') == deploy.BUCKET, deploy.bucket_for('') == deploy.BUCKET)") == "True True"
    assert _run("print(deploy.bucket_for('eu-west-1') == deploy.BUCKET + '-eu-west-1')") == "True"


def test_the_replica_is_waited_on_for_the_checksum_pushed_and_an_old_one_does_not_pass():
    code = '''
class S3:
    class exceptions:
        class NoSuchKey(Exception): pass
    def __init__(self, shas): self.shas, self.calls, self.buckets = list(shas), 0, set()
    def get_object_attributes(self, Bucket, Key, ObjectAttributes):
        self.calls += 1
        self.buckets.add(Bucket)
        sha = self.shas.pop(0) if len(self.shas) > 1 else self.shas[0]
        return {"VersionId": "v" + str(self.calls), "Checksum": {"ChecksumSHA256": sha}}
t = [0]
deploy.time = type("T", (), {"time": staticmethod(lambda: t[0]), "sleep": staticmethod(lambda s: t.__setitem__(0, t[0] + s))})
# the replica still holds the previous push for two reads, then carries the new one
s3 = S3(["old", "old", "new"])
print(deploy.replicated(s3, "k", "new", "b-eu"), s3.buckets == {"b-eu"})
# it never arrives: None, not the old version
print(deploy.replicated(S3(["old"]), "k", "new", "b-eu", seconds=10))
'''
    out = _run(code).split("\n")
    assert out[0] == "v3 True", "the version once the checksum matches, read from the region's bucket"
    assert out[1] == "None"



def test_a_build_carries_the_gerps_region():
    """`stop`/`start` started tower-per-customer without CUSTOMER_REGION, the buildspec defaulted to
    us-east-1, and Dublin's apply planned its eu-west-1 stack against us-east-1."""
    code = '''
class CB:
    def start_build(self, **kw):
        self.env = {v["name"]: v["value"] for v in kw["environmentVariablesOverride"]}
        return {"build": {"id": "p:1"}}
    def batch_get_builds(self, ids):
        return {"builds": [{"currentPhase": "COMPLETED", "buildComplete": True, "buildStatus": "SUCCEEDED", "logs": {}}]}
cb = CB()
class Op:
    def client(self, name):
        return cb
deploy._run_build(Op(), "dublin-x", "111", "apply", "eu-west-1")
print(cb.env["CUSTOMER_REGION"], cb.env["TF_ACTION"])
src = open("scripts/deploy.py").read()
print(src.count('row.get("region") or "us-east-1"'))
'''
    out = _run(code).splitlines()
    assert out[-2] == "eu-west-1 apply"
    assert out[-1] == "2", "stop and start both pass the row's region"

if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all deploy region bucket tests passed")
