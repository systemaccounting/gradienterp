# optimizer — how

Operator-account hub-and-spoke economic optimizer. Why → `README.md`; open work → `TODO.md`.

## current features

- **infra home** — `infra/` is an operator-account root module: backend + provider assume `OrganizationAccountAccessRole` in operator (`185369506315`), wired to the shared bus + gerp-instance/profile registries via the operator remote state. Holds the `reindex` writer, the `find_profiles` reader, and the `hub` agent (all below, all deployed).
- **hub agent** (`hub.tf` + `modules/agent/prompts/broker.md`) — `gerp_optimizer_hub`, an operator-account AgentCore runtime in **broker mode**. Reuses the shared agent image (`modules/agent/docker`) **gateway-less** (`StrandsEngine` is gateway-optional; selects on `BEDROCK_MODEL_ID` alone). Its tools are in-process: `find_profiles` (invokes the reader lambda) + `ask_spoke(gerp_id, msg)` (resolves the spoke's `runtime_endpoint_arn` off `gerp-customers`, then cross-account `InvokeAgentRuntime`). Serves extemporaneous owner requests ("find me a tech at my laundromat"): map trade → NAICS/SOC → `find_profiles` → ask ≤6 spokes → collate. Fan-out is repetition — one `InvokeAgentRuntime` per spoke, sequential; there is no multi-spoke primitive. The reciprocal is the spoke's `ask_hub` tool (`modules/agent`). Both cross-account legs are live-verified.
- **cross-firm a2a trust** — `InvokeAgentRuntime` with a qualifier authorizes against BOTH the **runtime** arn AND the **endpoint** arn, so a resource policy (`aws_bedrockagentcore_resource_policy`) is needed on each. The hub grants the whole org (`aws:PrincipalOrgID`) on its runtime+endpoint (`hub.tf`, so any spoke can `ask_hub` with no per-spoke update); each spoke grants ONLY the hub role on its own runtime+endpoint (`modules/agent` `hub_inbound`, so the hub can `ask_spoke` — spokes never reach each other). Identity side: `InvokeAgentRuntime` on `runtime/*` (the `*` covers endpoint arns). AgentCore does NOT deliver runtime container logs to CloudWatch here — debug via the `debug_whoami` payload hatch in `entrypoint.py` (`{"debug_whoami":true,"invoke_target":<arn>}` → sts caller-identity + a raw invoke), not logs.
- **registry query primitives** (`lambdas/_helpers.py` + `lambdas/find_profiles/`) — the hub's yellow-pages lookup over the profile registry, deployed as `gerp-optimizer-find-profiles` (the hub's tool invokes it). `reindex(profile)` writes the inverted index (`gerp-profile-index`, in `prod/platform/operator`): one row per match-key value, stale rows dropped via the `by-profile` GSI; the match-key field set is read from `profile_fields.json` (`role == "match-key"` → naics/soc/city/state), so a new match-key is a schema edit. `query_index("<dim>#<value>")` is a point-Query; `find_profiles({match:[…], near?})` intersects the keys, batch-gets the profiles, radius-filters. Local-proven: `bash scripts/test.sh --module optimizer` (10 tests).
- **measured match-keys** — `soc` and `naics` on a profile are distributions `[{code, share, …}]`
  written by the platform's count; `index_keys` reads `code` off each element (a bare string still
  works), so `soc#35-3023.01` finds the person whose hours say so.
- **profile index writer** (`lambdas/reindex/`) — the index self-heals off a DDB stream, so no writer implements indexing. `gerp-profiles` (stream enabled in `prod/platform/operator`) ─▶ `gerp-optimizer-reindex` lambda ─▶ `gerp-profile-index`: INSERT/MODIFY → `reindex(new_image)`, REMOVE → `deindex(id)`; the stream's DDB-typed images are deserialized to the same plain dicts `_helpers` sees locally. Any profile writer (BFF person form, provisioning business rows) auto-indexes. The lambda bundles `_helpers.py` + `profile_fields.json` beside `main.py`.

## apply

Operator-singleton stack — same chain as `api_openlyoperated/` (see `prod/AGENTS.md` §local apply). Assumes `OrganizationAccountAccessRole`, which trusts management, so a direct run is under `AWS_PROFILE=default` (NOT `operator-org`), which `apply.sh` sets:

```bash
bash scripts/apply.sh --stack optimizer        # --plan to stop after the plan
```

State: `s3://gradienterp-tfstate-185369506315/optimizer/terraform.tfstate`.

The hub runtime pulls the shared agent image by tag (`local.hub_image_tag` in `hub.tf`) — build+push it first (`bash scripts/docker.sh --build` → `--push …/agentcore:<tag>`, same image as the spokes). The operator account's first Bedrock invoke auto-subscribes the model agreement (the hub role carries `aws-marketplace:Subscribe`+`ViewSubscriptions`); it takes ~2 min to settle on a fresh account. Deploy order: build/push image → apply this stack → `per_customer` reconciles each spoke's `ask_hub` + inbound trust (it reads this stack's `hub_runtime_endpoint_arn` + `hub_role_arn` outputs).
