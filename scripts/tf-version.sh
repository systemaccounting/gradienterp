#!/usr/bin/env bash
# tf-version.sh — terraform's version and the providers': what is pinned, the change, the lock files.
#
#   bash scripts/tf-version.sh [--mirror <dir>] [--platforms darwin_amd64,linux_amd64] [<dir>…]
#   bash scripts/tf-version.sh --summary     # what is pinned where, no downloads: terraform's version in
#                                         # each place that names one, each provider's constraints and
#                                         # locked versions with counts, the platforms each lock file
#                                         # carries, any directory without one
#   bash scripts/tf-version.sh --terraform 1.11.0 --pin aws='~> 6.66' [--pin archive='~> 2.9' …]
#                                         # the change: TF_VERSION in config.json, which the buildspecs
#                                         # and the workflows read, and one constraint for the provider
#                                         # in every required_providers entry (added where an entry has
#                                         # none). Then the lock files, from tf-lock.yaml on a runner
#
# The tree's required_providers blocks name each provider's constraint. The script requires them all
# from one throwaway root and mirrors each provider once for the platforms (`terraform providers
# mirror`), then writes every directory's lock file from that mirror (`terraform providers lock
# -fs-mirror`): the zh: checksums from the zips, an h1: per platform. Two constraints for one
# provider stop it before any download, naming both; a directory that resolves a version the mirror
# lacks fails, naming the directory. That is the check that every directory is on one version, and
# the lock files are what the runner's plugin cache verifies against (.github/workflows/terraform.yaml).
#
# --mirror keeps the mirror where the caller says (tf-lock.yaml validates against it next); the
# default is a temp dir removed at the end. Directories default to every one holding a .tf file.
#
# `.github/workflows/tf-lock.yaml` runs it on a runner and pushes the lock files back, so the
# downloads never touch a laptop.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORMS="darwin_amd64,linux_amd64"
MIRROR=""
dirs=()
SUMMARY=false
TF_SET=""
pins=()
while (( $# )); do
  case "$1" in
    --summary) SUMMARY=true; shift ;;
    --terraform) TF_SET="$2"; shift 2 ;;
    --pin) pins+=("$2"); shift 2 ;;
    --mirror) MIRROR="$2"; shift 2 ;;
    --platforms) PLATFORMS="$2"; shift 2 ;;
    --*) sed -n '4p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
    *) dirs+=("$1"); shift ;;
  esac
