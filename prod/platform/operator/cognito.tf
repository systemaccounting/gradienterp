###############################################
# Cognito user pool — single auth identity for the platform.
#
# Account is the cognito identity. Capabilities (public_user, erp_instance)
# are tiles toggled on the account dashboard; tower's provision_customer
# lambda fires when an account toggles erp_instance — an explicit post-signup capability action.
#
# POC config: email-as-username, hosted UI on a cognito prefix domain
# (no custom domain / ACM yet). MFA off.
#
# SES REACH, deliberately asymmetric across the platform — same service, opposite requirement:
#   - THIS account (operator) must reach STRANGERS. Someone signing up has verified nothing, so the
#     operator needs SES PRODUCTION ACCESS — GRANTED 2026-08-06 (case 178606059100092), 50k/day at
#     14/sec. Any recipient now receives; nothing here is sandbox-limited.
#   - each CUSTOMER account stays in sandbox FOREVER. `modules/agent/infra/email.tf` gives every gerp
#     an identity on <gerp_id>.agents.gradienterp.cloud, and sandbox is what stops an autonomous
#     agent mailing arbitrary addresses — an allowlist by construction, for free. Do not "fix" a
#     customer account out of sandbox; that removes a safety boundary, it doesn't unblock anything.
# Sandbox status is per-account, so the two never interfere.
###############################################

resource "aws_cognito_user_pool" "main" {
  name = "gradienterp"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Verification + recovery mail goes out through SES on gradienterp.cloud, not Cognito's built-in
  # sender. Both reasons are production reasons, not test ones: COGNITO_DEFAULT caps at 50 emails/day
  # PER POOL — signups plus password resets for the entire platform — and it sends from an
  # AWS-branded no-reply@verificationemail.com rather than our own domain.
  #
  # The identity itself is created in prod/email (same account, different stack). The ARN is
  # constructed rather than pulled through remote_state — same reasoning as the post_confirmation ARN
  # below, which avoids a circular dependency between the stacks.
  email_configuration {
    email_sending_account = "DEVELOPER"
    from_email_address    = "no-reply@${local.mail_domain}"
    source_arn            = local.mail_identity_arn
  }

  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
    require_uppercase = true
  }

  mfa_configuration = "OFF"

  # An email change keeps the OLD address as the login until the new one confirms a code. Without
  # this, UpdateUserAttributes on `email` switches the login immediately and unverified — the one
  # thing a change-email flow must never do. Cognito still never notifies the old address and has
  # no undo, so this is the whole of what it offers.
  user_attribute_update_settings {
    attributes_require_verification_before_update = ["email"]
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Post-confirmation trigger fires once a user verifies their email. Points
  # at tower's cognito_post_confirmation lambda by constructed ARN — Cognito
  # doesn't validate the function exists at user-pool creation time, so the
  # initial apply order is operator first (this), tower second (creates the
  # actual lambda + lambda_permission). Avoids circular terraform_remote_state.
  lambda_config {
    post_confirmation = "arn:aws:lambda:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:function:tower-cognito-post-confirmation"
  }
}

resource "aws_cognito_user_pool_client" "gradienterp_cloud" {
  name         = "${local.stack_prefix}-cloud"
  user_pool_id = aws_cognito_user_pool.main.id

  # Public SPA client — no secret. Hosted UI flow (OAuth code).
  generate_secret = false

  # Callback URLs. gradienterp.cloud = future custom domain; localhost = local dev;
  # the execute-api URL = the deployed gerp-website BFF (interim, until the custom
  # domain lands — see prod/gradienterp_cloud/TODO.md).
  #
  # var.chat_callback_urls appends each gerp's web-chat Function URL so the chat's
  # own "Sign in with Cognito" completes the hosted-UI code flow back to itself
  # (the chat lambda exchanges the code server-side). One shared client is fine at
  # this scale; at fleet scale source this list from the gerp-customers table
  # (each row carries its chat_url) instead of tfvars. Cognito caps at 100 callbacks.
  callback_urls = concat([
    "https://gradienterp.cloud/auth/callback",
    "http://localhost:3000/auth/callback",
    "https://oq5y2j1trc.execute-api.us-east-1.amazonaws.com/auth/callback",
  ], var.chat_callback_urls)
  logout_urls = concat([
    "https://gradienterp.cloud",
    "http://localhost:3000",
    "https://oq5y2j1trc.execute-api.us-east-1.amazonaws.com",
  ], var.chat_callback_urls)

  allowed_oauth_flows = ["code"]
  # `aws.cognito.signin.user.admin` is what lets an OAuth access token call UpdateUserAttributes /
  # VerifyUserAttribute — the login-email change on Info & Billing. Without it Cognito answers
  # "Access Token does not have required scopes".
  allowed_oauth_scopes                 = ["openid", "email", "profile", "aws.cognito.signin.user.admin"]
  allowed_oauth_flows_user_pool_client = true
  supported_identity_providers         = ["COGNITO"]

  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]
}

