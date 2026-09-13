#!/usr/bin/env bash
# The local dev stack — every surface, as plain processes.
#
#   bash scripts/local-dev.sh --install     # one-time: .venv (python3.12), the python deps, each Node lambda's npm ci
#   bash scripts/local-dev.sh --start       # ends any running copy of each surface, then starts it
#   bash scripts/local-dev.sh --restart
#   bash scripts/local-dev.sh --status      # what's up, on which port, and any duplicate processes
#   bash scripts/local-dev.sh --stop        # ends every copy of every surface, whoever started it
#   bash scripts/local-dev.sh --uninstall   # stop + drop the deps
#
# Surfaces:
#   :5000  local AWS      moto_server — real boto3 calls land here (modules/aws/aws.py)
#   :3000  owner app BFF  tests/server/bff — a Cognito callback origin
#   :8080  customer gw    tests/server/per_customer/server.py — the accounting route catalog
#   :4242  stripe        tests/server/stripe/server.py — cards, a hosted page, the webhook; no Stripe
#   :4243  cognito       tests/server/cognito/server.py — signup, the login-email change, refresh; no Cognito
#   :3001  oob.biz       prod/openlyoperated_biz/web — the public dashboard, reading the live api
#   pump                 tests/server/pump.py — the streams and queues moto doesn't deliver; no port
#
# Plain processes, not containers: all three are Python, so on macOS a container would put a VM and
# a filesystem layer inside the edit→see-it loop, and mounting /repo to get live edits back only
# undoes what a process has for free. Unrelated: `scripts/docker.sh` builds the AGENT image, which
# AgentCore Runtime genuinely requires — that is deploy-time, not this loop.
#
# Env:
#   LOCAL_AWS_ENDPOINT   default http://localhost:5000
#   LOCAL_GERPS          the bff image's sub → gerps seed (see tests/server/bff/server.py)
#                        set in the repo-root .env; a copy is the SecureString
#                        /gradienterp/customers/gradienterp/local_dev/LOCAL_GERPS in the gradienterp gerp's account
#   MOTO_PORT / BFF_PORT / GW_PORT   to move a surface off a busy port

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# NOT under out/ — `scripts/test.sh` wipes that directory, which silently ate the pidfiles and
# left every surface running with nothing tracking it.
RUN_DIR="${TMPDIR:-/tmp}/gerp-local-dev"
PY="$REPO_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

MOTO_PORT="${MOTO_PORT:-5000}"
BFF_PORT="${BFF_PORT:-3000}"
GW_PORT="${GW_PORT:-8080}"
STRIPE_PORT="${STRIPE_PORT:-4242}"
COGNITO_PORT="${COGNITO_PORT:-4243}"
BIZ_PORT="${BIZ_PORT:-3001}"
# the bff forwards a gerp-scoped request to its gateway; locally that is the per_customer image.
# Unset to forward at the real deployed gateway instead (needs a real Hosted-UI token).
export LOCAL_GATEWAY_URL="${LOCAL_GATEWAY_URL-http://localhost:$GW_PORT}"
# the Stripe stand-in (tests/server/stripe); `off` keeps the lambdas on real Stripe with a test key
export LOCAL_STRIPE_URL="${LOCAL_STRIPE_URL-http://127.0.0.1:$STRIPE_PORT}"

mkdir -p "$RUN_DIR"

SCRATCH="$REPO_ROOT/out/accounting-smoke"

# Same PYTHONPATH scripts/test.sh exports, so a surface and a test see one import graph.
export PYTHONPATH="$REPO_ROOT/modules:$REPO_ROOT/modules/clock:$REPO_ROOT/modules/agreements:$REPO_ROOT/modules/schemas:$REPO_ROOT/modules/aws:$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

# AWS_LAMBDA_FUNCTION_NAME stays UNSET here, on purpose. Empty means both "not in Lambda" and "do
# not touch real AWS" — modules/aws/aws.py reads that and points every boto3 call at
# LOCAL_AWS_ENDPOINT. Setting it to reach a local endpoint would be a lie, and the 31
# event-emitting lambdas would start putting to the PRODUCTION bus.
unset AWS_LAMBDA_FUNCTION_NAME
export LOCAL_AWS_ENDPOINT="${LOCAL_AWS_ENDPOINT:-http://localhost:$MOTO_PORT}"

