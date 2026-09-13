# prod/init_customer

The part of a gerp that outlives the gerp. Applied **first** when a customer is provisioned and
destroyed **last** when they leave.

## current features

- the encrypted document store (`<prefix>-agent-<gerp_id>-uploads-<account>`) and its CMK, plus
  every piece of bucket configuration: versioning, the one lifecycle configuration, public-access
  block, default SSE-KMS, CORS
- `modules/export/infra` — `export_gerp`, its role, and the download reader role. The lambda only;
  its gateway registration is `modules/export/gateway`, instantiated by `per_customer`
- `force_destroy` on the bucket, so the day-30 sweep can actually delete it
- `gerp-ops-read` — the read-only role every investigator of this gerp assumes: logs, metrics,
  alarms, the failed queues, the tables' descriptions; no data reads, no writes. Its principals
  are the operator account (a person, the collector, the daily edges run) and the operator
  gerp's agent runtime (`agentcore_<seller gerp>_execution`, `SELLER_ACCOUNT_ID` in config).
  Here rather than `per_customer` so a stopped gerp's account can still be read
- the OAM link to the operator's sink (`OAM_SINK_ARN` in config), so the gerp's logs and metrics
  read from the operator account without assuming at all

## why a separate stack and not a module

`terraform destroy` destroys a **statefile**. Module nesting is namespacing and does not survive it,
so a module split would have left the teardown owning the customer's books either way.

The alternatives are worse than they look. `-target` prunes what it does not name without saying so.
`state rm` before every teardown is a manual step in a destructive path, and it only has to be
forgotten once.

## what belongs here

One question decides it: **after the instance is gone, can the owner still download their export for
fifteen days?**

| | |
|---|---|
| the bucket | the export objects, and the filing cabinet they were copied from |
| the CMK | the bucket is SSE-KMS. Destroy the key and every object is permanently unreadable while the bucket sits there looking kept — the failure that looks like success |
| the reader role | what the download script assumes |
| `export_gerp` | the credential is one hour (role chaining caps at 3600s) and the window is fifteen days, so something has to issue a fresh one |

Everything else is `per_customer`'s. All four are free at rest: object storage, a key, a role, an
idle lambda.

## how per_customer finds it

By NAME, with data sources — no remote state and no outputs to thread:

    data "aws_s3_bucket" "uploads" { bucket = "<prefix>-agent-<gerp_id>-uploads-<account>" }
    data "aws_kms_key"   "uploads" { key_id = "alias/agentcore_<gerp_id>_uploads" }

Every name here is a function of `(stack_prefix, gerp_id, account)`, the same convention the BFF uses
to reach a customer's export lambda. Apply this stack second and per_customer fails at plan time,
which is the right failure — better than half a gerp built around a bucket that does not exist.

## apply

Provisioning applies it: `.codebuild/per-customer.yml` runs this stack's init and apply ahead of
per_customer's on every `TF_ACTION=apply` build, and never on a destroy. The source zip carries
`prod/init_customer` (`scripts/build-codebuild-source.sh`). By hand:

```bash
cd prod/init_customer/
terraform init -backend-config="key=<gerp_id>/init.tfstate"
terraform apply -var "gerp_id=<gerp_id>" -var "aws_account_id=<aws_sub_account_id>"
```

The state key is `<gerp_id>/init.tfstate`, beside per_customer's `<gerp_id>/terraform.tfstate`.

## the ephemeral hook on a durable bucket

`per_customer` owns one thing that touches this bucket: the S3 notification that pokes the agent's
continuation lambda. That is correct — the lambda is ephemeral, so the notification should die with
it. S3 allows one notification configuration per bucket, so it stays a single resource over there
rather than being split.

## teardown

Closure destroys `per_customer` — a plain whole-stack destroy, no `-target`, no `state rm`. Verified:
that plan destroys 542 resources and the only one touching this stack's bucket is the notification
above. The bucket, CMK, reader role and lambda all survive it.

This stack is destroyed at the end of the 15-day window, by the day-30 sweep, which has to check the
export actually happened first. Sequence: `modules/export/TODO.md` § closure.
