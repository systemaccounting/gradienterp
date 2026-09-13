# per_customer — open work

`AGENTS.md` covers the template + the modules currently wired. This lists what's still open.

## modules to wire (as their `infra/` lands)

- [ ] **payments** — webhook ingest lambdas; transforms already live in `accounting/lambdas/ingest`
- [ ] **purchasing / labor / invoicing / treasury / iot** — still spec-stage; wire as each ships its `infra/` + lambdas

## hardening

- [ ] **stack overlay** — conditional `prod/stacks/<customer_id>/` apply for bespoke per-customer extensions. tower's orchestrator runs base (this) first, then the stack if it exists.
- [ ] **per-customer tfvars from SSM** — `sender_email` + `chat_base_url` are operator-wide today (passed to every apply). when they go per-customer, source from the SSM tenant-metadata blob.
- [ ] **destroy / offboard path** — offboarding requires `terraform destroy` against this template before account closure (empty S3 report bucket + ECR-less now, so fewer blockers). tower's lifecycle lambda drives.

## drift detection (code/schema/infra, not registry)

Registry config flows through the agent path (extend_schema + weekly canonical-pull), NOT terraform. Lambda code / schemas / IAM still flow through terraform/codebuild — detect when a customer is behind the operator's source bundle:

- [ ] **`terraform_data.applied_source_etag`** — `data "aws_s3_object" "per_customer_source"` reads operator's source-bundle etag at apply; capture it as `input` so state holds the last-applied etag.
- [ ] **`diff_config` lambda + EventBridge Scheduler** — per-customer cron reads operator's source-bundle etag (cross-account `s3:HeadObject` on `gerp-codebuild-source-185369506315/per-customer-source.zip`) vs own tfstate's `applied_source_etag`; on diff, assumes operator's `TowerStartBuild` role and calls `codebuild:StartBuild` on `tower-per-customer`. needs operator-side `TowerStartBuild` role + tfstate bucket policy (tower's TODO).

## note: canonical-registry adds need a manual reseed

`seed_schema` is all-or-nothing idempotent (skips if ANY canonical row exists across all registries). Adding a new `<module>_fields` registry to an already-provisioned customer is NOT picked up by re-apply — it needs a manual per-registry reseed (boto3 batch-write from the canonical S3 JSON into `gerp-schema-<customer_id>`). See `.agents/continuity.md` / memory `reference-canonical-schema-flow`.
