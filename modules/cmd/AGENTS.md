# cmd module

The agent-facing shell: a script arrives inline or as a cabinet key, the `cmd` lambda runs it on
linux with outbound internet, and `build_layer` grows the dependencies it can call. This is the
outward-facing execution vessel — `analyze` (modules/agent) computes over the firm's data
in a sandbox that deliberately reaches only S3.

The cmd lambda's role is the security boundary: model-written scripts execute against ITS
grants (one SSM env path, four cabinet prefixes), never the agent's.

It is the **outward-reaching half of `modules/automation`** — code that reaches the internet, where
automation's own runner reaches the firm's tools. Same split, two roles: this
one holds no `lambda:InvokeFunction`, so a script here cannot touch a module tool whatever it does.

## current features

- **`cmd`** — gateway tool, `{script}` inline or `{script_key}` into the cabinet, fetched here so
  the payload stays small and what runs is what is STORED rather than whatever the agent re-emitted.
  Two key prefixes and the difference is the whole gate: `scripts/` is agent-writable, for a run the
  owner asked for and is watching; `automations/approved/external/` is written only by
  `approve_automation`, so a SCHEDULED run can execute nothing that has not passed review. Anything
  else — or a `..` — is refused before S3 is touched. Both given = inline wins, logged as
  `cmd_both_sources`. Writes the script to /tmp, runs `sh -e`, TEES output
  (streams to the log group live so a long run is tailable mid-flight, and returns in the
  response). Returns `{exit_code, output}`; a non-zero exit is a 422, so a failed script
  reads as a failed tool call. Output over 200k chars spills to the cabinet
  (`outputs/<date>/cmd-<id>.log`) and the response carries `output_key` + a 20k tail.
  15 min timeout, 1GB, /tmp is per-invoke scratch.
- **owner-populated env** — every SSM param under `/gradienterp/customers/<gerp>/automation/env/*`
  exports as an env var (`…/automation/env/GH_TOKEN` → `$GH_TOKEN`), fetched PER INVOKE so a
  freshly stored secret works on the next script. Owners fill it via `collect_secret`;
  values never enter model context. The path boundary keeps this role away from platform
  secrets (webhook signing keys, provider creds live elsewhere).
- **`build_layer`** — gateway tool, `{spec_key}` starts / `{build_id}` polls. The buildspec
  is an agent-authored cabinet object (`buildspecs/*.yml`) passed as `buildspecOverride`,
  so terraform owns only the project shell. On SUCCEEDED it publishes the artifact as
  `<prefix>-<spec-stem>` and attaches it to the cmd lambda, replacing any older version of
  the same layer. Spec contract: install into `layer/`, artifact `layer.zip`; `bin/` inside
  lands on PATH as `/opt/bin`. Lambda caps a function at 5 layers, 250MB unzipped.
- **codebuild project** — `NO_SOURCE`, amazonlinux-x86_64-standard:5.0, artifacts to the
  cabinet's `layers/` prefix. Runs are tailable in codebuild's own logs.
- **the script library** — reusable scripts live in the cabinet under `scripts/` (captions
  are the index, `manage_storage` writes them); pass `script_key` and this fetches it.
  Owner-auditable: scripts, buildspecs, and outputs all sit in their filing cabinet.
- **scheduled runs go through automation's gate.** `manage_automation (op: schedule)` on a `.sh` script points
  an EventBridge schedule at this lambda with `{"script_key": "automations/approved/external/…"}`;
  automation's scheduler target role allowlists this function and its own runner, and nothing else.
  A firm authors a shell script into `automations/staged/`, `review_automation` reads it cold, and
  `approve_automation` is the only thing that can put it where a schedule can reach it.
- **outcome lines, for approved runs only** — a run out of `approved/external/` prints
  `{"event": "automation_ok" | "automation_fail", "automation": "<name>", …}`, which a subscription
  filter on this log group feeds to automation's `create_inc_from_log`: an incident, a notice to the
  owner, a diagnose poke. An ATTENDED failure prints nothing — the owner asked for it and is reading
  the error, so filing an incident would be noise.
- **python3.12 runtime, not provided.al2023** — the deploy pipeline zips files 0644, so a
  custom-runtime bootstrap couldn't carry its exec bit; the shim spawns `sh -e` and gets
  boto3 (the per-invoke SSM fetch) for free. Same shell, no packaging fight.
- **flag-gated** — `CMD_ENABLED` in `config.json` gates the whole module in
  `prod/per_customer` (the agent_email shape). Off = no lambdas, no targets, no tools.

## boundaries

- **tf owns SHAPE only, twice**: lambda code comes from the artifact bucket (`deploy.sh
  push`), and the cmd lambda's LAYERS belong to build_layer — `ignore_changes = [layers]`,
  or an apply would strip what the agent built.
- **the cabinet bucket is agent-owned** — reached by constructed name + KMS alias lookup
  (a module ref would cycle through `depends_on = [module.agent]`). cmd holds prefix grants
  only: read `scripts/`, `buildspecs/`, `layers/`, `automations/approved/external/`; write
  `outputs/`. It cannot write the approved prefix — a bucket policy on the cabinet denies
  `s3:PutObject` there to every principal except `approve_automation`, which is what makes a
  scheduled run's key trustworthy without cmd having to check anything.

## not owned

- spend/approval policy — what sits in the owner's env is the owner's business; policy is
  a stated instruction or a rules instance, never baked here
- the cabinet bucket, its KMS key, and `manage_storage` (modules/agent, modules/storage)
