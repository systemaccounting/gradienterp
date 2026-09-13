###############################################
# The operator's own bus: the operator is a spoke of the hub.
#
# The hub (prod/hub, a vended account) holds the buses gerps put to and the edges between them;
# one `forward` edge puts every event not addressed to a gerp here, and the operator's consumers
# — the publisher, the counters, the archive (prod/api_openlyoperated), the platform's reports
# (issue_collector.tf) — are this bus's rules. Firehose cannot be a cross-account target, so the
# forward is what carries them.
###############################################

data "aws_organizations_organization" "this" {}

resource "aws_cloudwatch_event_bus" "operator" {
  name = "${local.stack_prefix}-operator"
}

resource "aws_cloudwatch_event_bus_policy" "operator" {
  event_bus_name = aws_cloudwatch_event_bus.operator.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OrgPutEvents"
      Effect    = "Allow"
      Principal = "*"
      Action    = "events:PutEvents"
      Resource  = aws_cloudwatch_event_bus.operator.arn
      Condition = { StringEquals = { "aws:PrincipalOrgID" = local.org_ids } }
    }]
  })
}
