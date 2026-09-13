# secrets module

The gerp's vault. One `manage_secret` lambda: how a customer hands gerp any secret (provider
keys, tokens, a vendor app's client secret) **without it passing through the agent's context**,
and how the owner sees what is stored and deletes it.

## current features

- `manage_secret` lambda — three ops on `/gradienterp/customers/<id>/secrets/<name>` (scope
  `vault`, the default) or `/gradienterp/customers/<id>/automation/env/<name>` (scope
  `automation_env`, an env var in every cmd script):
  - `put {name, value, type?, overwrite?, scope?}` — writes a `SecureString` (or `String`);
    the caller supplies only the leaf `name` (charset `[A-Za-z0-9_-]`, ≤128) + `value`; answers
    `{status, name, scope}`, never the value. Refuses overwrite by default (409 on a name clash);
    `overwrite=true` rotates in place, SSM keeps prior versions. **Refused with 403 when the
    invoke carries the agent gateway's client context** (`bedrockAgentCoreToolName`): a value
    never comes from a model. Direct invokers put: the chat lambda as the `collect_secret`
    form's sink today, any lambda with invoke on it later.
  - `list {scope?}` — `{secrets: [{name, scope, updated_at}]}` by `DescribeParameters` on the
    path, which answers names and dates and no value.
  - `delete {name, scope?}` — removes the parameter; 404 when absent.
- the gateway tool — `manage_secret` on the customer's agent gateway (`register_with_agent`,
  `gateway_id`, `gateway_role_arn` from `module.agent`); its schema carries `op: list|delete`,
  `name`, `scope`, no `put` and no `value`.
- the form sink — the same lambda carries the `agent_frame_sink = "true"` tag (the `tags`
  argument of the shared lambda module; the chat lambda finds no sink without it). The chat
  lambda resolves `manage_secret` by that tag and invokes it with `{op: put, name, value}` after
  the owner submits the `collect_secret` form; the agent sees only `{ok, status, error?}`.
- IAM: `ssm:PutParameter` + `ssm:DeleteParameter` scoped to this customer's two subtrees,
  `ssm:DescribeParameters` (no resource; the call's path filter keeps it to the subtrees). No
  `GetParameter`: the vault lambda reads no value back.
- output: `manage_secret_fn_name`.

## the name is a handle

Consumers read a secret by name (`ssm:GetParameter WithDecryption`); the agent only ever holds
the name. The agent dictates the name conversationally ("name it `stripe_setup`") and passes
that same name to a consuming tool's `secret_name` parameter (`configure_webhook`,
`payment_links`, `manage_mcp`'s key path and `client_secret_name`). Values stay vaulted; the
agent causes a secret to be *used* without *seeing* it.

## intake (in-chat only)

There is **no HTTP route**. A value comes in through the agent's in-chat `collect_secret` form:
the owner asks to add a credential, the agent opens a secure field in the chat, and on submit the
**chat lambda invokes `manage_secret` directly** with the value the agent never sees. Access
control is the chat lambda's own JWT gate + IAM; see `modules/agent` `collect_secret`. Secrets
stay on this Cognito-authed surface deliberately — portal forms (capability-slug auth) carry
every OTHER intake, but a slug is a weaker credential than a credential deserves.

Rotation runs through the same form: the agent sets `collect_secret`'s `overwrite` flag only
after a refuse-overwrite error comes back and the owner confirms the replacement.

## derived vs owner-supplied

This module handles **owner-supplied** secrets (the owner names them) — flat leaves at
`…/secrets/<owner-chosen-name>`. **Derived** secrets — e.g. the Stripe `whsec_` that
`configure_webhook` captures from Stripe's response — are written by the deriving tool **nested
under a `<provider>/` folder** (`…/secrets/stripe/signing_secret`) so they can't collide with the
flat owner-submitted names, and read straight by the consumer, not through this intake. `list`
shows them under their folder name.