resource "aws_cognito_user_pool_domain" "main" {
  domain                = "${local.stack_prefix}-auth"
  user_pool_id          = aws_cognito_user_pool.main.id
  managed_login_version = 2 # Managed Login (new) — branded login experience (see aws_cognito_managed_login_branding below)
}

# Managed Login branding for the owner-app client. v2 only changes the login PAGE
# rendering; the OAuth code flow / tokens are unchanged. Other clients (chat,
# smoke-test) get the default v2 UI.
#
# Branded dark theme: settings (managed-login-settings.json) forces dark mode, the
# brand #0f1115 page / #13151a card, and the gradientERP gradient button; the form
# logo is the outlined lockup SVG (docs/images/art — same asset the web banner uses).
resource "aws_cognito_managed_login_branding" "gradienterp_cloud" {
  user_pool_id = aws_cognito_user_pool.main.id
  client_id    = aws_cognito_user_pool_client.gradienterp_cloud.id
  # decoded and re-encoded so the value is terraform's canonical JSON — byte-equal to what the
  # provider stores, which is what keeps this from planning an update on every run
  settings = jsonencode(jsondecode(file("${path.module}/managed-login-settings.json")))

  asset {
    category   = "FORM_LOGO"
    color_mode = "DARK"
    extension  = "SVG"
    bytes      = filebase64("${path.module}/cognito-logo.svg")
  }
  asset {
    category   = "FORM_LOGO"
    color_mode = "LIGHT"
    extension  = "SVG"
    bytes      = filebase64("${path.module}/cognito-logo.svg")
  }
}

# Test-only client. ALLOW_USER_PASSWORD_AUTH lets CI/local smoke tests drive
# the FORCE_CHANGE_PASSWORD flow via the CLI (initiate-auth →
# respond-to-auth-challenge), no browser needed. The frontend client
# (gradienterp_cloud) stays SRP-only so production passwords never transit
# even encrypted-in-transit. Used by .github/workflows/create-account.sh.
resource "aws_cognito_user_pool_client" "smoke_test" {
  name            = "smoke-test"
  user_pool_id    = aws_cognito_user_pool.main.id
  generate_secret = false

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]
}

# The firms' machine identity. modules/mcp makes one client-credentials app client per gerp on
# this pool (from the per_customer apply, through the operator provider); its token carries this
# scope and the gerp's vendor gateway trusts the client. The resource server is the pool's, once.
resource "aws_cognito_resource_server" "gerp_mcp" {
  identifier   = "gerp-mcp"
  name         = "gerp-mcp"
  user_pool_id = aws_cognito_user_pool.main.id

  scope {
    scope_name        = "call"
    scope_description = "call a gerp's vendor gateway as the firm"
  }
}

# Sending authorization: let Cognito send AS the gradienterp.cloud identity. Same-account, but the
# identity is owned by another stack (prod/email), so the grant is explicit rather than implied.
resource "aws_ses_identity_policy" "cognito" {
  identity = local.mail_identity_arn
  name     = "${local.stack_prefix}-cognito-send"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cognito-idp.amazonaws.com" }
      Action    = ["ses:SendEmail", "ses:SendRawEmail"]
      Resource  = local.mail_identity_arn
      # Scoped to the one address the pool sends as. SES identity policies accept only SES's own
      # condition keys — `AWS:SourceAccount` is rejected as "unknown or unapplicable" — so this is
      # the available narrowing, and it is the meaningful one: Cognito may send AS no-reply@ and
      # nothing else on the domain.
      Condition = { StringEquals = { "ses:FromAddress" = "no-reply@${local.mail_domain}" } }
    }]
  })
}
