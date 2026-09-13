# region — a region a gerp can be built in

This module is the operator's side of one region; this file is the map of everything a region
is, since no other directory holds the whole. A region is an entry in `config.json`, and a gerp
is built where its owner's country is.

## current features

- `config.json` names a region four ways: `REGIONS` (label, `model`, `offered` / `not yet`,
  the countries it is the default for — eight regions: us-east-1, eu-west-1, eu-central-1,
  eu-west-2, ap-southeast-1, ap-northeast-1, ap-southeast-2, ap-south-1), `HUBS` (the
  region's hub account, bus and edges door), `OAM_SINKS` and `OPS_ALERTS_TOPICS` (this
  module's outputs, by region). **The model is the region's `model` entry**, set by the
  platform for every gerp in the region: the full inference profile id — the best profile that
  keeps inference in the geography, read by per_customer
  for the runtime and by the provisioner for the Marketplace agreement (on the model under the
  prefix, whatever the prefix: `us.`, `eu.`, `apac.`, `jp.`, `au.`, `global.`)
- this module, once per region (`../regions.tf`, one provider alias each): the artifact bucket
  lambda code deploys from, filled by S3 replication from the us-east-1 bucket; the OAM sink a
  gerp's account links its telemetry to; the ops topic a gerp's alarms notify, subscribed to the
  us-east-1 issue collector and the ops mailbox; the ECR repository the agent image replicates
  into, with an organization-read policy (replication carries images, never policies)
- the customers OU per region, pinned to it (`prod/platform/management/regions.tf`); every pin
  reads one exceptions list, `local.region_pin_exceptions` in management's `main.tf`: global
  services, `bedrock:*`, `aws-marketplace:*`, `s3:Get*` / `s3:List*`, `events:PutEvents`
- a hub per region (`prod/hub`; `bash scripts/vend_hub.sh <region>`) and a `gerp-directory`
  replica per region (`prod/platform/operator/regions.tf`); an addressed event is put on the
  recipient hub's bus, in the recipient's region, resolved from the directory
  (`modules/events` `emit_to`); hubs hold nothing about each other
- the vend carries `region` from the create screen (the address's country's region by default)
  into the OU, the hub's spoke, the directory row, the model agreement and the build's
  `CUSTOMER_REGION` (`../AGENTS.md` § where a gerp lives); the template takes `aws_region` and
  reads the region's model, sink and topic off config
- the alarm path reads in the gerp's region: the collector takes the region off the alarm's arn
  and writes `region:` on the task; `scripts/investigate.py` and the operator gerp's
  `read_fleet_logs` open there
- global services stay in us-east-1: the Cognito pool, Marketplace, Route 53, the seller gerp,
  `gerp-customers`, the tfstate bucket; every arn built for a gerp reads `region` off its row

## what each region runs on (config.json `REGIONS[region].model`, read 2026-09-12)

| region | model | in geography |
|---|---|---|
| us-east-1 | `us.anthropic.claude-sonnet-4-6` | yes |
| eu-west-1, eu-central-1, eu-west-2 | `eu.anthropic.claude-sonnet-4-6` | yes |
| ap-northeast-1 (Tokyo) | `jp.anthropic.claude-sonnet-4-5-20250929-v1:0` | yes |
| ap-southeast-2 (Sydney) | `au.anthropic.claude-sonnet-4-5-20250929-v1:0` | yes |
| ap-southeast-1 (Singapore), ap-south-1 (Mumbai) | `apac.anthropic.claude-sonnet-4-20250514-v1:0` | yes; `global.` 4.6 / 5 exist there and leave it |

There is no `apac.` profile for Sonnet 4.6 or Sonnet 5; Tokyo and Sydney have country profiles
a generation ahead of `apac.`. The Sonnet 5 flip is one entry per region (`TODO.md`).

## what lives where

| piece | where |
|---|---|
| the region list, each region's model, the per-region arns | `config.json` |
| the OU, its pin, its Control Tower baseline, the trust stackset | `prod/platform/management/regions.tf` |
| the pins' shared exceptions | `prod/platform/management/main.tf` |
| the artifact bucket, sink, topic, ECR repo | this module; `../regions.tf` instantiates it and holds the S3 and ECR replication |
| the directory replica | `prod/platform/operator/regions.tf` |
| the hub | `prod/hub`, vended by `scripts/vend_hub.sh` |
| the vend by region | `../lambdas/provision_customer`, `../lambdas/close_account` |
| the build in the region | `.codebuild/per-customer.yml` (`CUSTOMER_REGION` → `AWS_REGION`; `OPERATOR_REGION` kept for the operator's table and topic) |
| deploying code to a gerp elsewhere | `scripts/deploy.py` (`bucket_for`, waits for the replica's checksum) |
| the create screen's region | `prod/gradienterp_cloud` (the dropdown, the refusal of a `not yet` region) |

## adding a region

1. the entry in `config.json` `REGIONS` (`status: "not yet"` until the rest is up; the create
   screen greys it) with its `model`: the best in-geography profile
   (`aws bedrock list-inference-profiles --region <r>`)
2. the landing zone governs it (`prod/platform/management/control_tower.tf`, `local.regions`;
   tens of minutes) and `regions.tf` builds its OU, pin, baseline and stackset instance
3. a provider alias and a module block in `../regions.tf`; apply tower; `aws s3 sync` the
   us-east-1 artifact bucket into the new one once; the `regions` output's sink and topic arns
   into `OAM_SINKS` and `OPS_ALERTS_TOPICS`
4. the directory replica (`prod/platform/operator/regions.tf`), then `vend_hub.sh <region>` and
   its `HUBS` entry
5. `status: "offered"`, a tower apply (the provisioner's env carries the lists), a BFF push

## measured

On the Irish gerp (`dublin-test-roasters-d542eb`, eu-west-1, 2026-09-12): the vend 22 s to an
active account, the per-customer build 10 min to `marked active`, a `po.proposed` from
gradienterp a row in its inbound table one second after the request, with no rule added on
either hub. The create screen says about 25 minutes: Account Factory takes ~12 of them on a
fresh account.
