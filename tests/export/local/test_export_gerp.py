"""What the export actually writes.

The coverage test says every table is accounted for. This says the accounting is honoured: the
default set goes out, the on-request pile does not unless asked, a SPLIT table sheds the rows that
are not the firm's, and the shapes a human needs are the shapes they get — invoices carrying their
lines, the ledger as a CSV, a manifest that can be checked against what arrived.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers import load_lambda, put_rows, read_export, scratch_env  # noqa: F401

# the smallest set that exercises every shape the exporter knows
TABLES = ("accounting-ledger", "accounting-balances", "accounting-pending",
          "invoicing-invoices", "invoicing-invoice-lines", "inventory-movements",
          "contacts", "rules-params", "payments-webhook-log")


REPO = Path(__file__).resolve().parents[3]


def _run(event=None):
    mod = load_lambda("export_gerp")
    resp = mod.handler(event or {}, None)
    body = json.loads(resp["body"])
    prefix = body["export"].split(f"/{'exports'}/", 1)[1]
    return body, read_export(f"exports/{prefix}")


def _seed():
    put_rows("contacts", [{"contact_id": "blue_bottle", "entity_type": "organization",
                           "name": "Blue Bottle"}])
    # the REAL stored shape, taken off a live row: both legs on ONE row, timestamp_ms, and
    # location nested under dimensions. An invented shape is how a passing test proves nothing.
    put_rows("accounting-ledger", [
        {"pk": "2026-01", "sk": "00000001767225600000#e2#0", "entry_id": "e2",
         "timestamp_ms": 1767225600000, "amount": 50, "memo": "second", "source": "seed",
         "debit_account": "CASH", "debit_account_type": "ASSET",
         "credit_account": "SALES_REVENUE", "credit_account_type": "REVENUE",
         "dimensions": {"location": "1"}},
        {"pk": "2026-01", "sk": "00000001767139200000#e1#0", "entry_id": "e1",
         "timestamp_ms": 1767139200000, "amount": 120, "memo": "first", "source": "seed",
         "debit_account": "ACCOUNTS_RECEIVABLE", "debit_account_type": "ASSET",
         "credit_account": "REVENUE_PENDING", "credit_account_type": "ASSET",
         "dimensions": {"location": "1"}},
    ])
    put_rows("invoicing-invoices", [{"invoice_id": "inv-1", "customer": "blue_bottle",
                                     "status": "issued", "subtotal": 120}])
    put_rows("invoicing-invoice-lines", [
        {"invoice_id": "inv-1", "sk": "item#beans#range#1", "description": "beans", "amount": 120}])
    put_rows("inventory-movements", [
        {"item_id": "1#beans", "mv_sk": "2026-02-02#b", "qty": 2},
        {"item_id": "1#beans", "mv_sk": "2026-01-01#a", "qty": 5},
    ])
    put_rows("rules-params", [
        {"pk": "GENERAL", "sk": "fed_income_tax#2026", "rate": 22},
        {"pk": "worker_42", "sk": "sui_rate#2026", "rate": 3},
    ])
    put_rows("payments-webhook-log", [{"pk": "stripe#evt_1", "seen_at": 1}])


def _lines(text):
    return [json.loads(l) for l in text.strip().splitlines() if l.strip()]


def test_no_arguments_takes_everything():
    """No selection is no restriction. Quietly omitting part of someone's books is the one failure
    an export cannot have, so the default is complete and `include` narrows."""
    with scratch_env(TABLES):
        _seed()
        body, files = _run()

        assert "contacts/contacts.jsonl" in files
        assert "accounting/ledger.jsonl" in files
        assert "payments/webhook-log.jsonl" in files, "everything means everything"
        assert body["rows"] > 0


def test_include_narrows_rather_than_adds():
    """`include` is a selection, not an addition: naming two tables takes those two and not the
    rest."""
    with scratch_env(TABLES):
        _seed()
        _, files = _run({"include": ["contacts", "payments-webhook-log"]})
        assert "contacts/contacts.jsonl" in files
        assert "payments/webhook-log.jsonl" in files
        assert "accounting/ledger.jsonl" not in files, "naming tables excludes the unnamed ones"


def test_a_name_the_export_does_not_know_is_refused_not_dropped():
    """The owner asked for "ledger, contacts and settings" and the agent passed `ledger`, which is
    not a table name; the export took contacts and settings and left the books out (westwood,
    2026-09-05). An unknown name is refused with the names that exist, nothing is planned, and
    the caller asks again with a name from the list."""
    with scratch_env(TABLES):
        _seed()
        mod = load_lambda("export_gerp")
        out = mod.handler({"include": ["ledger", "contacts", "settings"]}, None)
        assert out["statusCode"] == 400
        body = json.loads(out["body"])
        assert "ledger" in body["error"] and "accounting-ledger" in body["tables"] and "schema" not in body["tables"]
        assert mod._jobs().scan().get("Items", []) == [], "nothing was planned"


def test_the_tool_schema_lists_exactly_the_tables_include_accepts():
    """The agent can only name what the schema tells it; the list there and the policy here are
    one list, or the agent guesses and gets refused."""
    import re
    src = (REPO / "modules/export/lambdas/export_gerp/main.py").read_text()
    i = src.index("_TABLES = {"); j = src.index("\n}\n", i)
    policy = {k for k, v in re.findall(r'"([a-z0-9-]+)":\s+(BOOKS|EXHAUST|SPLIT|EXCLUDED)', src[i:j]) if v != "EXCLUDED"}
    consumed = set(re.findall(r'_CONSUMED_BY = \{"([a-z0-9-]+)"', src))
    desc = json.loads((REPO / "modules/export/lambdas/export_gerp/schema.json").read_text())["properties"]["include"]["description"]
    listed = set(re.findall(r"\b[a-z]+(?:-[a-z]+)+\b|\b(?:contacts|settings|notes|tasks|assets|agreements|shipping)\b", desc.split("by their exact names: ")[1].split(". A name")[0]))
    assert listed == policy - consumed, f"schema lists {sorted(listed ^ (policy - consumed))} differently from the policy"


def test_a_split_table_sheds_the_rows_that_are_not_theirs():
    """rules-params holds the law under pk=GENERAL — bracket tables seeded from canonical, identical
    in every gerp — and the firm's own params under a contact_id. Only the second is theirs."""
    with scratch_env(TABLES):
        _seed()
        _, files = _run()
        rows = _lines(files["rules/params.jsonl"])
        assert [r["pk"] for r in rows] == ["worker_42"]


