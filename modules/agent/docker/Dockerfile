# AgentCore Runtime container — phase 4a-ii.
#
# Contract:
#   - ARM64 (Graviton) required — AgentCore Runtime won't run x86
#   - HTTP server on 0.0.0.0:8080, ASGI (FastAPI/Starlette)
#   - GET /ping returns {"status":"Healthy", "time_of_last_update": ...}
#   - POST /invocations body is {"prompt": "..."}
#
# Deploy via scripts/docker.sh --build + --push.

FROM --platform=linux/arm64 python:3.14-slim

# Unbuffered stdout/stderr — block buffering under non-TTY would swallow
# early prints (before uvicorn starts) and crash tracebacks.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# deps first — cached unless requirements.txt changes
COPY docker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# prompts (loaded by entrypoint based on AGENT_MODE env var) + entrypoint
COPY prompts/ /app/prompts/
COPY docker/entrypoint.py /app/entrypoint.py

# platform registries (chart_of_accounts.json, contact_fields.json,
# calendar_fields.json). Source-of-truth: modules/schemas/data/*.json.
# scripts/docker.sh stages them into modules/agent/docker/registries/ before
# the build (the docker build context is modules/agent, can't reach across).
COPY docker/registries/ /app/registries/

EXPOSE 8080

# Plain uvicorn (no opentelemetry-instrument wrapper — that blocks AgentCore
# microVM boot). Traces come back later via the OTel SDK directly inside
# entrypoint.py once we know what Observability hookup AgentCore prefers.
CMD ["uvicorn", "entrypoint:app", "--host", "0.0.0.0", "--port", "8080"]
