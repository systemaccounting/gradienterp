############################################
# Managed web search — the AgentCore Gateway `web-search` connector.
#
# The gateway role's `bedrock-agentcore:InvokeWebSearch` grant on the service-owned tool ARN lives
# in main.tf, next to the rest of the execution role's policy. This file is the TARGET.
#
# The `connector` block needs AWS provider >= 6.63.0 (hashicorp/terraform-provider-aws#48706).
# gradienterp's target was created by hand before the provider shipped it and imported into state
# (`terraform import 'module.agent.aws_bedrockagentcore_gateway_target.web_search' <gateway_id>,<target_id>`, comma-joined);
# every other gerp gets it from the apply.
#
# `parameter_values` is REQUIRED on every configuration, even empty. The API rejects its absence with
# "Connector configurations must not be empty" — which reads as a complaint about the LIST and is
# not — while the SDK marks only `name` required and the provider's own acceptance fixture omits it.
# So the fixture is expected to fail against the live API, and this differs from it deliberately.
#
# The connector advertises no description of its own (tools/list returns null), so
# `configuration.description` is the agent's only guidance about when to reach for the tool. The
# connector names its tool `WebSearch` under the target `web-search`, so the address is
# `web-search___WebSearch` — a name `_gateway.address` passes through rather than composes.
############################################

resource "aws_bedrockagentcore_gateway_target" "web_search" {
  gateway_identifier = aws_bedrockagentcore_gateway.this.gateway_id
  name               = "web-search"
  description        = "Managed web search — current external facts (requirements, prices, announcements)"

  target_configuration {
    mcp {
      connector {
        source {
          connector_id = "web-search"
        }

        configuration {
          name = "WebSearch"

          # The connector advertises NO description of its own — tools/list returns null — so the
          # agent would get a tool with no guidance about when to reach for it. This is what the
          # in-process tool carried in its docstring.
          description = join(" ", [
            "Search the live web and return titles, URLs, published dates and page text.",
            "Use it when an answer depends on up-to-date or external facts your training may not",
            "cover — current government requirements (what an I-9 needs, which state wants a",
            "DE-4), recent announcements, prices, anything that may have changed since training.",
            "Pass a focused natural-language query and cite the URLs you rely on.",
          ])

          # required by the API even when empty. Domain filtering is the caller's, per call.
          parameter_values = jsonencode({})
        }
      }
    }
  }

  credential_provider_configuration {
    gateway_iam_role {}
  }
}
