#!/usr/bin/env bash
# mailbox — read real inbound email from the gradienterp.cloud SES catch-all.
#
# `prod/email` already receives for the whole domain and writes every message to S3 before
# forwarding, so there is no stack to stand up here: this is a reader over state that already
# exists. Addresses under `test+…` are skipped by the forwarder, so test traffic never reaches a
# personal inbox.
#
# Usage:
#   bash scripts/mailbox.sh --address signup                    # create test+signup-<uuid>@…
#   bash scripts/mailbox.sh --wait <addr> --code                # block, print the 6-digit code
#   bash scripts/mailbox.sh --wait <addr> --link                # block, print the first link
#   bash scripts/mailbox.sh --wait <addr> --subject "Verify"    # only that subject
#   bash scripts/mailbox.sh --list                              # what has arrived lately
#   bash scripts/mailbox.sh --get <s3-key>                      # one message, in full
#
# Env:
#   MAILBOX_PROFILE   AWS profile (default operator-org — the account that owns the mail bucket)
#   MAILBOX_BUCKET    override the bucket (default gerp-mail-inbound-<operator-acct> via STS)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$HERE/tests/mailbox"

[[ -d "$DIR/node_modules" ]] || {
  echo "installing mailbox deps…" >&2
  ( cd "$DIR" && npm install --silent --no-audit --no-fund )
}

exec node "$DIR/cli.mjs" "$@"
