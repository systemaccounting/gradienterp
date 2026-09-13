# gradienterp.cloud → the BFF. ACM cert (DNS-validated) + an HTTP API custom domain mapped
# to the $default stage + an apex alias. The zone lives in prod/dns; we add records to it.
# Validation/alias resolve now that the domain's NS point at Route53.

data "aws_route53_zone" "cloud" {
  name = "gradienterp.cloud."
}

resource "aws_acm_certificate" "cloud" {
  domain_name               = "gradienterp.cloud"
  subject_alternative_names = ["www.gradienterp.cloud"]
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for o in aws_acm_certificate.cloud.domain_validation_options : o.domain_name => {
      name   = o.resource_record_name
      type   = o.resource_record_type
      record = o.resource_record_value
    }
  }
  zone_id         = data.aws_route53_zone.cloud.zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "cloud" {
  certificate_arn         = aws_acm_certificate.cloud.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_apigatewayv2_domain_name" "cloud" {
  domain_name = "gradienterp.cloud"
  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.cloud.certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_apigatewayv2_api_mapping" "cloud" {
  api_id      = aws_apigatewayv2_api.this.id
  domain_name = aws_apigatewayv2_domain_name.cloud.id
  stage       = aws_apigatewayv2_stage.default.id
}

resource "aws_route53_record" "apex" {
  zone_id = data.aws_route53_zone.cloud.zone_id
  name    = "gradienterp.cloud"
  type    = "A"
  alias {
    name                   = aws_apigatewayv2_domain_name.cloud.domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.cloud.domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}

# www → same app (no redirect; the SPA is host-agnostic). Shares the cert's www SAN.
resource "aws_apigatewayv2_domain_name" "www" {
  domain_name = "www.gradienterp.cloud"
  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.cloud.certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_apigatewayv2_api_mapping" "www" {
  api_id      = aws_apigatewayv2_api.this.id
  domain_name = aws_apigatewayv2_domain_name.www.id
  stage       = aws_apigatewayv2_stage.default.id
}

resource "aws_route53_record" "www" {
  zone_id = data.aws_route53_zone.cloud.zone_id
  name    = "www.gradienterp.cloud"
  type    = "A"
  alias {
    name                   = aws_apigatewayv2_domain_name.www.domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.www.domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}

output "app_url" {
  value = "https://gradienterp.cloud"
}
