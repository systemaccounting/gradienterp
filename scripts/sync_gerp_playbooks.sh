#!/usr/bin/env bash
# One gerp's playbooks sync (issue #59): the region off its row, its knowledge base and data source
# by name in its account, then scripts/sync_playbooks.sh over the playbooks changed since the commit the
# gerp last synced (`GERP#playbooks_commit` on its settings table, stamped after each sync; no stamp, or
# one this tree does not descend from, is every playbook). Prints the gerp's summary section on stdout;
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
# the stamp: what this gerp last synced; the diff from it is what to ingest
table="gerp-settings-$GERP"
key="{\"gerp_id\": {\"S\": \"$GERP\"}, \"sk\": {\"S\": \"GERP#playbooks_commit\"}}"
stamp="$(aws dynamodb get-item --table-name "$table" --profile "gerp-$GERP" --region "$region" --no-cli-pager \
  --key "$key" --output json | jq -r '.Item.value.S // ""')"
head="$(git rev-parse HEAD)"
if [ -n "$stamp" ] && git cat-file -e "$stamp^{commit}" 2>/dev/null && git merge-base --is-ancestor "$stamp" "$head"; then
  ONLY="$(git diff --name-only "$stamp" -- modules | grep '/kb.md$' || true)"   # the tree as it is, uncommitted edits included
  export ONLY
  echo "==> $GERP: since ${stamp:0:12}, $(grep -c . <<< "$ONLY" || true) playbook(s) changed" >&2
else
  echo "==> $GERP: no stamp this tree descends from; every playbook" >&2
fi
out="$(mktemp)"
bash scripts/sync_playbooks.sh "$GERP" "$kb" "$ds" "gerp-$GERP" "$region" | tee "$out" >&2
aws dynamodb put-item --table-name "$table" --profile "gerp-$GERP" --region "$region" --no-cli-pager \
  --item "{\"gerp_id\": {\"S\": \"$GERP\"}, \"sk\": {\"S\": \"GERP#playbooks_commit\"}, \"value\": {\"S\": \"$head\"}}"
{ echo "### $GERP"; echo '```'; tail -1 "$out"; echo '```'; }
