# export — open work

## closure: export, then tear the instance down around it

The shape of the whole thing, decided 2026-08-14. A gerp closes — the owner asked, or an invoice
went unpaid long enough — and what has to happen is: run the export to completion, destroy the
instance, keep the copy reachable for fifteen days, then delete that too.

**The clock, end to end.** Day 0 the invoice is unpaid. Day 7 we say so. Day 15 the account closes:
the export runs and the instance is destroyed. Day 30 the bucket goes and the AWS account with it.
A voluntary closure enters the same path at day 15; only the reason differs.

**Why the instance comes down at closure rather than at the end.** The fifteen days is for
DOWNLOADING an export that already exists as objects in S3 — presigned URLs and an IAM role, no
gateway, no runtime, no lambdas but one. Leaving the instance up for it means carrying a live gerp's
AWS spend for two weeks, billed to someone who by definition is not paying. Destroying first costs
storage on one export instead.

That is also a wording fix owed to `docs/TERMS.md`, which currently says the account "keeps running"
for the window. It does not need to, and saying so implies the owner can keep USING it, which is not
what is meant.

### what has to survive the destroy

Not "the bucket". Four things, and the second is the one that quietly destroys the data:

| | |
|---|---|
| `aws_s3_bucket.uploads` | the export objects, and the filing cabinet they were copied from |
| `aws_kms_key.uploads` | the bucket is SSE-KMS. Destroy the key and every object is permanently unreadable, while the bucket still sits there looking kept |
| `aws_iam_role.reader` | what the download script assumes. Without it the script in the export is inert |
| `export_gerp` + its role | the credential is ONE HOUR (role chaining caps at 3600s) and the window is fifteen days, so something must be able to re-issue. `{"credentials_only": true}` is that something |

All four are free at rest — an idle lambda, a role, a key, and object storage.

**But a lambda needs a caller, and the caller was the agent.** Re-issuing is a person asking their
agent for a fresh link; "expiry is a sentence, not a support request" assumes something is listening
to the sentence. Destroy the agent and the survival set is inert — the link dies in an hour and
there is no way to ask for another.

Keeping the agent up for the window means keeping the runtime, the gateway and memory, which is most
of the stack and spends what the teardown was for. So the door moves instead: the BFF is in the
OPERATOR account and outlives any customer closure by construction. The owner signs into
gradienterp.cloud, opens the closed gerp, and the BFF invokes `export_gerp` cross-account for fresh
links — the same shape as the card-saving pair already reaching into a customer account.

Which makes the account-screen entry point below a PREREQUISITE of closure rather than a
convenience: after the teardown it is the only door.

### the split, as built

`prod/init_customer` — its own stack, its own statefile (`<gerp_id>/init.tfstate`) — holds the four.
`prod/per_customer` finds the bucket and CMK by name with data sources rather than remote state, and
`modules/export` is now two modules: `infra` (the lambda, in init) and `gateway` (its MCP target
registration, in per_customer, because the gateway dies at closure and the lambda must not).

A module split alone would not have done it: `terraform destroy` destroys a statefile, and module
nesting does not survive that.

Migrated on gradienterp by importing the 14 live resources into the new state and removing them from
the old — no resource was recreated. Verified after: per_customer plans clean, and its DESTROY plan
takes 542 resources without touching the bucket, the CMK, the reader role or the lambda.

- [ ] **the day-30 sweep** — delete the bucket and close the AWS account. Has to verify the export
      happened before it runs, or a failed export at day 15 becomes a silent deletion at day 30.

## the rest

- [ ] **the unpaid sequence cannot ask.** The owner can; day 15 of an unpaid invoice still has no
      code behind it. Same `StartBuild`, different caller — `prod/tower/TODO.md` § lifecycle.
- [ ] **documents ignore `include`.** Narrowing names tables, and the filing cabinet is not a table,
      so a narrowed export still copies every document. Fine while narrowing is for dropping bulky
      logs; wrong the moment someone narrows to get a small copy fast. Needs a name in the same
      namespace as the tables rather than a second flag.
