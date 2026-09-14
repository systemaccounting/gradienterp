# hub — how it works

## current features

- **`gerp-events`**, the main bus: the gerps of this hub's region put every event that leaves the firm here (`OP_EVENT_BUS_ARN`), and a gerp anywhere puts an event addressed to one of this hub's gerps here, having read this hub's bus arn off the platform directory (`gerp-directory`, prod/platform/operator; modules/events `emit_to`); its policy admits `events:PutEvents` from the organization (`local.org_ids`). The event arrives with the sender's account on it, stamped by EventBridge at the put
- **edges**: rules on the main bus, added and removed through `manage_edges` — `spoke` (`gerp-edge-spoke-<gerp_id>`, exact `detail.to` → the gerp's own bus), `capture` (`gerp-edge-capture-<label>`, any pattern → a queue the caller owns); the `forward` edge (`gerp-edge-forward-operator`, everything with no `detail.to` → the operator's `gerp-operator` bus) is declared here. Nothing here names another hub
- **the sets**: `EDGE_SETS` in `config.json` are shares of the rules-per-bus quota (`L-244521F2`, read live by the door) less the hub's own rules; a set at its size refuses an add (409, naming the quota); `EdgesUsedPercent` (`gerp/platform`, dimensions `hub`, `set`) is published on every change and `gerp-hub-<hub>-edges-<set>-80pct` alarms on the ops topic
- **the edge role** `gerp-hub-edges`: `events:PutEvents` on any bus in the organization and `sqs:SendMessage` on any queue in it; every edge target carries it (a cross-account target needs a role)
- **`gerp-edges-failed`**: the queue every edge target parks a failed delivery on; `gerp-edges-parked` on the ops topic stays until the message is redriven or removed
- **`manage_edges`**, the door: `add {kind, to, account_id | target, pattern}`, `remove {kind, to}`, `list {kind?}`; the operator account may invoke it (the provisioner, `close_account`, `scripts/edge.sh`, `scripts/puppet.sh`)

## a hub is vended

`bash scripts/vend_hub.sh <region>` puts `{kind: hub, hub_id, region}` on the vends queue; the
provisioner vends an account into the organization's `hubs` OU (no region pin; the trust stackset
deploys `OperatorOrchestration` there) and starts the `tower-hub` build (`.codebuild/hub.yml`),
which applies this stack with `hub_id`, the account and the region, state at
`hubs/<hub_id>/terraform.tfstate`. The build prints the `HUBS.<hub_id>` entry for `config.json`
(`account`, `region`, `bus_arn`, `manage_edges_arn`); a tower apply carries it to the provisioner
and `close_account`, and a `per_customer` apply points a gerp of that region at the bus. No other
hub is touched: a sender finds this hub through the directory row a vend writes. An existing hub
re-applies with `bash scripts/apply.sh --stack hub --region <region>` (`--plan` for a plan build), the
same `tower-hub` build a vend starts. Directly: `terraform init -backend-config="key=hubs/<hub_id>/terraform.tfstate"` and
`TF_VAR_hub_id`, `TF_VAR_aws_account_id` under `AWS_PROFILE=operator-org`.

## an edge, by kind

| kind | pattern | target | who |
|---|---|---|---|
| `spoke` | `{"detail":{"to":["<gerp_id>"]}}` | `arn:aws:events:<region>:<gerp account>:event-bus/gerp-internal-<gerp_id>` | `provision_customer` at vend; `close_account` removes before `CloseAccount` |
| `capture` | the caller's | an SQS queue arn; its policy admits the edge role | `scripts/puppet.sh`, a person |
| `forward` | `{"detail":{"to":[{"exists":false}]}}` | `arn:aws:events:<region>:<operator>:event-bus/gerp-operator` | this stack |

A `to` nobody holds a spoke for is undelivered and unlogged. It does not happen from a gerp's
emitter: `emit_to` refuses a recipient the directory does not hold, and the directory row and
the spoke are written by the same vend and removed by the same close. A stopped gerp keeps its
spoke: its bus is gone with its stack, the deliveries park on `gerp-edges-failed`, and
its apply (`apply.sh --stack per_customer`) redrives them.

`scripts/edge.sh <hub> list | add <kind> <to> … | remove <kind> <to>` is the door for a person.

## the gerp's side

`modules/events` puts the organization statement on the gerp's own bus so the edge role may put
there; `modules/inbox` declares the `consume` rule (`to = self` → `receive_inbound`) with its
`-consume-failed` queue and `-consume-parked` alarm. The gerp's row carries `region`, `hub` (the
bus arn) and `org`, written at vend.
