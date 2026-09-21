# playbooks

Per-customer **Bedrock Knowledge Base** (over **S3 Vectors**) holding the agent's how-to / setup
playbooks. The customer agent searches it with the in-process `search_guides(query)` tool
(`modules/agent`), gets back the matching guide chunks, and merges per-customer facts itself when
talking to the human.

The corpus is every `modules/**/kb.md` in the repo (today: payments stripe / paypal / square,
assets, shipping, inventory, accounting's migration walk, storage's portal, cmd's shell,
inventory/capacity's booking, agreements' capital instruments, settings' locations + job tags,
invoicing's tags, rules' attachment mechanics, and agent's browser). It carries workflow MECHANICS as well as setup: what the
persona keeps is the trigger, what lands here is the shape. Repo is
the source of truth — there is **no S3 staging bucket**; docs are pushed straight
into the KB as inline CUSTOM-data-source documents.

Source of truth cuts both ways: the sync **prunes** as well as ingests, so a `kb.md` deleted from the
repo is deleted from the KB.

## current features

- No agent tool or lambda of its own — the agent's in-process `search_guides` (`modules/agent`) retrieves against this module's KB.
- `aws_s3vectors_vector_bucket` + `aws_s3vectors_index` — `<stack_prefix>-playbooks-<gerp_id>` / index `playbooks`, 1024-dim Titan v2, cosine, float32.
- `aws_bedrockagent_knowledge_base` — Titan Text Embeddings v2, `S3_VECTORS` storage → the index ARN.
- `aws_bedrockagent_data_source` — `type = "CUSTOM"` (inline ingest, `RETAIN`); terraform creates it but never ingests.
- `aws_iam_role.kb_service` — assumed by `bedrock.amazonaws.com`; `InvokeModel` on the Titan ARN + s3vectors read/write on the index.
- outputs: `knowledge_base_id` (feeds the agent's `PLAYBOOK_KB_ID`), `data_source_id`, `kb_service_role_arn`.
- ingestion is `scripts/sync_playbooks.sh <gerp_id> <kb_id> <data_source_id> [profile] [region]`, three callers: the provisioning build (`.codebuild/per-customer.yml` post_build, `env` creds, in the gerp's account before the row is marked active, on every apply — a vended gerp is never ready with an empty shelf); `.github/workflows/playbooks.yaml`, one job per gerp, on every push to `main` that touches a `modules/**/kb.md` (every active gerp), by dispatch (`bash scripts/workflow.sh run playbooks.yaml -f gerp=<id>|all`, no upload: it runs on its checkout) or composed into `deploy.yaml` (`-f playbooks=true`), the knowledge base and data source found by name in the gerp's account (`playbooks-<gerp>`, `repo-playbooks`); and by hand, the same script with the `gerp-<gerp_id>` profile; `--dry-run` previews (see below).

## why per-customer, in the customer account

Bedrock KBs have no cross-account `Retrieve` resource policy, so a single shared KB would force a
per-call cross-account `AssumeRole` (standing trust + a central dependency on every agent turn). One
KB per customer in the customer's own account keeps `Retrieve` same-account; the only thing that
crosses accounts is the operator running ingestion. `modules/playbooks/infra` is instantiated from
`prod/per_customer/main.tf` (`module "playbooks"`) on the default provider already assumed into the
customer account; its `knowledge_base_id` output feeds the agent module's `playbook_kb_id` →
`PLAYBOOK_KB_ID` runtime env (which gates `search_guides`).

## resources (`infra/`)

- `aws_s3vectors_vector_bucket` + `aws_s3vectors_index` — `gerp-playbooks-<gerp_id>` / index `playbooks`,
  1024-dim (Titan v2), `cosine`, `float32`. Chunks are 2,000 tokens: a guide is meant to be
  followed whole, and most come back as one chunk; `search_guides` returns five. **Gotcha:** the index's `non_filterable_metadata_keys`
  MUST be **both** `AMAZON_BEDROCK_TEXT` and `AMAZON_BEDROCK_METADATA` — Bedrock stores the chunk text
  + source metadata under those reserved keys; omit either and ingestion fails.
- `aws_bedrockagent_knowledge_base` — Titan Text Embeddings v2, `S3_VECTORS` storage → the index ARN.
- `aws_bedrockagent_data_source` — `type = "CUSTOM"` (inline ingest; no S3 source). Terraform creates
  it but does **not** ingest — see below.
- `aws_iam_role.kb_service` — assumed by `bedrock.amazonaws.com` (SourceAccount/SourceArn confused-deputy
  conditions). Grants: `bedrock:InvokeModel` on the Titan ARN + s3vectors read/write on the index.
  No S3 object perms (inline ingest). The agent runtime role separately gets `bedrock:Retrieve`.

S3 Vectors is **semantic-only** — never set `overrideSearchType: HYBRID`.

## ingesting / re-ingesting playbooks

Terraform never ingests content; the build does, after the apply. By hand:

```
bash scripts/sync_playbooks.sh [--dry-run] <gerp_id> <kb_id> <data_source_id>
# profile defaults to gerp-<gerp_id>, region to us-east-1
```

It globs `modules/**/kb.md`, pushes each as an inline `TEXT` document keyed by its
repo-relative path (`customDocumentIdentifier.id`) so re-runs **upsert** (no dupes), and ingests
directly (CUSTOM inline needs no separate `start-ingestion-job`). Editing a guide = edit the `.md` +
re-run the script; no image rebuild. Discover the ids with `aws bedrock-agent list-knowledge-bases`
+ `list-data-sources`. Operator-wide refresh = fan the script over every customer (same shape as the
canonical-schema reseed).

**Then it prunes.** Upsert alone would let a DELETED playbook stay indexed and retrievable by
`search_guides` forever, with the repo and the KB silently disagreeing. So the second half lists the
indexed documents and deletes any whose file is gone. Because the ids ARE the repo paths, that is a
set difference, and because nothing else ingests into this data source, a document with no file
behind it is stale by definition. The rail against a bad diff deleting live playbooks is the
existing "no `kb.md` found" abort: a wrong repo root exits before it can conclude that everything is
stale. `--dry-run` prints both halves and calls nothing that writes.

## deploy notes

- Provider must support S3 Vectors + `aws_bedrockagent_*` — locked at `hashicorp/aws 6.45`.
- `module.playbooks` applies before `module.agent` automatically (the agent references its output).
  For a scoped apply use `-target=module.playbooks -target=module.agent` to avoid the per_customer
  full-plan gateway-target churn (see `prod/per_customer/AGENTS.md` / the agent image-deploy notes).
