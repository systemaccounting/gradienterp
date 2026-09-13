#!/usr/bin/env bash
# vend_hub.sh — vend a hub (prod/hub): an account in the hubs OU, one per region, through the
# same vends queue a gerp goes through (044). ~15 minutes; the build prints the HUBS entry for
# config.json when it finishes.
#
#   bash scripts/vend_hub.sh <region>            # e.g. us-east-1
#
# Runs as the operator account (AWS_PROFILE, default operator-org).
set -euo pipefail
REGION="${1:?region}"
export AWS_PROFILE="${AWS_PROFILE:-operator-org}"
QUEUE=$(aws sqs get-queue-url --queue-name tower-vends --query QueueUrl --output text --no-cli-pager)
aws sqs send-message --queue-url "$QUEUE" --no-cli-pager --output text --query MessageId \
  --message-body "$(jq -nc --arg r "$REGION" '{kind: "hub", hub_id: $r, region: $r}')"
echo "sent; follow with: aws logs tail /aws/lambda/tower-provision-customer --follow --filter-pattern '{ $.customer_id = \"hub-$REGION\" }'"
