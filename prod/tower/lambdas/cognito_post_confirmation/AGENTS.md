# cognito_post_confirmation

## current features

- the `gradienterp` user pool's post-confirmation trigger: on `PostConfirmation_ConfirmSignUp` it seeds the account's private profile row and returns the event so signup completes
- a failed seed is logged and signup still completes; the BFF creates the row on the account's first read

## what it does

Cognito triggers run synchronously with a five-second wall, so this function writes one row and returns.

1. receive the Cognito event (`triggerSource`, `request.userAttributes` with `sub` and `email`, `request.clientMetadata` with `first` and `last` from the SPA's ConfirmSignUp)
2. `gerp-accounts` put, key `account_id = sub`, `attribute_not_exists` so an existing profile is kept
3. return the event unchanged

Signup creates the Cognito identity and this row. A sub-account is vended when the account creates a gerp in the owner app and a card lands: the BFF sends the vend to `tower-vends` (`prod/tower/AGENTS.md` § the vends queue).

## env vars

| var | source |
|---|---|
| `ACCOUNTS_TABLE` | `gerp-accounts` (operator account) |
