#!/usr/bin/env bash
# Run the browser suite (tests/e2e, Playwright + AWS SDK). Defaults: local target, smoke suite.
#
#   bash scripts/e2e.sh --configure              # once — the suite's and tests/mailbox's install + LOCAL_GERPS
#   bash scripts/e2e.sh                          # smoke against the local stack
#   bash scripts/e2e.sh --suite full
#   bash scripts/e2e.sh --env prod               # smoke against gradienterp.cloud, read-only
#   bash scripts/e2e.sh --env prod --configure   # the prod half: the profile + the owner password
#   bash scripts/e2e.sh -- login.spec.mjs --headed
#
# Flags:
#   --env <local|prod>                 which target (default: local)
#   --suite <smoke|full|lifecycle>     which npm script (default: smoke); lifecycle is prod only
#   --configure                        set up what a run on --env needs, then exit
#   -- <args>                          passed to playwright: a spec file, --grep, --headed
#
#   local smoke → npm run smoke:local     prod smoke     → npm run smoke:prod (skips @mutating)
#   local full  → npm run full:local      prod full      → npm run full:prod (mutates the seeded gerp)
#                                         prod lifecycle → npm run lifecycle (headed)
#
# A local run needs the local stack (scripts/local-dev.sh) and LOCAL_GERPS, the bff's sub → gerps
# seed, in the repo-root .env. --configure copies LOCAL_GERPS into .env from the SSM copy in the
# gradienterp gerp's account when .env has none. A run starts the stack only when a surface is
# down: --start replaces every running surface, which drops the emulator's state.

set -euo pipefail

ENV="local"
SUITE="smoke"
CONFIGURE=0
PASS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env|--suite)
            [[ $# -ge 2 ]] || { echo "$1 needs a value" >&2; exit 1; }
            if [[ "$1" == "--env" ]]; then ENV="$2"; else SUITE="$2"; fi
            shift 2;;
        --configure) CONFIGURE=1; shift;;
        --)          shift; PASS=("$@"); break;;
        -h|--help)
            awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
            exit 0;;
        *) echo "unknown arg: $1" >&2; exit 1;;
    esac
done

case "$ENV" in local|prod) ;; *) echo "--env must be local or prod, got: $ENV" >&2; exit 1;; esac
case "$SUITE" in smoke|full|lifecycle) ;; *) echo "--suite must be smoke, full or lifecycle, got: $SUITE" >&2; exit 1;; esac
if [[ "$SUITE" == "lifecycle" && "$ENV" != "prod" ]]; then
    echo "--suite lifecycle runs against prod only: add --env prod" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
E2E_DIR="$REPO_ROOT/tests/e2e"
# the specs import tests/mailbox (a real inbox for signup codes), its own npm package
MAILBOX_DIR="$REPO_ROOT/tests/mailbox"
ENV_FILE="$REPO_ROOT/.env"

LOCAL_GERPS_PARAM="/gradienterp/customers/gradienterp/local_dev/LOCAL_GERPS"
LOCAL_GERPS_PROFILE="gerp-gradienterp"
PROD_PROFILE="operator-org"
OWNER_PASSWORD_PARAM="/gradienterp/test/e2e/owner_password"

# ── what a run needs ─────────────────────────────────────────────────────────