def test_invoices_carry_their_lines():
    """The lines live in their own table keyed by invoice_id — right for querying, useless to a
    human handed two files to rejoin by hand."""
    with scratch_env(TABLES):
        _seed()
        _, files = _run()
        [inv] = _lines(files["invoicing/invoices.jsonl"])
        assert [l["description"] for l in inv["lines"]] == ["beans"]


def test_the_ledger_csv_has_one_row_per_leg():
    """A stored entry carries BOTH sides on one row; a general ledger shows one line per leg with
    debit and credit columns, which is what a spreadsheet expects and what an accountant reads."""
    with scratch_env(TABLES):
        _seed()
        _, files = _run()
        header, *rows = files["accounting/ledger.csv"].strip().splitlines()
        assert header == "date,entry_id,account,account_type,debit,credit,memo,source,location"

        cells = [r.split(",") for r in rows]
        assert [c[1] for c in cells] == ["e1", "e1", "e2", "e2"], "sk order, two legs each"
        # e1: DR ACCOUNTS_RECEIVABLE 120 / CR REVENUE_PENDING 120
        assert cells[0][2:6] == ["ACCOUNTS_RECEIVABLE", "ASSET", "120", ""]
        assert cells[1][2:6] == ["REVENUE_PENDING", "ASSET", "", "120"]
        assert cells[0][0] == "2025-12-31" or cells[0][0].startswith("20"), "a real date"
        assert cells[0][8] == "1", "location comes out of dimensions"


def test_movements_are_in_date_order():
    """The movement log IS the stock history, and a history out of order is a pile."""
    with scratch_env(TABLES):
        _seed()
        _, files = _run()
        rows = _lines(files["inventory/movements.jsonl"])
        assert [r["mv_sk"] for r in rows] == ["2026-01-01#a", "2026-02-02#b"]


