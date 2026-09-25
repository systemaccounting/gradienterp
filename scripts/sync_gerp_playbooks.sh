#!/usr/bin/env bash
# One gerp's playbooks sync (issue #59): the region off its row, its knowledge base and data source
# by name in its account, then scripts/sync_playbooks.sh over the playbooks whose content changed since
# the gerp last synced (`GERP#playbooks` on its settings table, a map of each path's sha256, stamped
# after each sync; no stamp is every playbook). Prints the gerp's summary section on stdout;
# exits non-zero when the base is missing or the sync fails. Through the `gerp-<id>` profile, which
# `scripts/awsacct.sh --all` writes on a laptop and the aws action writes on a runner alike.
#
#   bash scripts/sync_gerp_playbooks.sh <gerp_id>
#
# Every gerp at once is this script per gerp: `parallel` in playbooks.yaml, `xargs -P` on a laptop.
set -euo pipefail
cd "$(dirname "$0")/.."
GERP="${1:?gerp id}"
region="$(aws dynamodb get-item --table-name gerp-customers --profile operator-org --no-cli-pager \
  --key "{\"gerp_id\": {\"S\": \"$GERP\"}}" --projection-expression '#r' --expression-attribute-names '{"#r": "region"}' \
  --output json | jq -r '.Item.region.S // "us-east-1"')"
kb="$(aws bedrock-agent list-knowledge-bases --profile "gerp-$GERP" --region "$region" --no-cli-pager --output json \
  | jq -r --arg n "playbooks-${GERP//_/-}" '.knowledgeBaseSummaries[] | select(.name == $n) | .knowledgeBaseId')"
ds="$(aws bedrock-agent list-data-sources --knowledge-base-id "$kb" --profile "gerp-$GERP" --region "$region" --no-cli-pager --output json \
  | jq -r '.dataSourceSummaries[] | select(.name == "repo-playbooks") | .dataSourceId')"
[ -n "$kb" ] && [ -n "$ds" ] || { echo "no playbooks knowledge base for $GERP in $region" >&2; exit 1; }
# the stamp: what this gerp last synced, as `{path: sha256}` on its settings table; the diff is by
# content, so a squash merge, a rebase or an uncommitted edit on a laptop changes nothing about it
table="gerp-settings-$GERP"
key="{\"gerp_id\": {\"S\": \"$GERP\"}, \"sk\": {\"S\": \"GERP#playbooks\"}}"
stamp="$(aws dynamodb get-item --table-name "$table" --profile "gerp-$GERP" --region "$region" --no-cli-pager \
  --key "$key" --output json | jq -c '.Item.value.S // "{}" | fromjson')"
[ -n "$stamp" ] || stamp="{}"   # get-item prints nothing at all for a row that is not there
hasher="$(command -v sha256sum || echo "shasum -a 256")"
now="$(find modules -type f -name kb.md -print0 | sort -z | xargs -0 $hasher | jq -Rn '[inputs | capture("^(?<h>[0-9a-f]+) +(?<p>.+)$") | {(.p): .h}] | add // {}')"
ONLY="$(jq -rn --argjson a "$stamp" --argjson b "$now" '$b | to_entries[] | select($a[.key] != .value) | .key')"
export ONLY
echo "==> $GERP: $(grep -c . <<< "$ONLY" || true) playbook(s) changed since the last sync" >&2
out="$(mktemp)"
bash scripts/sync_playbooks.sh "$GERP" "$kb" "$ds" "gerp-$GERP" "$region" | tee "$out" >&2
aws dynamodb put-item --table-name "$table" --profile "gerp-$GERP" --region "$region" --no-cli-pager \
  --item "$(jq -cn --arg g "$GERP" --arg v "$now" '{gerp_id: {S: $g}, sk: {S: "GERP#playbooks"}, value: {S: $v}}')"
{ echo "### $GERP"; echo '```'; tail -1 "$out"; echo '```'; }
