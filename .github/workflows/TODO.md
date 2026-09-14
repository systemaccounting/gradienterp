# .github/workflows — open work

Two workflows run only when dispatched — `deploy.yaml` (lambdas and the agent image, one job per gerp)
and `apply.yaml`, from an uploaded `source.zip` (`scripts/dispatch.sh` uploads the tree, starts one and
waits on it). Three run on every pull request and every push to `main`: `unit.yaml`
(`bash scripts/test.sh`), `e2e.yaml` (`bash scripts/e2e.sh` against the local stack, the seed written by
`--configure`) and `terraform.yaml` (`terraform fmt -check`, then `tf-validate-all.sh`). None needs AWS.
A job that does reaches it through the `prod` environment (`environment.sh`: deployments from `main`
only, no reviewers) and the shared step `.github/actions/aws`, which takes `gerp-github-deploy`
(`prod/platform/management/github_deploy.tf`) with the environment's `AWS_DEPLOY_ROLE_ARN` and writes the
profiles the scripts read.
GitHub itself runs secret scanning with push protection, Dependabot alerts and Dependabot security
updates, set in the repo settings. The
other `.sh` files here are operator-run helpers; Actions ignores `.sh`. Current docs for everything below: fetch
`code.claude.com/docs/llms.txt` and pull pages as raw markdown with an `.md` suffix.

## terraform validate time

- [ ] **validate takes about four minutes.** `tf-validate-all.sh` runs one directory per core on a
      4-core runner, and 8 at a time measured 3m39s — the work is CPU. Split the directories across
      parallel jobs (a matrix, the script validating one shard of the sorted list), tried on a branch
      with a pull request.

## the issue-triage workflow (the escalation loop's outside net)

claude-code-action on `issues.opened` / `issue_comment`: labels (`bug|feature`, module),
asks-for-missing, and the merge net — linking hash-distinct issues that are semantically
one defect when the operator agent missed the match. Board hygiene only; filing decisions
belong to the operator agent (prod/platform/operator/TODO.md § the operator agent).

- [ ] **provider: the Anthropic API, not Bedrock** — this bot's entire context is public
      by construction (the public repo + the public board; the platform's private halves
      never reach github), so data-residency buys nothing, and the API carries the newest
      models while the org's Bedrock sits behind the claude-5 gate. One repo secret
      (`ANTHROPIC_API_KEY`), zero AWS infra. Copilot cloud agent evaluated and rejected
      for this duty: its event-triggered automations are private/internal-repo only, its
      credits are token-metered not flat, and its Claude ceiling trails the API. Its
      assign-issue→PR shape stays a candidate for the fix leg once PR traffic is real.
- [ ] **the custom GitHub App** (owner clicks, per the docs): Contents/Issues/PRs R+W,
      webhooks off, installed on this repo only; `APP_ID` + `APP_PRIVATE_KEY` repo
      secrets; workflows mint tokens via `actions/create-github-app-token`.
- [ ] **the workflow yaml** — claude-code-action@v1, triage prompt via `prompt` (label,
      link dupes, ask for whats missing; never close — closing is the operator agent's,
      keyed to release annotations), limits via `claude_args` (`--max-turns`, `--model`).
- [ ] **practice first** — the whole loop rehearses on a throwaway public repo before the
      real one exists; repo name is config in the operator agent's SSM, so cutover is a
      value change.

## later (with the repo)

- [ ] **the fix leg** — @claude mentions on triaged issues yield PRs (same action, same
      key); artifact push deploys; release annotations (`deploy.sh push --notes`) name the
      signature_hashes they fix, which is what lets the operator agent close classes and
      tenants' agents tell their owners "fixed yesterday." Tool extension when it lands:
      `claude_args --mcp-config` mounts mcp servers per-workflow — e.g. AWS docs over http
      (`knowledge-mcp.global.api.aws`, no auth, no install) so infra PRs check api behavior
      first. Mcp tools are deny-by-default; name each in `--allowedTools`.
- [ ] **PR review** — the code-review workflow (docs page `code-review.md`) posts
      multi-agent reviews on every PR, no @-trigger; distinct from triage (issues) and the
      fix leg (authoring).
- [ ] **script conversions** — `create-account.sh` + `delete-test-user.sh` become a scheduled
      cognito smoke with cleanup. The workflow's log is public: `create-account.sh` echoes the
      generated password on success, which goes before the conversion. `per-customer-apply.sh` stays operator-run (provisioning walks are the
      operator agent's, not CI's).
