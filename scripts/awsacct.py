"""awsacct — the AWS CLI profiles that reach each account of the organization, written from the
records that name the accounts.

Every profile is a role chain with no secret in it. `default` holds the management account's
credentials; `operator-org` assumes `OrganizationAccountAccessRole` in the operator account from
`default`; a hub or a gerp assumes `OperatorOrchestration` in its account from `operator-org`. The
account and the region come from `config.json` (`OPERATOR_ACCOUNT_ID`, `HUBS`) and from a gerp's row
on `gerp-customers` (`aws_account_id`, `region`), so nothing here is stored and nothing goes stale.

    bash scripts/awsacct.sh <target>   # [profile current] → the target; prints the caller identity
    bash scripts/awsacct.sh --list     # the targets, with account and region
    bash scripts/awsacct.sh --all      # a named profile per target: operator-org, hub-<region>, gerp-<gerp_id>

A target is `management`, `operator`, `hub:<region>` or a gerp id. `[profile current]` carries the
target it reaches as its first line (`# awsacct: <target>`).

The script owns `[profile current]`, `[profile operator-org]`, `[profile hub-<region>]` and every
`[profile gerp-…]` in the config file (`AWS_CONFIG_FILE`, else `~/.aws/config`). It rewrites those
where they stand, appends the ones missing, and leaves every other line of the file as it is. The
write is a rename, and a run that changes nothing leaves the file's bytes alone.

`current` is for reading. It is one file every process on the machine shares, so a command that
changes AWS names its account — `deploy.sh push --gerp <gerp_id>`.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = json.loads((REPO / "config.json").read_text())
CUSTOMERS_TABLE = "gerp-customers"
OPERATOR_REGION = "us-east-1"
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


def config_path() -> Path:
    return Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")


# ── the file: sections kept as their own lines ──────────────────────────────────────────────────

def split_sections(text: str) -> list[tuple[str | None, list[str]]]:
    """[(name, lines)] in file order; the lines before the first header have the name None. A
    section's lines are its header and everything up to the next header, blank lines included."""
    out: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in text.splitlines(keepends=True):
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if m:
            out.append((m.group(1).strip(), [line]))
        else:
            out[-1][1].append(line)
    return out


def owned(name: str | None) -> bool:
    if not name or not name.startswith("profile "):
        return False
    p = name[len("profile "):].strip()
    return (p in ("current", "operator-org") or p.startswith("gerp-")
            or (p.startswith("hub-") and bool(REGION_RE.match(p[len("hub-"):]))))


def render(profile: str, fields: list[tuple[str, str]]) -> list[str]:
    """A `#` key is a comment line: `current` carries the target it reaches."""
    return [f"[profile {profile}]\n", *(f"# {v}\n" if k == "#" else f"{k} = {v}\n" for k, v in fields), "\n"]


def merge(text: str, blocks: dict[str, list[tuple[str, str]]], prune: bool = False) -> str:
    """The file with each profile in `blocks` written where it stands, or appended in name order.
    With `prune`, an owned `gerp-`/`hub-` profile not in `blocks` is removed (a closed gerp)."""
    sections, seen, out = split_sections(text), set(), []
    for name, lines in sections:
        profile = name[len("profile "):].strip() if name and name.startswith("profile ") else None
        if profile in blocks:
            out.append(render(profile, blocks[profile]))
            seen.add(profile)
        elif prune and owned(name) and profile not in ("current", "operator-org"):
            continue
        else:
            out.append(lines)
    body = "".join("".join(lines) for lines in out)
    missing = sorted(p for p in blocks if p not in seen)
    if missing:
        if body and not body.endswith("\n"):
            body += "\n"
        if body and not body.endswith("\n\n"):
            body += "\n"
        body += "".join("".join(render(p, blocks[p])) for p in missing)
    return body


def write(blocks: dict[str, list[tuple[str, str]]], prune: bool = False) -> bool:
    path = config_path()
    before = path.read_text() if path.exists() else ""
    after = merge(before, blocks, prune)
    if after == before:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".awsacct")
    tmp.write_text(after)
    os.replace(tmp, path)
    return True


# ── the accounts ────────────────────────────────────────────────────────────────────────────────

def chain(account: str, region: str, role: str = "OperatorOrchestration",
          source: str = "operator-org") -> list[tuple[str, str]]:
    return [("role_arn", f"arn:aws:iam::{account}:role/{role}"), ("source_profile", source),
            ("region", region), ("output", "json")]


