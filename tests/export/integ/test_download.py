"""Integration test — the export's download path, against DEPLOYED infra (gradienterp).

The one case the unit tests cannot hold: a person with no AWS configuration fetches the
presigned link and runs the script it points at. Three things fail only here — the presigned
URL's signing (SSE-KMS needs SigV4; boto3's default was refused with InvalidArgument until
someone clicked), the reader role's grant, and the credential baked into the script — so the
shell that runs the script has every AWS variable unset. Then the manifest's counts are held to
the tables. Reads only; leaves one export under the bucket's 30-day rule.

Run: `bash scripts/test.sh --env integ --module export` (or this file). Skips without the
customer profile.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

GERP = os.environ.get("EXPORT_INTEG_GERP", "gradienterp")
PROFILE = os.environ.get("EXPORT_INTEG_PROFILE", f"gerp-{GERP}")
FN = f"gerp-export-{GERP.replace('_', '-')}-export_gerp"

# manifest path -> the table it came from (the export names paths; the tables are named by module)
TABLE_OF = {"accounting/ledger.jsonl": f"gerp-accounting-{GERP}-ledger",
            "contacts.jsonl": f"gerp-contacts-{GERP}",
            "settings.jsonl": f"gerp-settings-{GERP}"}


def _session():
    import boto3
    return boto3.Session(profile_name=PROFILE, region_name="us-east-1")


def _invoke(ses, payload):
    r = ses.client("lambda").invoke(FunctionName=FN, Payload=json.dumps(payload).encode())
    out = json.loads(r["Payload"].read() or b"{}")
    assert not r.get("FunctionError"), out
    return out["statusCode"], json.loads(out.get("body") or "{}")


def test_the_script_fetched_by_link_downloads_the_export_with_no_aws_configuration():
    ses = _session()
    # a complete export: loop the resume the way the closure build does
    code, body = _invoke(ses, {})
    for _ in range(20):
        if code == 200:
            break
        assert code == 202, body
        time.sleep(5)
        code, body = _invoke(ses, {"resume": body["export_id"]})
    assert code == 200, body
    link = body["download"]["download.sh"]

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "download.sh"
        script.write_bytes(urllib.request.urlopen(link, timeout=30).read())   # the presigned fetch: no credential of ours
        clean = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
        clean["HOME"] = tmp   # no ~/.aws either: the credential must come from the file
        clean["PATH"] = os.environ["PATH"]
        run = subprocess.run(["bash", str(script), str(Path(tmp) / "out")], env=clean, capture_output=True, text=True, timeout=600)
        assert run.returncode == 0, run.stdout[-800:] + run.stderr[-800:]
        out = Path(tmp) / "out"
        manifest = json.loads((out / "manifest.json").read_text())
        assert (out / "README.md").exists()
        for path, count in manifest["tables"].items():
            f = out / path
            assert f.exists(), f"the manifest names {path} but the script did not fetch it"
            if path.endswith(".jsonl"):
                assert sum(1 for line in f.read_text().splitlines() if line.strip()) == count, path
        assert (out / "accounting" / "ledger.csv").exists(), "the ledger comes out as a CSV too"

        # the counts are the tables' — read live, through the profile, for the tables named here
        ddb = ses.client("dynamodb")
        for path, table in TABLE_OF.items():
            if path in manifest["tables"]:
                live = ddb.scan(TableName=table, Select="COUNT")["Count"]
                assert manifest["tables"][path] == live, f"{path}: manifest {manifest['tables'][path]}, table {live}"


def test_the_links_again_rewrite_the_scripts_and_make_no_second_copy():
    ses = _session()
    code, first = _invoke(ses, {"credentials_only": True})
    assert code == 200, first
    code, again = _invoke(ses, {"credentials_only": True})
    assert code == 200 and again["export"] == first["export"], "the same export, re-minted"
    assert again["download"]["download.sh"] != first["download"]["download.sh"], "a fresh credential, a fresh link"


if __name__ == "__main__":
    try:
        _session().client("sts").get_caller_identity()
    except Exception as e:  # noqa: BLE001
        print(f"skip: {PROFILE} does not resolve — integ test needs deployed infra ({e})")
        sys.exit(0)
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
            print(f"ok {_n}")
    print("all export download integ tests passed")
