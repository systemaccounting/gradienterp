# The web door: POST /automate/{proxy+}
#
# A script has no URL, so the owner's web app cannot run one. This is the one route that serves
# every published script — HTTP APIs take a greedy path variable, so `/automate/collections/charge`
# arrives as `pathParameters.proxy` and needs no route of its own. Nothing is created per script and
# the agent needs no `apigateway:*`.
#
# What makes a url exist is an object: `automations/published/<path>.json`, written with
# `manage_storage op=put`, naming an approved script and the arguments the publisher fixes. So the
# prefix IS the route table — publish is a put, unpublish is a delete, list is a list — which is the
# arrangement `modules/storage`'s portal already uses for `pages/`.
#
# The alternative, `POST /automate` taking `{script, params}`, would hand anyone holding the owner
# JWT any approved script with any arguments. A record narrows the surface to what was published.

resource "aws_apigatewayv2_integration" "automate_web" {
  count                  = var.serve_web ? 1 : 0
  api_id                 = var.server_api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.fn["automate"].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "automate_web" {
  count              = var.serve_web ? 1 : 0
  api_id             = var.server_api_id
  route_key          = "POST /automate/{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.automate_web[0].id}"
  authorization_type = var.owner_authorizer_id != "" ? "JWT" : "AWS_IAM"
  authorizer_id      = var.owner_authorizer_id != "" ? var.owner_authorizer_id : null
}

resource "aws_lambda_permission" "apigw_automate_web" {
  count = var.serve_web ? 1 : 0

  # AddPermission/RemovePermission have no update, so any change replaces this. A generated
  # statement id lets the new grant exist before the old is removed, so there is no window where
  # the principal is unauthorised.
  lifecycle {
    create_before_destroy = true
  }
  statement_id_prefix = "AllowAPIGatewayInvokeAutomate"
  action              = "lambda:InvokeFunction"
  function_name       = module.fn["automate"].name
  principal           = "apigateway.amazonaws.com"
  source_arn          = "${var.server_api_execution_arn}/*/*"
}

# The second door: the same lambda, no authorizer. A vendor's webhook, a carrier's callback, the
# operator's web app — callers that hold nothing of ours. The gateway cannot check a per-route
# secret, so the route RECORD names one (`caller.bearer`, a name at the firm's automation env path)
# and `automate` checks it; a record without `caller` is not reachable here at all. Published with
# `manage_hooks` publish, which mints the secret and writes the record in one act.
resource "aws_apigatewayv2_route" "automate_hooks" {
  count              = var.serve_web ? 1 : 0
  api_id             = var.server_api_id
  route_key          = "POST /hooks/{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.automate_web[0].id}"
  authorization_type = "NONE"
}