def operator_block() -> list[tuple[str, str]]:
    return chain(CONFIG["OPERATOR_ACCOUNT_ID"], OPERATOR_REGION, "OrganizationAccountAccessRole", "default")


def management_block() -> list[tuple[str, str]]:
    return [("credential_process", "aws configure export-credentials --profile default --format process"),
            ("region", OPERATOR_REGION), ("output", "json")]


def aws(*args: str) -> dict:
    r = subprocess.run(["aws", *args, "--output", "json", "--no-cli-pager"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"aws {' '.join(args[:2])} failed: {r.stderr.strip()[-600:]}")
    return json.loads(r.stdout or "{}")


def gerp_rows() -> list[dict]:
    """Every gerp with an account, as {gerp_id, account, region, status}, by gerp id."""
    out = aws("dynamodb", "scan", "--table-name", CUSTOMERS_TABLE, "--profile", "operator-org",
              "--region", OPERATOR_REGION, "--projection-expression", "gerp_id, aws_account_id, #r, #s",
              "--expression-attribute-names", '{"#r": "region", "#s": "status"}')
    rows = []
    for it in out.get("Items", []):
        account = it.get("aws_account_id", {}).get("S")
        if account and account != "None":
            rows.append({"gerp_id": it["gerp_id"]["S"], "account": account,
                         "region": it.get("region", {}).get("S") or OPERATOR_REGION,
                         "status": it.get("status", {}).get("S", "")})
    return sorted(rows, key=lambda r: r["gerp_id"])


def resolve(target: str, rows: list[dict] | None = None) -> tuple[str, list[tuple[str, str]], str]:
    """(named profile, its fields, the account it reaches) — the account is "" for management,
    whose id no record holds."""
    if target == "management":
        return "default", management_block(), ""
    if target == "operator":
        return "operator-org", operator_block(), CONFIG["OPERATOR_ACCOUNT_ID"]
    if target.startswith("hub:"):
        region = target[len("hub:"):]
        hub = CONFIG["HUBS"].get(region)
        if not hub:
            sys.exit(f"no hub in {region}: config.json HUBS has {', '.join(CONFIG['HUBS'])}")
        return f"hub-{region}", chain(hub["account"], hub["region"]), hub["account"]
    row = next((r for r in (rows if rows is not None else gerp_rows()) if r["gerp_id"] == target), None)
    if not row:
        sys.exit(f"no gerp {target} with an account on {CUSTOMERS_TABLE} — `--list` shows the targets")
    return f"gerp-{target}", chain(row["account"], row["region"]), row["account"]


# ── the verbs ───────────────────────────────────────────────────────────────────────────────────

def cmd_switch(target: str) -> None:
    write({"operator-org": operator_block()})
    _, fields, account = resolve(target)
    write({"current": [("#", f"awsacct: {target}"), *fields]})
    who = aws("sts", "get-caller-identity", "--profile", "current")
    print(f"current → {target}: {who.get('Account')} {who.get('Arn')}")
    if account and who.get("Account") != account:
        sys.exit(f"current answered from {who.get('Account')}, not {account}")


def cmd_list() -> None:
    write({"operator-org": operator_block()})
    print(f"{'management':<34}  {'(default)':<12}  {OPERATOR_REGION}")
    print(f"{'operator':<34}  {CONFIG['OPERATOR_ACCOUNT_ID']:<12}  {OPERATOR_REGION}")
    for region, hub in CONFIG["HUBS"].items():
        print(f"{'hub:' + region:<34}  {hub['account']:<12}  {hub['region']}")
    for r in gerp_rows():
        print(f"{r['gerp_id']:<34}  {r['account']:<12}  {r['region']}  {r['status']}")


def cmd_all() -> None:
    write({"operator-org": operator_block()})
    rows = gerp_rows()
    blocks = {"operator-org": operator_block()}
    for region in CONFIG["HUBS"]:
        name, fields, _ = resolve(f"hub:{region}")
        blocks[name] = fields
    for r in rows:
        name, fields, _ = resolve(r["gerp_id"], rows)
        blocks[name] = fields
    changed = write(blocks, prune=True)
    print(f"{len(blocks)} profiles {'written' if changed else 'already current'} in {config_path()}")


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if argv else 1)
    if argv[0] == "--list":
        cmd_list()
    elif argv[0] == "--all":
        cmd_all()
    elif argv[0].startswith("-"):
        sys.exit(f"unknown flag: {argv[0]}")
    else:
        cmd_switch(argv[0])


if __name__ == "__main__":
    main(sys.argv[1:])