def test_the_manifest_counts_what_was_written():
    """An export nobody can check is not evidence: the counts have to come from what landed, so
    what arrived can be compared against what was there."""
    with scratch_env(TABLES):
        _seed()
        _, files = _run()
        manifest = json.loads(files["manifest.json"])
        assert manifest["gerp_id"] == "gradienterp"
        assert manifest["tables"]["contacts/contacts.jsonl"] == 1
        assert manifest["tables"]["accounting/ledger.jsonl"] == 2
        assert "schema" in manifest["excluded"]
        # every counted file is actually present, and vice versa for the jsonl set
        for path, count in manifest["tables"].items():
            assert len(_lines(files[path])) == count, path


def test_the_readme_says_when_an_export_was_narrowed():
    """A complete export says nothing about narrowing; a narrowed one has to say so, or it reads
    as everything the firm has and quietly is not."""
    with scratch_env(TABLES):
        _seed()
        _, full = _run()
        assert "narrowed" not in full["README.md"]

        _, part = _run({"include": ["contacts"]})
        assert "this export was narrowed" in part["README.md"]
        assert "- contacts" in part["README.md"]


def test_an_export_does_not_contain_the_previous_export():
    """Skipping our own prefix, or export two contains export one and export three contains both."""
    with scratch_env(TABLES):
        _seed()
        _run()
        _, files = _run()
        assert not [k for k in files if k.startswith("storage/exports/")], \
            "the exporter copied its own output into itself"


def test_documents_come_across_under_their_original_keys():
    with scratch_env(TABLES):
        _seed()
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "modules" / "aws"))
        from aws import client
        from _helpers import BUCKET
        client("s3").put_object(Bucket=BUCKET, Key="receipts/2026/coffee.pdf", Body=b"%PDF-1.4")
        # AgentCore's probe at browser creation, in every gerp's bucket: not a document
        client("s3").put_object(Bucket=BUCKET, Key="browse-recordings/BrowserRecordingTestFile", Body=b"")
        # a real recording under the same prefix is the firm's audit artifact, and comes across
        client("s3").put_object(Bucket=BUCKET, Key="browse-recordings/2026-09-05/session.webm", Body=b"\x1a\x45")

        body, files = _run()
        assert body["documents"] == 2
        assert files["storage/receipts/2026/coffee.pdf"].startswith("%PDF")
        assert "storage/browse-recordings/2026-09-05/session.webm" in files
        assert "storage/browse-recordings/BrowserRecordingTestFile" not in files


def test_a_table_this_gerp_does_not_have_is_recorded_not_invented():
    """A gerp need not have every module deployed. An export that dies on one absent table is worse
    than one that reports what it found — but "no file" and "a file with no rows" mean different
    things, and only one of them is an answer. The manifest has to say which."""
    with scratch_env(TABLES):
        _seed()
        _, files = _run()
        manifest = json.loads(files["manifest.json"])

        # tasks/notes/labor/etc were never created in this scratch env
        assert "tasks" in manifest["not_present"]
        assert "tasks/tasks.jsonl" not in files, "an absent table must not become an empty file"

        # and a table that IS there but holds nothing gets a file with no rows — the opposite claim
        assert manifest["tables"]["accounting/pending.jsonl"] == 0
        assert files["accounting/pending.jsonl"] == ""


def test_an_unfinished_export_is_not_offered_for_download():
    """A run that times out mid-way leaves table files and no manifest. Handing someone credentials
    to that is worse than saying there is no export: it looks like their books and is missing
    whatever came after the minute it died."""
    with scratch_env(TABLES):
        _seed()
        mod = load_lambda("export_gerp")
        _run()                                    # one good export
        good = mod._latest_export()
        assert good

        # a later prefix with content but no manifest — what a timeout leaves behind
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "modules" / "aws"))
        from aws import client
        from _helpers import BUCKET
        client("s3").put_object(Bucket=BUCKET, Key="exports/2099-01-01T00-00-00Z/contacts/contacts.jsonl",
                                Body=b'{"contact_id":"x"}\n')

        assert mod._latest_export() == good, "an unfinished export must not be the one handed out"


class _Clock:
    """A Lambda context that runs out of time after N units, so a resume can be tested without
    waiting fifteen minutes for a real one."""

    def __init__(self, budget_ms, drain):
        self.left, self.drain = budget_ms, drain

    def get_remaining_time_in_millis(self):
        now, self.left = self.left, self.left - self.drain
        return now


