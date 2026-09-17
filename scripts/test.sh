#!/usr/bin/env bash
# Run local-mode tests. Defaults: all modules, local env, all test files.
#
# Wipes out/ and logs/ before the run so human inspection after only shows
# this run's artifacts. Pass --logs to preserve prior artifacts.
#
#   bash scripts/test.sh
#   bash scripts/test.sh --module accounting
#   bash scripts/test.sh --env local --name post_journal_entry
#   bash scripts/test.sh --env integ
#   bash scripts/test.sh --logs
#   bash scripts/test.sh -j 1            # serial, streaming output
#
# Flags:
#   --env <local|integ>  which subdir under each module to run (default: local)
#   --module <name>      restrict to one module dir (default: all)
#   --name <substring>   restrict to test files whose stem contains this (default: all)
#   --logs               don't wipe out/ and logs/ before running
#   -j <n>               worker count (default: see `workers` below; 1 = serial + streaming)

set -euo pipefail

ENV="local"
MODULE=""
NAME=""
LOGS=0
JOBS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env)    ENV="$2"; shift 2;;
        --module) MODULE="$2"; shift 2;;
        --name)   NAME="$2"; shift 2;;
        --logs)   LOGS=1; shift;;
        -j)       JOBS="$2"; shift 2;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \?//'
            exit 0;;
        *) echo "unknown arg: $1" >&2; exit 1;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TESTS_DIR="$REPO_ROOT/tests"

if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PY="$REPO_ROOT/.venv/bin/python"
else
    PY="python3"
fi

# Shared libraries that any module's lambda may import. In prod `scripts/deploy.py` resolves these
# by walking the import graph across modules/*/ and bundles them into the zip; locally nothing does
# that, so the harness puts them on the path. Without this a lambda that grew an `import clock`
# passes its own module's tests and fails every other module's.
export PYTHONPATH="$REPO_ROOT/modules:$REPO_ROOT/modules/clock:$REPO_ROOT/modules/metrics:$REPO_ROOT/modules/agreements:$REPO_ROOT/modules/schemas:$REPO_ROOT/modules/aws:$REPO_ROOT/modules/events:$REPO_ROOT/modules/payments:$REPO_ROOT/modules/rules:$REPO_ROOT/modules/invoicing:$REPO_ROOT/modules/journal:$REPO_ROOT/modules/mcp${PYTHONPATH:+:$PYTHONPATH}"

# ── how many workers ─────────────────────────────────────────────────────────
# Test files are independent — every table, bucket and queue a test makes carries a unique name —
# so the only question is how many to run at once.
#
# The core count is asked for portably (`getconf`, not `nproc`, which is Linux-only). But a
# container with a CPU quota still reports its HOST's cores: a 2-core CI container on a 64-core box
# answers 64, so the number is wrong rather than merely large. cgroup holds the real budget, and
# reading it needs no OS branch — the file is simply absent on macOS, which is the macOS answer.
#
# min(8, n-1): the -1 leaves a core for whatever else the developer is running, and 8 is where the
# measurement stopped paying (8 workers 17s, 16 workers 16s, for double the memory). Load average
# is deliberately NOT consulted — it averages over a minute and the whole suite runs in under 20s,
# so it would report the machine as it was before this run started.
workers() {
    local n
    if [[ -r /sys/fs/cgroup/cpu.max ]]; then                       # cgroup v2
        read -r q p < /sys/fs/cgroup/cpu.max
        [[ "$q" != "max" ]] && n=$(( q / p ))
    elif [[ -r /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]]; then        # cgroup v1
        q=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us)
        p=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us)
        [[ "$q" -gt 0 ]] && n=$(( q / p ))
    fi
    [[ -z "${n:-}" ]] && n=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
    (( n <= 2 )) && { echo 1; return; }        # too small to share; serial beats thrashing
    (( n - 1 < 8 )) && echo $(( n - 1 )) || echo 8
}
[[ -z "$JOBS" ]] && JOBS=$(workers)

