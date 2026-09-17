# tests/puppet — the cross-firm integration puppet's capture infra.
#
# A puppet lets a test (or a human) play the OPPOSITE side of a cross-firm integration without
# standing up a second gerp. The whole cross-firm boundary is the addressed event (detail.to on
# the shared gerp-events bus), so a puppet needs just two halves: SEE events addressed to it, and
# EMIT events as itself. This stack is the SEE half — one rule + one queue:
#
#   gerp-events bus ──(detail.to prefix "puppet-")──▶ this rule ──▶ SQS inbox ──▶ puppet.sh --receive
#
# The EMIT half is `scripts/puppet.sh --send` (PutEvents; no infra). The operator dispatcher also
# sees puppet-addressed events, resolves detail.to in gerp-customers, finds no puppet, and DROPS
# gracefully (prod/platform/operator/lambdas/event_dispatcher) — so puppet traffic never reaches a
# real inbox and needs no dispatcher change. Apply/destroy via `scripts/puppet.sh --apply/--destroy`.
#
# Applies into the account that OWNS the gerp-events bus (the operator account); puppet.sh sets the
# creds (--acctid + --profile) and verifies them before touching anything. Local state (ephemeral).

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.65" }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "stack_prefix" {
  type    = string
  default = "gerp"
}

variable "hub_account" {
  description = "The hub's account (config.json HUBS): the capture edge is a rule on its bus, and the queue admits that rule."
  type        = string
}

variable "puppet_prefix" {
  type        = string
  default     = "puppet-"
  description = "detail.to prefix that routes to the capture queue. A puppet's gerp_id is any id starting with this."
}

provider "aws" {
  region = var.region
  # ambient creds — scripts/puppet.sh sets AWS_PROFILE for --acctid (the gerp-events bus owner)
}

locals {
  bus_name = "${var.stack_prefix}-events"
  name     = "${var.stack_prefix}-puppet"
}

# the puppet inbox — everything the tenant addresses to a puppet lands here for --receive/--poll
resource "aws_sqs_queue" "inbox" {
  name                       = "${local.name}-inbox"
  message_retention_seconds  = 3600 # test traffic — short retention
  receive_wait_time_seconds  = 10   # long-poll friendly (puppet.sh --poll)
  visibility_timeout_seconds = 30
}

resource "aws_sqs_queue_policy" "allow_events" {
  queue_url = aws_sqs_queue.inbox.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      # the capture edge on the hub (scripts/edge.sh add capture) delivers as the hub's edge role
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.hub_account}:role/${var.stack_prefix}-hub-edges" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.inbox.arn
    }]
  })
}

output "inbox_url" {
  value = aws_sqs_queue.inbox.url
}

