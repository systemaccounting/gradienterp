#!/usr/bin/env bash
# tf-lock.sh — one lock file per terraform directory, one version per provider, both platforms.
#
#   bash scripts/tf-lock.sh [--mirror <dir>] [--platforms darwin_amd64,linux_amd64] [<dir>…]
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
while (( $# )); do
  case "$1" in
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
  if out="$(cd "$d" && terraform providers lock -fs-mirror="$MIRROR" "${platform_args[@]}" 2>&1)"; then
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
