###############################################
# OperatorOrchestration role — deployed to every account in the customers OU
# via a service-managed CloudFormation StackSet with auto-deployment.
#
# Why: CT's AWSControlTowerExecution role on each customer account trusts ONLY
# the management account. Our codebuild (in operator) needs to assume into
# customer accounts to apply prod/per_customer/ terraform. Without this role,
# we're back to the chicken-egg trust dance the lambda used to do by hand.
#
# Auto-deployment: when a new account is moved INTO the customers OU (via SC
# Account Factory), AWS CloudFormation auto-deploys this stackset to it. No
# operator-side wait, no manual trust widening, no IAM consistency loops.
#
# Trust: operator account root. Permissions: AdministratorAccess. Bounded by
# the SCPs attached to the customers OU (region-pin + CT preventive controls)
# and by what operator's codebuild role can actually do upstream.
#
# Member account churn: stackset auto-removes from accounts that leave the OU
# (retain_stacks_on_account_removal = false).
###############################################

resource "aws_cloudformation_stack_set" "operator_orchestration" {
  name             = "operator-orchestration-role"
  description      = "OperatorOrchestration role on every customer account; trusts operator. Auto-deployed to customers OU."
  permission_model = "SERVICE_MANAGED"
  capabilities     = ["CAPABILITY_NAMED_IAM"]

  auto_deployment {
    enabled                          = true
    retain_stacks_on_account_removal = false
  }

  template_body = jsonencode({
    AWSTemplateFormatVersion = "2010-09-09"
    Description              = "OperatorOrchestration role for cross-account access from the operator account."
    Resources = {
      OperatorOrchestrationRole = {
        Type = "AWS::IAM::Role"
        Properties = {
          RoleName = "OperatorOrchestration"
          AssumeRolePolicyDocument = {
            Version = "2012-10-17"
            Statement = [{
              Effect    = "Allow"
              Principal = { AWS = "arn:aws:iam::${aws_organizations_account.operator.id}:root" }
              Action    = "sts:AssumeRole"
            }]
          }
          ManagedPolicyArns = [
            "arn:aws:iam::aws:policy/AdministratorAccess",
          ]
        }
      }
    }
  })
}

resource "aws_cloudformation_stack_set_instance" "operator_orchestration_customers" {
  stack_set_name = aws_cloudformation_stack_set.operator_orchestration.name

  deployment_targets {
    organizational_unit_ids = [aws_organizations_organizational_unit.customers.id]
  }

  # IAM is global; deploying once to us-east-1 is sufficient.
  stack_set_instance_region = var.aws_region

  operation_preferences {
    failure_tolerance_percentage = 0
    max_concurrent_percentage    = 100
    region_concurrency_type      = "PARALLEL"
  }
}

# The hubs OU gets the same role: a hub's build assumes it the way a gerp's does. A second
# instance, so the customers OU's stacks are never touched by a change to the hubs'.
resource "aws_cloudformation_stack_set_instance" "operator_orchestration_hubs" {
  stack_set_name = aws_cloudformation_stack_set.operator_orchestration.name

  deployment_targets {
    organizational_unit_ids = [aws_organizations_organizational_unit.hubs.id]
  }

  # IAM is global; deploying once to us-east-1 is sufficient.
  stack_set_instance_region = var.aws_region

  operation_preferences {
    failure_tolerance_percentage = 0
    max_concurrent_percentage    = 100
    region_concurrency_type      = "PARALLEL"
  }
}