# name|port|signature|start-command. The signature is what `pgrep -f` finds a running copy by,
# however it was started: the module or script the surface runs.
_surfaces() {
    cat <<EOF
moto|$MOTO_PORT|moto.server|$PY -m moto.server -p $MOTO_PORT
bff|$BFF_PORT|tests.server.bff.server:app|$PY -m uvicorn tests.server.bff.server:app --host 127.0.0.1 --port $BFF_PORT
gateway|$GW_PORT|tests.server.per_customer.server:app|$PY -m uvicorn tests.server.per_customer.server:app --host 127.0.0.1 --port $GW_PORT
biz|$BIZ_PORT|tests.server.openlyoperated_biz.server:app|$PY -m uvicorn tests.server.openlyoperated_biz.server:app --host 127.0.0.1 --port $BIZ_PORT
stripe|$STRIPE_PORT|tests.server.stripe.server:app|$PY -m uvicorn tests.server.stripe.server:app --host 127.0.0.1 --port $STRIPE_PORT
cognito|$COGNITO_PORT|tests.server.cognito.server:app|$PY -m uvicorn tests.server.cognito.server:app --host 127.0.0.1 --port $COGNITO_PORT
pump|-|tests/server/pump.py|$PY $REPO_ROOT/tests/server/pump.py
EOF
}

# Every running copy of one surface: the processes whose command line carries its signature, and
# its pidfile's process while alive. `moto.server` is not this repo's own name, so a moto process
# counts only when it runs from this repo (its command or its working directory). A process that
# merely holds the port is not a surface — on macOS :5000 is AirPlay — and is left alone.
_pids_of() {
    local name="$1" sig="$2" pids="" p cmd cwd
    for p in $(pgrep -f -- "$sig" 2>/dev/null || true); do
        [[ "$p" == "$$" ]] && continue
        if [[ "$name" == "moto" ]]; then
            cmd="$(ps -o command= -p "$p" 2>/dev/null || true)"
            cwd="$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || true)"
            [[ "$cmd" == *"$REPO_ROOT"* || "$cwd" == "$REPO_ROOT"* ]] || continue
        fi
        pids="$pids $p"
    done
    local pidfile="$RUN_DIR/$name.pid"
    if [[ -f "$pidfile" ]]; then
        p="$(cat "$pidfile")"
        [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null && pids="$pids $p"
    fi
    # shellcheck disable=SC2086
    printf '%s\n' $pids | sed '/^$/d' | sort -un | tr '\n' ' ' | sed 's/ $//'
}

# End every running copy of every surface — TERM, up to 5s to exit, then KILL — and drop the
# pidfiles. `--stop` is this; `--start` runs it first, so a start always replaces what was running.
_kill_surfaces() {
    local verbose="${1:-}" name port sig cmd pids p left
    while IFS='|' read -r name port sig cmd; do
        pids="$(_pids_of "$name" "$sig")"
        if [[ -n "$pids" ]]; then
            # shellcheck disable=SC2086
            kill -TERM $pids 2>/dev/null || true
            left=""
            for _ in $(seq 1 50); do
                left=""
                for p in $pids; do kill -0 "$p" 2>/dev/null && left="$left $p"; done
                [[ -z "$left" ]] && break
                sleep 0.1
            done
            if [[ -n "$left" ]]; then
                # shellcheck disable=SC2086
                kill -KILL $left 2>/dev/null || true
                echo "  $name killed (pids$left) after 5s"
            else
                echo "  $name stopped (pids $pids)"
            fi
        elif [[ -n "$verbose" ]]; then
            echo "  $name not running"
        fi
        rm -f "$RUN_DIR/$name.pid"
    done < <(_surfaces)
}

# a surface with no listener (the pump) reports "-" as its port and is alive by a running copy instead
_up() {
    if [[ "$1" == "-" ]]; then
        local sig; sig="$(_surfaces | awk -F'|' -v n="${2:-}" '$1 == n {print $3}')"
        [[ -n "$(_pids_of "${2:-}" "$sig")" ]]
        return
    fi
    curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$1/"
}

_start_one() {
    local name="$1" port="$2" cmd="$3" pidfile="$RUN_DIR/$name.pid" holder
    # after _kill_surfaces nothing of ours holds the port, so a listener there is someone else's
    if [[ "$port" != "-" ]] && holder="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null | head -1)" && [[ -n "$holder" ]]; then
        echo "  !! $name not started: :$port is held by pid $holder ($(ps -o command= -p "$holder" | sed -E 's#^[^ ]*/##' | cut -c1-60)) — move the surface's port or free it" >&2
        return
    fi
    mkdir -p "$SCRATCH"
    cd "$REPO_ROOT"
    # accounting is not migrated off IS_LAMBDA yet, so the gateway's post_journal_entry still writes
    # jsonl. Drop these three once modules/accounting moves to modules/aws/aws.py.
    LOCAL_LEDGER="$SCRATCH/ledger.jsonl" \
    LOCAL_PENDING="$SCRATCH/pending.jsonl" \
    LOCAL_EVENTS="$SCRATCH/events.jsonl" \
        nohup $cmd >"$RUN_DIR/$name.log" 2>&1 &
    echo $! >"$pidfile"
    for _ in $(seq 1 40); do _up "$port" "$name" && break; sleep 0.3; done
    if _up "$port" "$name"; then
        echo "  $name up$([[ "$port" == "-" ]] && echo "" || echo " on :$port")"
    else
        echo "  !! $name failed — see $RUN_DIR/$name.log" >&2
        tail -5 "$RUN_DIR/$name.log" >&2 || true
    fi
}

# --restart is --stop then --start; re-entering the same case is what keeps the two in step
if [[ "${1:-}" == "--restart" ]]; then
    bash "$0" --stop      # via bash: the script is run as `bash script.sh`, never chmod +x
    set -- --start
fi

case "${1:---status}" in
--install)
    # .venv on the version the lambdas run (python3.12), or python3 where that isn't installed
    if [[ ! -x "$REPO_ROOT/.venv/bin/python" ]]; then
        base="$(command -v python3.12 || command -v python3)"
        echo "creating .venv with $base"
        "$base" -m venv "$REPO_ROOT/.venv"
    fi
    PY="$REPO_ROOT/.venv/bin/python"
    echo "installing the python deps into .venv: the tests', the agent container's, the local servers'"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r "$REPO_ROOT/tests/requirements.txt" \
        -r "$REPO_ROOT/modules/agent/docker/requirements.txt" uvicorn
    # each Node lambda's own dependencies: its tests import them, and deploy.sh zips its node_modules
    if command -v npm >/dev/null; then
        while IFS= read -r lock; do
            echo "npm ci  ${lock%/package-lock.json}"
            (cd "$(dirname "$lock")" && npm ci --no-audit --no-fund --silent)
        done < <(cd "$REPO_ROOT" && find modules prod -name package-lock.json -not -path '*/node_modules/*' | sort)
    else
        echo "!! npm not found: the Node lambdas' tests and deploys need Node 22 and npm" >&2
    fi
    echo "done — bash scripts/test.sh, then bash scripts/local-dev.sh --start"
    ;;