# ── local AWS ────────────────────────────────────────────────────────────────
# Modules migrated off the jsonl local mode run their REAL boto3 calls against a local endpoint
# (modules/aws/aws.py). An absent AWS_LAMBDA_FUNCTION_NAME still means "not in Lambda, don't
# touch real AWS" — it now resolves to localhost, which fails to connect rather than reaching prod.
#
# moto, not LocalStack: Apache-2.0 with no licence gate, pure Python so it matches the stack, and it
# runs as a plain process — no Docker in the test loop. (AWS itself only ships DynamoDB Local, which
# would leave the 31 event-emitting lambdas with nothing.) Started here if it isn't already up, so
# the suite has no external prerequisite.
#
# ONE MOTO PER WORKER. moto is single-threaded: sharing one across workers pins it at a full core
# and the suite flattens at 34s no matter how many workers you add. A moto each moves the ceiling
# onto real CPU — 71s serial, 17s at 8 — at 83 MB per process.
export LOCAL_AWS_ENDPOINT="${LOCAL_AWS_ENDPOINT:-http://localhost:5000}"
# a log field outside aws.IDS ∪ aws.VALUES raises under test (never in prod): the vocabulary
# check runs in the helper on every exercised path, not as a regex over call sites
export LOG_STRICT=1
MOTO_PIDS=()
MOTO_ENDPOINTS=()
# only kill what we started; a server the developer left running stays running
cleanup() { for p in "${MOTO_PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; }
trap cleanup EXIT

# Free ports from the OS rather than a hardcoded base, and readiness probed with $PY — which this
# script already requires, so it is one fewer dependency than curl (absent from slim Linux images).
start_motos() {
    local want=$1 ports port log
    if ! "$PY" -c "import moto" >/dev/null 2>&1; then
        echo "!! moto not installed — pip install 'moto[server]'" >&2
        exit 1
    fi
    ports=$("$PY" - "$want" <<'PY'
import socket, sys
socks = [socket.socket() for _ in range(int(sys.argv[1]))]
for s in socks:
    s.bind(("127.0.0.1", 0))
print(" ".join(str(s.getsockname()[1]) for s in socks))
for s in socks:
    s.close()
PY
)
    for port in $ports; do
        log="${TMPDIR:-/tmp}/gerp-moto-$port.log"
        "$PY" -m moto.server -p "$port" >"$log" 2>&1 &
        MOTO_PIDS+=($!)
        MOTO_ENDPOINTS+=("http://localhost:$port")
    done
    # A dead endpoint does NOT fail loudly — botocore retries with backoff, so the run crawls
    # instead of stopping. Assert every one answers before dispatching anything.
    # One interpreter polls all of them: a probe per attempt per port was ~50ms of process startup
    # each, which cost more than the servers took to boot.
    if ! "$PY" - $ports <<'PY'
import socket, sys, time
pending = [int(p) for p in sys.argv[1:]]
deadline = time.time() + 30
while pending and time.time() < deadline:
    for port in list(pending):
        s = socket.socket(); s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            pending.remove(port)
        s.close()
    if pending:
        time.sleep(0.05)
if pending:
    sys.exit(f"never came up: {pending}")
PY
    then
        echo "!! moto pool failed to start — see ${TMPDIR:-/tmp}/gerp-moto-*.log" >&2
        exit 1
    fi
}

# Two schema surfaces, both cheap and global, both catching things that otherwise surface late:
#   - gateway schema.json descriptions cap at 200 chars (over it fails `terraform apply` deep with
#     "Invalid Attribute Value Length")
#   - the canonical field registries, which get PUBLISHED and seeded fleet-wide, so a hand-edited
#     entry with a typo'd type or a missing description propagates before anyone notices
( cd "$REPO_ROOT" && "$PY" "$SCRIPT_DIR/lint_schemas.py" )

# every shell script parses: a syntax error surfaces here, naming the file and the line, rather than
# half way through a deploy or a codebuild run
sh_files=0
while IFS= read -r -d '' f; do
    bash -n "$f" || { echo "!! ${f#$REPO_ROOT/} does not parse" >&2; exit 1; }
    sh_files=$((sh_files + 1))
done < <(find "$REPO_ROOT" \( -name node_modules -o -name .venv -o -name .terraform -o -name tmp -o -name .git \) -prune \
         -o -name '*.sh' -type f -print0)
echo "shell syntax: $sh_files scripts parse ✓"

if [[ $LOGS -eq 0 ]]; then
    rm -rf "$REPO_ROOT/out" "$REPO_ROOT/logs"
fi

# ── collect the jobs ─────────────────────────────────────────────────────────
# The whole list is gathered before anything runs, so the workers can be handed disjoint slices and
# the output can be replayed in list order regardless of who finished when.
#
# The COUNT comes from what each file's `__main__` block actually reaches, not from how many
# `def test_` it holds — a defined-but-uncalled test used to be reported as a passing one, and four
# were in that state. `collect_tests.py` also names them, and they fail the file rather than
# disappearing into the total.
# Each job is  kind<TAB>path<TAB>label<TAB>count<TAB>orphans.
jobs=()
tests=0
orphaned=0

while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    IFS=$'\t' read -r _kind _path _label count orphans <<<"$line"
    tests=$((tests + count))
    if [[ -n "$orphans" ]]; then
        echo "!! $_label: defined but never called by its __main__ block — ${orphans//,/, }" >&2
        orphaned=$((orphaned + 1))
    fi
    jobs+=("$line")
done < <("$PY" "$SCRIPT_DIR/collect_tests.py" "$TESTS_DIR" "$ENV" "$MODULE" "$NAME")

files=${#jobs[@]}
if [[ $files -eq 0 ]]; then
    echo "no tests matched" >&2
    exit 1
fi

(( JOBS > files )) && JOBS=$files
start_motos "$JOBS"

# ── run one job ──────────────────────────────────────────────────────────────
# $1 job index, $2 worker index. Streams when serial; when parallel, stdout goes to a per-job file
# so two workers can't interleave mid-line, and the exit status goes to a sibling file (a subshell
# cannot hand a counter back to the parent).
run_job() {
    local idx=$1 slot=$2 kind path label rc=0
    IFS=$'\t' read -r kind path label _ <<<"${jobs[$idx]}"
    [[ ${#MOTO_ENDPOINTS[@]} -gt 0 ]] && export LOCAL_AWS_ENDPOINT="${MOTO_ENDPOINTS[$slot]}"
    {
        echo "=== $label ==="
        if [[ "$kind" == py ]]; then
            # A test file is a SCRIPT: the count above comes from grep, the running comes from the
            # file's own `if __name__ == "__main__"` block. A file missing that block exits 0 having
            # run nothing, and its tests are counted as passing — five files were in that state.
            if ! grep -q '^if __name__ == "__main__":' "$path"; then
                echo "  FAIL: no __main__ runner block — tests would be counted but never run"
                rc=1
            else
                "$PY" "$path" || rc=1
            fi
        else
            node --test "$path" || rc=1
        fi
    } >"$JOBDIR/$idx.out" 2>&1
    echo "$rc" >"$JOBDIR/$idx.rc"
}

JOBDIR=$(mktemp -d "${TMPDIR:-/tmp}/gerp-test.XXXXXX")
trap 'cleanup; rm -rf "$JOBDIR"' EXIT

if [[ $JOBS -le 1 ]]; then
    for idx in "${!jobs[@]}"; do
        run_job "$idx" 0
        cat "$JOBDIR/$idx.out"
    done
else
    echo "running $files files across $JOBS workers"
    pids=()
    for slot in $(seq 0 $((JOBS-1))); do
        (
            for idx in "${!jobs[@]}"; do
                if (( idx % JOBS == slot )); then run_job "$idx" "$slot"; fi
            done
            exit 0   # a failing test is recorded in its .rc file; the WORKER always succeeds,
                     # or `set -e` on the wait below would abort the run mid-suite
        ) &
        pids+=($!)
    done
    # ONLY the workers — a bare `wait` also waits on the motos, which never exit.
    wait "${pids[@]}"
    for idx in "${!jobs[@]}"; do cat "$JOBDIR/$idx.out"; done
fi

failed=$orphaned
for idx in "${!jobs[@]}"; do
    [[ "$(cat "$JOBDIR/$idx.rc" 2>/dev/null || echo 1)" == 0 ]] || failed=$((failed + 1))
done

echo
if [[ $failed -eq 0 ]]; then
    if [[ $files -eq 1 ]]; then
        echo "all $tests tests passed"
    else
        echo "all $tests tests passed across $files files"
    fi
else
    echo "$failed of $files failed" >&2
    exit 1
fi
