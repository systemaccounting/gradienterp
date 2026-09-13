# ─── custom AgentCore Browser — the default browser plus session RECORDING ───
#
# The browse_* tools drive this instead of aws.browser.v1 (BROWSER_ID env → the
# SDK's start(identifier=…)). What it buys: every drive records to the gerp's own
# encrypted uploads bucket under browse-recordings/ — the portal session itself
# becomes an audit artifact (the filing WAS driven, here's the replay), beyond
# the screenshots the agent takes deliberately.

resource "aws_iam_role" "browser_recording" {
  name = "${local.resource_prefix_snake}_browser_rec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "browser_recording" {
  name = "${local.resource_prefix_snake}_browser_rec"
  role = aws_iam_role.browser_recording.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${var.uploads_bucket_arn}/browse-recordings/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = var.uploads_kms_key_arn
      },
    ]
  })
}

# CreateBrowser assumes the role and tests its write to the bucket at create, and IAM is
# eventually consistent: the browser depends on the role's POLICY, not only the role its arn
# names, and waits for the policy to be readable ("does not have permission to write to S3
# location" — westwood's re-apply, 2026-09-06; the first vend won the race). Create-only.
resource "time_sleep" "browser_recording_policy" {
  depends_on      = [aws_iam_role_policy.browser_recording]
  create_duration = "15s"
}

resource "aws_bedrockagentcore_browser" "this" {
  depends_on         = [time_sleep.browser_recording_policy]
  name               = "${local.resource_prefix_snake}_browser"
  description        = "Recorded browser for ${local.customer.business_name} — portal drives replayable from the uploads bucket"
  execution_role_arn = aws_iam_role.browser_recording.arn

  network_configuration {
    network_mode = "PUBLIC"
  }

  recording {
    enabled = true
    s3_location {
      bucket = var.uploads_bucket
      prefix = "browse-recordings/"
    }
  }
}

# persistent browser profile — cookies/localStorage survive across sessions, so a
# portal login (secrets-at-fill-time) sticks: subsequent drives skip the sign-in AND
# the MFA interrupt until the portal's cookie expires. one profile per gerp — the
# agent IS one identity to its portals.
resource "aws_bedrockagentcore_browser_profile" "this" {
  name        = "${local.resource_prefix_snake}_profile"
  description = "Persistent portal identity for ${local.customer.business_name} — logins survive across browse sessions"
}
