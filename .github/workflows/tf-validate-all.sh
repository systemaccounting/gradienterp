#!/usr/bin/env bash
# tf-validate-all.sh — terraform validate every dir containing .tf files.
#
# CI hygiene: catches accidental syntax breakage across dirs. Safe to run
# anywhere — uses `terraform init -backend=false` (no network state ops).
#
# .github/workflows/terraform.yaml runs it on every pull request and push to main.
#
# The directories run in parallel, one per core (TF_VALIDATE_JOBS overrides): each one's output is
# held and printed in directory order, so the log reads the same as a serial run. Workers share no
# write — a root's .terraform and its alias-provider file are its own, and the provider mirror the
# workflow points init at is only read.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JOBS="${TF_VALIDATE_JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)}"

# Find every dir that contains at least one .tf file. Exclude vendored/cached state.
mapfile -t tf_dirs < <(find "$REPO_ROOT" \
  \( -path '*/.terraform' -o -path '*/.venv' -o -path '*/__pycache__' -o -path '*/terraform.tfstate.d' -o -path '*/.git' \) -prune -o \
  -name '*.tf' -type f -print | xargs -n1 dirname | sort -u)

# A module that takes an aliased provider from its caller (`configuration_aliases`) has no
# configuration for that alias when validated on its own, and terraform refuses it. For the length
# of its validate it gets one empty provider block per alias, in a file removed straight after.
ALIAS_FILE="zz_validate_providers.tf"
OUT="$(mktemp -d)"
trap 'for d in "${tf_dirs[@]}"; do rm -f "$d/$ALIAS_FILE"; done; rm -rf "$OUT"' EXIT

# one directory: its log to $OUT/<i>.out, and what failed (init | validate | nothing) to $OUT/<i>.rc
validate_one() {
  local i=$1 tf_dir=$2 rel="${2#$REPO_ROOT/}" init_out aliases a
  {
    echo "=== ${rel} ==="
    cd "$tf_dir" || { echo init >"$OUT/$i.rc"; return; }
    # one retry: a provider download that drops is not a broken directory
    if ! init_out="$(terraform init -backend=false -input=false -no-color 2>&1)" &&
       ! init_out="$(terraform init -backend=false -input=false -no-color 2>&1)"; then
      echo "    init failed"
      printf '%s\n' "$init_out" | grep -A8 '^Error' | sed 's/^/    /'
      echo init >"$OUT/$i.rc"
      return
    fi
    aliases="$(grep -ho 'configuration_aliases *= *\[[^]]*\]' ./*.tf 2>/dev/null | grep -o '[a-z0-9_-]*\.[a-z0-9_-]*' | sort -u)"
    if [[ -n "$aliases" ]]; then
      for a in $aliases; do printf 'provider "%s" {\n  alias = "%s"\n}\n' "${a%%.*}" "${a#*.}"; done >"$ALIAS_FILE"
    fi
    if terraform validate -no-color; then echo "" >"$OUT/$i.rc"; else echo validate >"$OUT/$i.rc"; fi
    rm -f "$ALIAS_FILE"
  } >"$OUT/$i.out" 2>&1
}

for i in "${!tf_dirs[@]}"; do
  while (( $(jobs -rp | wc -l) >= JOBS )); do wait -n; done
  validate_one "$i" "${tf_dirs[$i]}" &
done
wait

failed_dirs=()
for i in "${!tf_dirs[@]}"; do
  cat "$OUT/$i.out"
  what="$(cat "$OUT/$i.rc" 2>/dev/null || echo init)"
  [[ -n "$what" ]] && failed_dirs+=("${tf_dirs[$i]#$REPO_ROOT/} ($what)")
done

total=${#tf_dirs[@]}
echo
if [ ${#failed_dirs[@]} -eq 0 ]; then
  echo "all $total dirs valid"
  exit 0
else
  echo "${#failed_dirs[@]} of $total dirs failed:"
  for d in "${failed_dirs[@]}"; do
    echo "  - $d"
  done
  exit 1
fi