installed() {
    [[ -d "$E2E_DIR/node_modules/@playwright/test" ]] || return 1
    [[ -d "$MAILBOX_DIR/node_modules" ]] || return 1
    (cd "$E2E_DIR" && node -e "
        const { chromium } = require('@playwright/test');
        require('fs').accessSync(chromium.executablePath());
    " >/dev/null 2>&1)
}

# the .env line, not the shell: the bff reads .env, and a shell variable doesn't reach a stack
# started from another terminal
env_has_local_gerps() {
    [[ -f "$ENV_FILE" ]] && grep -Eq '^[[:space:]]*LOCAL_GERPS[[:space:]]*=[[:space:]]*[^[:space:]]' "$ENV_FILE"
}

profile_resolves() {
    aws sts get-caller-identity --no-cli-pager --profile "$1" >/dev/null 2>&1
}

surface_up() {
    curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$1/"
}

# ── --configure ──────────────────────────────────────────────────────────────

if [[ "$CONFIGURE" == 1 ]]; then
    echo "configuring e2e for $ENV"
    for dir in "$E2E_DIR" "$MAILBOX_DIR"; do
        cd "$dir"
        if [[ ! -d node_modules || package-lock.json -nt node_modules/.package-lock.json ]]; then
            npm ci --no-audit --no-fund
        else
            echo "  ${dir#$REPO_ROOT/}/node_modules up to date"
        fi
    done
    cd "$E2E_DIR"
    npm run --silent setup

    if [[ "$ENV" == "local" ]]; then
        if env_has_local_gerps; then
            echo "  LOCAL_GERPS already in .env — left as it is"
        else
            if ! value="$(aws ssm get-parameter --no-cli-pager --profile "$LOCAL_GERPS_PROFILE" \
                    --name "$LOCAL_GERPS_PARAM" --with-decryption \
                    --query Parameter.Value --output text 2>/dev/null)" || [[ -z "$value" ]]; then
                echo "  !! could not read $LOCAL_GERPS_PARAM with profile $LOCAL_GERPS_PROFILE" >&2
                exit 1
            fi
            # a new .env holds a login's gerps; keep it the owner's
            [[ -f "$ENV_FILE" ]] || (umask 077 && : >"$ENV_FILE")
            [[ -s "$ENV_FILE" && -n "$(tail -c 1 "$ENV_FILE")" ]] && echo >>"$ENV_FILE"
            printf 'LOCAL_GERPS=%s\n' "$value" >>"$ENV_FILE"
            echo "  LOCAL_GERPS written to .env from $LOCAL_GERPS_PARAM"
        fi
    else
        if ! profile_resolves "$PROD_PROFILE"; then
            echo "  !! profile $PROD_PROFILE does not resolve — sign in, then run this again" >&2
            exit 1
        fi
        found="$(aws ssm describe-parameters --no-cli-pager --profile "$PROD_PROFILE" \
            --parameter-filters "Key=Name,Values=$OWNER_PASSWORD_PARAM" \
            --query 'length(Parameters)' --output text 2>/dev/null || echo 0)"
        if [[ "$found" != "1" ]]; then
            echo "  !! $OWNER_PASSWORD_PARAM is missing in the operator account — set it by hand (SecureString)" >&2
            exit 1
        fi
        echo "  profile $PROD_PROFILE resolves; $OWNER_PASSWORD_PARAM exists"
    fi
    echo "configured"
    exit 0
fi

# ── before a run ─────────────────────────────────────────────────────────────

if ! installed; then
    echo "the e2e install or chromium is missing — run: bash scripts/e2e.sh --configure" >&2
    exit 1
fi

if [[ "$ENV" == "local" ]]; then
    if ! env_has_local_gerps; then
        echo "no LOCAL_GERPS in $ENV_FILE — run: bash scripts/e2e.sh --configure" >&2
        exit 1
    fi
    down=""
    for port in 5000 3000 8080; do surface_up "$port" || down="$down :$port"; done
    if [[ -n "$down" ]]; then
        echo "local stack down on$down — starting it"
        bash "$REPO_ROOT/scripts/local-dev.sh" --start
        for port in 5000 3000 8080; do
            if ! surface_up "$port"; then
                echo "  !! :$port still down after --start — see bash scripts/local-dev.sh --status" >&2
                exit 1
            fi
        done
    fi
else
    if ! profile_resolves "$PROD_PROFILE"; then
        echo "profile $PROD_PROFILE does not resolve — run: bash scripts/e2e.sh --env prod --configure" >&2
        exit 1
    fi
fi

case "$ENV:$SUITE" in
    local:smoke)    SCRIPT="smoke:local";;
    local:full)     SCRIPT="full:local";;
    prod:smoke)     SCRIPT="smoke:prod";;
    prod:full)      SCRIPT="full:prod";;
    prod:lifecycle) SCRIPT="lifecycle";;
esac

cd "$E2E_DIR"
exec npm run "$SCRIPT" -- ${PASS[@]+"${PASS[@]}"}
