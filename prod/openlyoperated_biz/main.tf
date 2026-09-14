# openlyoperated.biz — public dashboard. S3 (private) → CloudFront (OAC) → openlyoperated.biz.
# Static HTML/CSS/JS only (the dashboard fetches its data from api.openlyoperated.biz client-side),
# so it's a pure CDN edge: no lambda, no BFF. The zone lives in prod/dns; we add records to it.
#
#   web/index.html      — deployable empty scaffold (the live main page)
#   web/index.mock.html — seeded demo, reachable from the "mock site" button
#
# Updating the site: `terraform apply` re-uploads changed objects; objects carry max-age=60 so the
# edge picks them up within a minute (no invalidation needed for routine edits).

locals {
  domain      = "openlyoperated.biz"
  www         = "www.openlyoperated.biz"
  bucket_name = "openlyoperated-biz-site-185369506315"
  # the fixed CloudFront alias-target hosted zone id (global)
  cloudfront_zone_id = "Z2FDTNDATAQYW2"

  web_dir = "${path.module}/web"
  content_types = {
    html = "text/html; charset=utf-8"
    css  = "text/css; charset=utf-8"
    js   = "text/javascript; charset=utf-8"
    svg  = "image/svg+xml"
    json = "application/json"
    txt  = "text/plain; charset=utf-8"
  }
}

data "aws_route53_zone" "biz" {
  name = "${local.domain}."
}

# ---------- origin bucket (private; only CloudFront reads it) ----------
resource "aws_s3_bucket" "site" {
  bucket = local.bucket_name
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# everything under web/ (html + app.js + app.css + vendor/lit-html.js), content-type by extension.
# new files upload automatically; max-age=60 so edits show within ~1 min without an invalidation.
resource "aws_s3_object" "web" {
  for_each      = fileset(local.web_dir, "**")
  bucket        = aws_s3_bucket.site.id
  key           = each.value
  source        = "${local.web_dir}/${each.value}"
  etag          = filemd5("${local.web_dir}/${each.value}")
  content_type  = lookup(local.content_types, lower(reverse(split(".", each.value))[0]), "application/octet-stream")
  cache_control = "public, max-age=60"
}

# ---------- TLS cert (DNS-validated against the prod/dns zone; resolves now that NS point here) ----------
resource "aws_acm_certificate" "site" {
  domain_name               = local.domain
  subject_alternative_names = [local.www]
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for o in aws_acm_certificate.site.domain_validation_options : o.domain_name => {
      name   = o.resource_record_name
      type   = o.resource_record_type
      record = o.resource_record_value
    }
  }
  zone_id         = data.aws_route53_zone.biz.zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "site" {
  certificate_arn         = aws_acm_certificate.site.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# ---------- CloudFront ----------
resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "openlyoperated-biz-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

# Every response carries these. The CSP allows no inline script: the pages' scripts are files
# (main.js, mock.js, app.js, vendor/lit-html.js). The api and the events stream are the page's only
# connections, Google Fonts its only outside styles and fonts.
resource "aws_cloudfront_response_headers_policy" "site" {
  name = "openlyoperated-biz-security"

  security_headers_config {
    content_security_policy {
      content_security_policy = join("; ", [
        "default-src 'self'",
        "script-src 'self'",
        "connect-src 'self' https://api.openlyoperated.biz wss://events.openlyoperated.biz",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
      ])
      override = true
    }
    content_type_options {
      override = true
    }
    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
    }
  }

  custom_headers_config {
    items {
      header   = "Cross-Origin-Opener-Policy"
      value    = "same-origin"
      override = true
    }
  }
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  comment             = "openlyoperated.biz public dashboard"
  aliases             = [local.domain, local.www]
  price_class         = "PriceClass_100"

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "s3-site"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id           = "s3-site"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.site.id
  }

  # SPA-friendly: a missing object (S3 returns 403 under OAC) falls back to the app shell.
  # index.mock.html is a real object, so it's unaffected.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }
  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.site.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# bucket policy: only this distribution (via OAC) may read the objects
data "aws_iam_policy_document" "site" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.site.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.site.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = data.aws_iam_policy_document.site.json
}

# ---------- DNS: apex + www → CloudFront (A + AAAA) ----------
resource "aws_route53_record" "apex_a" {
  zone_id = data.aws_route53_zone.biz.zone_id
  name    = local.domain
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = local.cloudfront_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "apex_aaaa" {
  zone_id = data.aws_route53_zone.biz.zone_id
  name    = local.domain
  type    = "AAAA"
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = local.cloudfront_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "www_a" {
  zone_id = data.aws_route53_zone.biz.zone_id
  name    = local.www
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = local.cloudfront_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "www_aaaa" {
  zone_id = data.aws_route53_zone.biz.zone_id
  name    = local.www
  type    = "AAAA"
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = local.cloudfront_zone_id
    evaluate_target_health = false
  }
}

output "site_url" { value = "https://${local.domain}" }
output "cloudfront_domain" { value = aws_cloudfront_distribution.site.domain_name }
output "distribution_id" { value = aws_cloudfront_distribution.site.id }
output "bucket" { value = aws_s3_bucket.site.id }