def test_a_run_that_runs_out_of_time_reports_incomplete_and_hands_out_nothing():
    """Stopping early is a normal outcome, not a failure — but an unfinished export must not be
    offered as if it were one, which is why no credentials come back with a 202."""
    with scratch_env(TABLES):
        _seed()
        mod = load_lambda("export_gerp")
        # 90s of budget against a 60s reserve: one unit runs, then it stops
        resp = mod.handler({}, _Clock(90_000, 15_000))

        assert resp["statusCode"] == 202
        body = json.loads(resp["body"])
        assert body["status"] == "incomplete"
        assert body["units_left"] > 0 and body["units_done"] >= 1
        assert "download" not in body, "nothing complete to hand anyone yet"
        assert mod._latest_export() is None, "an unfinished export is not the latest one"


def test_resuming_finishes_it_and_does_not_redo_stamped_work():
    with scratch_env(TABLES):
        _seed()
        mod = load_lambda("export_gerp")
        first = json.loads(mod.handler({}, _Clock(90_000, 15_000))["body"])
        export_id = first["export_id"]
        done_after_first = first["units_done"]

        resp = mod.handler(first["resume"], None)     # no context = all the time in the world
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200 and body["status"] == "complete"
        assert body["export_id"] == export_id, "resume continues the same export, not a new one"
        assert body["units"] >= done_after_first
        assert body["download"]["download.sh"], "a finished export hands over a link"

        # and it is now the one handed out
        assert mod._latest_export() == f"exports/{export_id}"
        files = read_export(f"exports/{export_id}")
        assert "manifest.json" in files and "download.sh" in files
        manifest = json.loads(files["manifest.json"])
        assert manifest["units"] == body["units"]


def test_a_second_unit_is_not_redone_on_resume():
    """`done_at` is the whole coordination mechanism: a stamped unit is skipped, no lease, no lock,
    no distinguishing crashed from slow."""
    with scratch_env(TABLES):
        _seed()
        mod = load_lambda("export_gerp")
        first = json.loads(mod.handler({}, _Clock(90_000, 15_000))["body"])
        stamped = [u for u in mod._units(first["export_id"]) if u.get("done_at")]
        assert stamped, "the first pass stamped something"

        ran = []
        real = mod._run_unit
        mod._run_unit = lambda out, row: ran.append(row["unit"]) or real(out, row)
        mod.handler(first["resume"], None)

        assert not (set(ran) & {u["unit"] for u in stamped}), "a stamped unit was redone"


def test_resuming_an_unknown_export_is_404():
    with scratch_env(TABLES):
        mod = load_lambda("export_gerp")
        resp = mod.handler({"resume": "2099-01-01T00-00-00Z"}, None)
        assert resp["statusCode"] == 404


def test_re_minting_rewrites_the_scripts_rather_than_re_exporting():
    """This path exists because the credentials inside a script expired. It rewrites the scripts
    with fresh ones and hands back new links — it does NOT make a second copy of the books to
    deliver the same thing."""
    with scratch_env(TABLES):
        _seed()
        mod = load_lambda("export_gerp")
        first, files = _run()
        before = files["download.sh"]

        resp = mod.handler({"credentials_only": True}, None)
        body = json.loads(resp["body"])
        assert body["download"]["download.sh"], "a link, not a credential"
        assert body["export"] == first["export"], "the same export, not a new one"

        after = read_export(f"exports/{first['export_id']}")["download.sh"]
        assert after != before, "the script was rewritten with fresh credentials"


def test_the_conversation_carries_a_link_and_the_script_carries_the_credentials():
    """Credentials pasted into a chat live in the transcript forever. A link fetches a script that
    already has them, so the person runs one thing with nothing to configure — and the link is
    presigned, or reaching the download instructions would need the very credentials they deliver."""
    with scratch_env(TABLES):
        _seed()
        body, files = _run()

        assert set(body["download"]) == {"download.sh", "download.ps1"}
        assert "credentials" not in body, "the chat gets a link, not a secret"

        script = files["download.sh"]
        assert "export AWS_ACCESS_KEY_ID=" in script and "AWS_SESSION_TOKEN" in script
        assert "aws s3 sync" in script
        assert files["download.ps1"].startswith("# Download your gradientERP export.")


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all export tests passed")