done
if (( ${#dirs[@]} == 0 )); then
  mapfile -t dirs < <(find "$REPO_ROOT" \
    \( -path '*/.terraform' -o -path '*/.venv' -o -path '*/__pycache__' -o -path '*/terraform.tfstate.d' -o -path '*/.git' -o -path '*/node_modules' \) -prune -o \
    -name '*.tf' -type f -print | xargs -n1 dirname | sort -u)
fi
platform_args=()
for p in ${PLATFORMS//,/ }; do platform_args+=(-platform="$p"); done

if [[ -n "$TF_SET" || ${#pins[@]} -gt 0 ]]; then
  python3 - "$REPO_ROOT" "$TF_SET" "${pins[@]}" -- "${dirs[@]}" <<'PY'
import glob, json, os, re, sys
root, tf_set, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
pins, dirs = rest[:rest.index("--")], rest[rest.index("--") + 1:]
os.chdir(root)
if tf_set:
    if not re.fullmatch(r"\d+\.\d+\.\d+", tf_set):
        sys.exit(f"--terraform takes a version like 1.11.0, not {tf_set!r}")
    cfg = json.load(open("config.json"))
    was = cfg.get("TF_VERSION")
    text = open("config.json").read()
    if was:
        text = text.replace(f'"TF_VERSION": "{was}"', f'"TF_VERSION": "{tf_set}"', 1)
    else:
        text = text.replace("{\n", f'{{\n  "TF_VERSION": "{tf_set}",\n', 1)
    open("config.json", "w").write(text)
    print(f"==> terraform {was or '(none)'} → {tf_set}  (config.json TF_VERSION)")
targets = {}
for pin in pins:
    if "=" not in pin:
        sys.exit(f"--pin takes provider=constraint, e.g. aws='~> 6.66', not {pin!r}")
    name, cons = pin.split("=", 1)
    targets[name if "/" in name else "hashicorp/" + name] = cons.strip()
ENTRY = re.compile(r'(\w+)(\s*=\s*\{)([^{}]*)(\})')
for src, cons in targets.items():
    changed = []
    for d in dirs:
        for tf in sorted(glob.glob(os.path.join(d, "*.tf"))):
            text = open(tf).read()
            if "required_providers" not in text:
                continue
            def fix(e):
                body = e.group(3)
                m = re.search(r'source\s*=\s*"([^"]+)"', body)
                if not m:
                    return e.group(0)
                s_ = m.group(1) if "/" in m.group(1) else "hashicorp/" + m.group(1)
                if s_ != src:
                    return e.group(0)
                if re.search(r'version\s*=\s*"', body):
                    body2 = re.sub(r'(version\s*=\s*)"[^"]+"', lambda v: f'{v.group(1)}"{cons}"', body)
                elif "\n" in body:                       # a multi-line entry: version on its own line
                    indent = re.match(r"\s*", body.split("\n", 1)[1]).group(0) if "\n" in body.strip("\n") else "      "
                    body2 = body.rstrip() + f'\n{indent}version = "{cons}"\n' + body[len(body.rstrip()):].lstrip("\n")
                else:                                     # one line: `{ source = "…" }`
                    body2 = body.rstrip() + f', version = "{cons}" '
                return e.group(1) + e.group(2) + body2 + e.group(4)
            new = ENTRY.sub(fix, text)
            if new != text:
                open(tf, "w").write(new); changed.append(os.path.relpath(tf))
    print(f"==> {src} = {cons!r} in {len(changed)} file(s)" + (": " + ", ".join(changed) if changed else ""))
PY
  (cd "$REPO_ROOT" && terraform fmt -recursive -list=false modules prod tests 2>/dev/null || true)
  echo "==> next: commit, then \`gh workflow run tf-lock.yaml -f ref=<branch>\` writes the lock files"
  exit 0
fi

if $SUMMARY; then
  python3 - "$REPO_ROOT" "${dirs[@]}" <<'PY'
import collections, glob, json, os, re, subprocess, sys
root, dirs = sys.argv[1], sys.argv[2:]
os.chdir(root)
rel = lambda p: os.path.relpath(p, root)

print("==> terraform")
cfg = json.load(open("config.json")).get("TF_VERSION", "")
print(f"    config.json TF_VERSION            {cfg or '(none)'}")
for f in [".codebuild/per-customer.yml", ".codebuild/hub.yml", ".github/workflows/terraform.yaml",
          ".github/workflows/tf-lock.yaml", ".github/workflows/apply.yaml"]:
    if not os.path.exists(f):
        continue
    t = open(f).read()
    pinned = re.findall(r'(?:TF_VERSION|terraform_version):\s*"([^"]+)"', t)
    reads = any("TF_VERSION" in line and "config.json" in line for line in t.splitlines())
    print(f"    {f:34}{'config.json' if reads else ', '.join(pinned) or '(none)'}")
try:
    local = json.loads(subprocess.run(["terraform", "version", "-json"], capture_output=True, text=True).stdout)["terraform_version"]
except Exception:  # noqa: BLE001
    local = "(no terraform on the path)"
print(f"    this machine                      {local}")
req = collections.Counter()
for d in dirs:
    for tf in glob.glob(os.path.join(d, "*.tf")):
        for m in re.finditer(r'required_version\s*=\s*"([^"]+)"', open(tf).read()):
            req[m.group(1)] += 1
print("    required_version                  " + ", ".join(f"{k} ×{n}" for k, n in sorted(req.items())))

print("==> providers, constrained")
cons = collections.defaultdict(collections.Counter)
for d in dirs:
    for tf in glob.glob(os.path.join(d, "*.tf")):
        t = open(tf).read()
        if "required_providers" not in t:
            continue
        for e in re.finditer(r'(\w+)\s*=\s*\{([^{}]*)\}', t):
            b = e.group(2)
            src = re.search(r'source\s*=\s*"([^"]+)"', b)
            if not src or not re.fullmatch(r"([a-z0-9-]+/)?[a-z0-9-]+", src.group(1)):
                continue
            ver = re.search(r'version\s*=\s*"([^"]+)"', b)
            cons[src.group(1) if "/" in src.group(1) else "hashicorp/" + src.group(1)][ver.group(1) if ver else "unconstrained"] += 1
for src, c in sorted(cons.items()):
    print(f"    {src:22}" + ", ".join(f"{k} ×{n}" for k, n in sorted(c.items())))

print(f"==> providers, locked ({len(dirs)} directories)")
locked = collections.defaultdict(collections.Counter)
plat = collections.defaultdict(collections.Counter)
missing = []
for d in dirs:
    lock = os.path.join(d, ".terraform.lock.hcl")
    if not os.path.isfile(lock):
        missing.append(rel(d)); continue
    for block in re.split(r'^provider "', open(lock).read(), flags=re.M)[1:]:
        addr = block.split('"', 1)[0].replace("registry.terraform.io/", "")
        ver = re.search(r'^  version\s*=\s*"([^"]+)"', block, re.M)
        locked[addr][ver.group(1) if ver else "?"] += 1
        plat[addr][f"{block.count('h1:')} h1"] += 1
for addr, c in sorted(locked.items()):
    print(f"    {addr:22}" + ", ".join(f"{k} ×{n}" for k, n in sorted(c.items()))
          + "   (" + ", ".join(f"{k} ×{n}" for k, n in sorted(plat[addr].items())) + ")")
if missing:
    print(f"    no lock file: {', '.join(missing)}")
PY
  exit 0
fi

cleanup=""
if [[ -z "$MIRROR" ]]; then MIRROR="$(mktemp -d)"; cleanup="$MIRROR"; fi
ROOT="$(mktemp -d)"
trap '[[ -n "$cleanup" ]] && rm -rf "$cleanup"; rm -rf "$ROOT"' EXIT

# every provider the tree constrains, one constraint each — two for one provider is the thing to fix
python3 - "$ROOT" "${dirs[@]}" <<'PY'
import glob, os, re, sys
root, dirs = sys.argv[1], sys.argv[2:]
seen = {}   # source → {constraint: [files]}
for d in dirs:
    for tf in sorted(glob.glob(os.path.join(d, "*.tf"))):
        text = open(tf).read()
        if "required_providers" not in text:
            continue
        # an entry is `name = { source = "…", version = "…" }`, one line or several; the only
        # `source =` inside braces in a .tf file is a required_providers entry
        for entry in re.finditer(r'(\w+)\s*=\s*\{([^{}]*)\}', text):
            body = entry.group(2)
            src = re.search(r'source\s*=\s*"([^"]+)"', body)
            ver = re.search(r'version\s*=\s*"([^"]+)"', body)
            if not src or not re.fullmatch(r"([a-z0-9-]+/)?[a-z0-9-]+", src.group(1)):
                continue   # a `source = "$.detail…"` in an input_paths map is not a provider
            source = src.group(1) if "/" in src.group(1) else "hashicorp/" + src.group(1)
            seen.setdefault(source, {}).setdefault(ver.group(1) if ver else "", []).append(os.path.relpath(tf))
bad = {s: c for s, c in seen.items() if len([k for k in c if k]) > 1}
if bad:
    for s, c in sorted(bad.items()):
        print(f"{s} is constrained two ways:", file=sys.stderr)
        for k, files in sorted(c.items()):
            if k:
                print(f"  {k!r}: {', '.join(files)}", file=sys.stderr)
    sys.exit("one constraint per provider, in every required_providers block")
lines = []
for s, c in sorted(seen.items()):
    k = next((k for k in c if k), "")
    name = s.split("/")[-1]
    lines.append(f'    {name} = {{ source = "{s}"' + (f', version = "{k}"' if k else "") + " }")
open(os.path.join(root, "main.tf"), "w").write("terraform {\n  required_providers {\n" + "\n".join(lines) + "\n  }\n}\n")
print("==> providers: " + ", ".join(f"{s.split('/')[-1]} {next((k for k in c if k), 'any')}" for s, c in sorted(seen.items())))
PY

echo "==> mirroring for ${PLATFORMS} into ${MIRROR#$REPO_ROOT/}"
(cd "$ROOT" && terraform providers mirror "${platform_args[@]}" "$MIRROR" | grep -E "^- (Downloading|Package)" | sed 's/^/    /' || true)

# every directory's lock file from the mirror, four at a time; a failure names its directory
lock_one() {
  local d="$1" rel="${1#$REPO_ROOT/}" out
  rm -f "$d/.terraform.lock.hcl"
  # a directory's provider requirements include its child modules' (`module "fn"`), which have to
  # be installed to be read: `get` fetches the local ones and touches no provider and no backend
  if out="$(cd "$d" && terraform get -no-color 2>&1 && terraform providers lock -fs-mirror="$MIRROR" "${platform_args[@]}" 2>&1)"; then
    echo "    $rel: $(awk '/^provider/ { gsub(/"|registry.terraform.io\/hashicorp\//, "", $2); p = $2 } /^  version/ { gsub(/"/, "", $3); printf "%s %s  ", p, $3 }' "$d/.terraform.lock.hcl")"
  else
    echo "    $rel: FAILED"; printf '%s\n' "$out" | grep -E "Error|version|constraint" | sed 's/^/        /'
    return 1
  fi
}
echo "==> locking ${#dirs[@]} directories"
failed=0
for d in "${dirs[@]}"; do
  lock_one "$d" || failed=$((failed + 1))
done
if (( failed )); then echo "$failed of ${#dirs[@]} directories did not lock" >&2; exit 1; fi
echo "==> ${#dirs[@]} lock files written for ${PLATFORMS}"