--start)
    echo "starting local dev stack"
    _kill_surfaces
    while IFS='|' read -r name port sig cmd; do _start_one "$name" "$port" "$cmd"; done < <(_surfaces)
    echo
    echo "  local AWS  http://localhost:$MOTO_PORT"
    echo "  owner app  http://localhost:$BFF_PORT   (a registered Cognito callback origin)"
    echo "  gateway    http://localhost:$GW_PORT    /webhooks/<provider>
  oob.biz    http://localhost:$BIZ_PORT   the public dashboard, on the live api"
    ;;

--stop)
    echo "stopping"
    _kill_surfaces verbose
    ;;

--status)
    while IFS='|' read -r name port sig cmd; do
        pids="$(_pids_of "$name" "$sig")"
        dup=""
        [[ "$pids" == *" "* ]] && dup="   $(echo "$pids" | wc -w | tr -d ' ') processes: $pids"
        if _up "$port" "$name"; then echo "  $name  UP    ${port/-/}$dup"; else echo "  $name  down  ${port/-/}$dup"; fi
    done < <(_surfaces)
    ;;

--uninstall)
    bash "$0" --stop
    "$PY" -m pip uninstall --quiet -y moto fastapi uvicorn || true
    rm -rf "$RUN_DIR"
    echo "uninstalled"
    ;;

-h|--help)
    grep '^#' "$0" | sed 's/^# \?//'
    ;;

*)
    echo "unknown: $1  (--install/--start/--status/--stop/--uninstall)" >&2
    exit 1
    ;;
esac
