# events — the firm's own event bus. One per gerp, for messages between two modules of the same firm.
#
# Not the operator's `gerp-events`, which is shared across every customer and carries what leaves the
# firm. Not the account's `default` bus either: `MatchedEvents` and `TriggeredRules` are per-bus, so
# on `default` ours would be mixed in with every service event the account emits.
#
# Producers and consumers live in the modules themselves. This is the only resource here.

variable "gerp_id" {
  description = "Logical tenant identifier; per-tenant resource-name suffix."
  type        = string
}

variable "stack_prefix" {
  description = "Resource-name prefix (e.g. `gerp`). Threaded from repo-root config.json."
  type        = string
  default     = "gerp"
}

variable "org_ids" {
  description = "The organizations whose accounts may put to this bus: the hub's edge for this gerp puts here."
  type        = list(string)
  default     = []
}

resource "aws_cloudwatch_event_bus" "internal" {
  name = "${var.stack_prefix}-internal-${replace(var.gerp_id, "_", "-")}"
}

# The gerp is a spoke: the hub's `spoke` edge (a rule on the hub's bus with this bus as its
# target) puts here as the hub's edge role, and the rule a receiving module declares on this bus
# takes it from there.
resource "aws_cloudwatch_event_bus_policy" "internal" {
  count          = length(var.org_ids) > 0 ? 1 : 0
  event_bus_name = aws_cloudwatch_event_bus.internal.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrgPutEvents"
      Effect    = "Allow"
      Principal = "*"
      Action    = "events:PutEvents"
      Resource  = aws_cloudwatch_event_bus.internal.arn
      Condition = { StringEquals = { "aws:PrincipalOrgID" = var.org_ids } }
    }]
  })
}

output "internal_bus_name" {
  description = "The firm's own bus. Producers PutEvents here; a consuming module puts its rule on it."
  value       = aws_cloudwatch_event_bus.internal.name
}

output "internal_bus_arn" {
  description = "For the `events:PutEvents` grant on a producing module's role."
  value       = aws_cloudwatch_event_bus.internal.arn
}
