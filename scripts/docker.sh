#!/usr/bin/env bash
# Local agent-container helpers.
#
# Usage:
#   bash scripts/docker.sh --build
#   bash scripts/docker.sh --run
#   bash scripts/docker.sh --stop
#   bash scripts/docker.sh --push <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>
#
# --build  docker buildx for linux/arm64 from modules/agent/
# --run    detach; mounts the repo at /repo, wires LOCAL_* into out/container-smoke/,
#          OTEL console exporters. requires ANTHROPIC_API_KEY in the invoking shell
# --stop   idempotent; no-op if not running
# --push   tag + ecr login + push. arg is the full ECR image path including tag.
#          the script parses the region from the URI and runs aws ecr get-login-password
#          against it — requires AWS credentials in the invoking shell
#
# For the accounting dev API server (mock of the customer-side HTTP gateway),

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="agentcore-dev"
IMAGE="agentcore-bookkeeper:dev"
PORT=8080
DEFAULT_BUSINESS_NAME="Maria's Cafe"
BUSINESS_NAME="${BUSINESS_NAME:-$DEFAULT_BUSINESS_NAME}"
AGENT_MODE="${AGENT_MODE:-bookkeeper}"
SCRATCH="$REPO_ROOT/out/container-smoke"
LOGS="$REPO_ROOT/logs/container-smoke"

do_build() {
    # Stage platform registries into the build context (Dockerfile COPYs them).
    # The build context is modules/agent; modules/schemas/data/ lives outside,
    # so a pre-build copy is required.
    mkdir -p "$REPO_ROOT/modules/agent/docker/registries"
    cp "$REPO_ROOT/modules/schemas/data/"*.json "$REPO_ROOT/modules/agent/docker/registries/"

    # --provenance/--sbom off: buildx otherwise wraps the image in an index with untagged
    # attestation + platform child manifests, so each push = 3 ECR artifacts. Those untagged
    # children make "keep last N tagged" retention (prod/tower/agent_image.tf) mean ~N/3 versions
    # and let an untagged-expiry rule orphan a retained tag's children. AgentCore only needs the
    # runnable arm64 image; a single tagged manifest is cleaner.
    docker buildx build --platform linux/arm64 \
        --provenance=false --sbom=false \
        -f "$REPO_ROOT/modules/agent/docker/Dockerfile" \
        -t "$IMAGE" \
        "$REPO_ROOT/modules/agent"
}

do_run() {
    if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
        echo "ANTHROPIC_API_KEY must be set in the invoking shell" >&2
        exit 1
    fi
    mkdir -p "$SCRATCH" "$LOGS"
    docker run -d --name "$NAME" --rm -p "${PORT}:${PORT}" \
        -v "$REPO_ROOT":/repo \
        -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
        -e AGENT_MODE="$AGENT_MODE" \
        -e BUSINESS_NAME="$BUSINESS_NAME" \
        -e OTEL_SERVICE_NAME=agentcore-bookkeeper \
        -e OTEL_TRACES_EXPORTER=console \
        -e OTEL_METRICS_EXPORTER=console \
        -e OTEL_LOGS_EXPORTER=console \
        -e LOCAL_LEDGER=/repo/out/container-smoke/ledger.jsonl \
        -e LOCAL_PENDING=/repo/out/container-smoke/pending.jsonl \
        -e LOCAL_BALANCES=/repo/out/container-smoke/balances.jsonl \
        -e LOCAL_CLASSIFICATIONS=/repo/out/container-smoke/classifications.jsonl \
        -e LOCAL_S3=/repo/out/container-smoke/reports \
        -e LOCAL_LOGS=/repo/logs/container-smoke \
        "$IMAGE"
    # wait for readiness so subsequent curls don't race startup under qemu emulation
    until curl -sS "http://localhost:${PORT}/healthz" >/dev/null 2>&1; do sleep 1; done
    curl -sS "http://localhost:${PORT}/healthz"
    echo
}

do_stop() {
    docker stop "$NAME" >/dev/null 2>&1 || true
    echo "stopped $NAME (or was not running)"
}

do_push() {
    local target="${1:-}"
    if [[ -z "$target" ]]; then
        echo "--push needs a full ECR image path, e.g. 123.dkr.ecr.us-east-1.amazonaws.com/agentcore/cust_01:v3" >&2
        exit 1
    fi
    local region
    region=$(echo "$target" | sed -n 's#.*\.dkr\.ecr\.\([^.]*\)\.amazonaws\.com.*#\1#p')
    if [[ -z "$region" ]]; then
        echo "couldn't parse region from ECR URI: $target" >&2
        exit 1
    fi
    local registry
    registry=$(echo "$target" | cut -d/ -f1)
    aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"
    docker tag "$IMAGE" "$target"
    docker push "$target"
}

case "${1:-}" in
    --build) do_build ;;
    --run)   do_run ;;
    --stop)  do_stop ;;
    --push)  do_push "${2:-}" ;;
    *)       echo "usage: bash scripts/docker.sh --build | --run | --stop | --push <ecr-uri>" >&2; exit 1 ;;
esac
