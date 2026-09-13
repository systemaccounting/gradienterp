#!/usr/bin/env bash
# tf-validate-all.sh — terraform validate every dir containing .tf files.
#
# CI hygiene: catches accidental syntax breakage across dirs. Safe to run
# anywhere — uses `terraform init -backend=false` (no network state ops).
#
# .github/workflows/terraform.yaml runs it on every pull request.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

failures=0
total=0
failed_dirs=()

# Find every dir that contains at least one .tf file. Exclude vendored/cached state.
mapfile -t tf_dirs < <(find "$REPO_ROOT" \
  \( -path '*/.terraform' -o -path '*/.venv' -o -path '*/__pycache__' -o -path '*/terraform.tfstate.d' -o -path '*/.git' \) -prune -o \
  -name '*.tf' -type f -print | xargs -n1 dirname | sort -u)

# A module that takes an aliased provider from its caller (`configuration_aliases`) has no
# configuration for that alias when validated on its own, and terraform refuses it. For the length
# of its validate it gets one empty provider block per alias, in a file removed straight after.
ALIAS_FILE="zz_validate_providers.tf"
alias_file=""
trap '[[ -n "$alias_file" ]] && rm -f "$alias_file"' EXIT

for tf_dir in "${tf_dirs[@]}"; do
  total=$((total+1))
  rel="${tf_dir#$REPO_ROOT/}"
  echo "=== ${rel} ==="
  cd "$tf_dir"
  # one retry: a provider download that drops is not a broken directory
  if ! terraform init -backend=false -input=false -no-color >/dev/null 2>&1 &&
     ! terraform init -backend=false -input=false -no-color >/dev/null 2>&1; then
    echo "    init failed"
    failures=$((failures+1))
    failed_dirs+=("$rel (init)")
    continue
  fi
  aliases="$(grep -ho 'configuration_aliases *= *\[[^]]*\]' ./*.tf 2>/dev/null | grep -o '[a-z0-9_-]*\.[a-z0-9_-]*' | sort -u)"
  if [[ -n "$aliases" ]]; then
    alias_file="$tf_dir/$ALIAS_FILE"
    for a in $aliases; do printf 'provider "%s" {\n  alias = "%s"\n}\n' "${a%%.*}" "${a#*.}"; done >"$alias_file"
  fi
  if ! terraform validate -no-color; then
    failures=$((failures+1))
    failed_dirs+=("$rel (validate)")
  fi
  if [[ -n "$alias_file" ]]; then rm -f "$alias_file"; alias_file=""; fi
done

echo
if [ $failures -eq 0 ]; then
  echo "all $total dirs valid"
  exit 0
else
  echo "$failures of $total dirs failed:"
  for d in "${failed_dirs[@]}"; do
    echo "  - $d"
  done
  exit 1
fi
