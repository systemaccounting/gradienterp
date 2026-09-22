# closure_requester — the one role a customer account may assume to ask for a gerp to be closed.
#
# Closing a gerp is three acts in THIS account: mark the customer row, start the `tower-per-customer`
# build that exports the firm's records and destroys `prod/per_customer`, and fifteen days later
# invoke `tower-close-account`. All three are made by the closure scripts in gradienterp's own gerp —
# a different account — whether the closure began with an unpaid invoice or a customer's request
# (`prod/gradienterp_cloud/bff/main.py`, POST /api/gerps/close, hands the request to those scripts).
#
# A ROLE rather than a stored credential. There is nothing to rotate, nothing to leak, and the trust
# policy is the audit: who may ask is a terraform list, not a secret someone holds.
#
# **Off until named.** With config.json `CLOSURE_REQUESTER_ACCOUNTS` empty the role trusts nobody;
# it holds gradienterp's account since its collection sequence went live. Same posture as `close_build_project` — the machinery exists and does nothing
# until switched on deliberately, and for the same reason: what it does cannot be undone.
#
# The grant is three statements and no more. It cannot read a customer's data, cannot touch another
# table, cannot start another build. Everything about WHICH gerp and whether it is due comes from the
# caller's own request; the authority here is only "may ask at all".

locals {
  # AWS account ids allowed to assume the closure-requester role. A customer's gerp never appears.
  closure_requester_accounts = local.config.CLOSURE_REQUESTER_ACCOUNTS
}

resource "aws_iam_role" "closure_requester" {
  count = length(local.closure_requester_accounts) == 0 ? 0 : 1
  name  = "${local.stack_prefix}-closure-requester"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      # The ACCOUNT, not a role inside it — the caller's own IAM decides which of its principals may
      # assume, and that half lives with the runner in modules/automation. Naming a role here would
      # pin this stack to another stack's resource name.
      Principal = { AWS = [for a in local.closure_requester_accounts : "arn:aws:iam::${a}:root"] }
      Condition = {
        # A caller that cannot say which gerp it means is not asking for a closure.
        StringLike = { "sts:RoleSessionName" = "closure-*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "closure_requester" {
  count = length(local.closure_requester_accounts) == 0 ? 0 : 1
  name  = "${local.stack_prefix}-closure-requester"
  role  = aws_iam_role.closure_requester[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # mark the row, and read it: a requested closure's re-read is "does the row still say
        # close_requested", and the row is where the aws_account_id lives
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.customers.arn
      },
      {
        # fifteen days after the build: close the AWS account, through tower's lambda that holds the
        # management-only call
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:tower-close-account"
      },
      {
        # and the build itself. One project; TF_ACTION rides in the caller's overrides, which is why
        # the build's own role is what bounds what a destroy can reach.
        Effect   = "Allow"
        Action   = "codebuild:StartBuild"
        Resource = "arn:aws:codebuild:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:project/tower-per-customer"
      },
    ]
  })
}

output "closure_requester_role_arn" {
  description = "Assumed by a gerp's automation to request a closure. Empty when nobody is trusted."
  value       = length(local.closure_requester_accounts) == 0 ? "" : aws_iam_role.closure_requester[0].arn
}
