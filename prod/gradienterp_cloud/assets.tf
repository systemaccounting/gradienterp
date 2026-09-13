# assets.gradienterp.cloud — the login-page demo gifs. S3 (private) → CloudFront (OAC), the
# openlyoperated.biz shape. These are ~23MB of media; inside the BFF bundle they'd ride every code
# push and every cold start, so they get a CDN edge instead and the app references absolute URLs.
#
# CONTENT is not managed here (objects carry max-age=30d; deploy.sh invalidates what it changes) — `bash scripts/deploy.sh assets` uploads changed gifs from assets/
# and invalidates them on the distro (the lambda-code split, applied to media: content = deploy.sh,
# shape = this file).

locals {
  assets_domain = "assets.gradienterp.cloud"
  assets_bucket = "gradienterp-cloud-assets-185369506315"
  assets_dir    = "${path.module}/assets"
  # the fixed CloudFront alias-target hosted zone id (global)
  cloudfront_zone_id = "Z2FDTNDATAQYW2"
}

# ---------- origin bucket (private; only CloudFront reads it) ----------
resource "aws_s3_bucket" "assets" {
  bucket = local.assets_bucket
}

resource "aws_s3_bucket_public_access_block" "assets" {
  bucket                  = aws_s3_bucket.assets.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------- TLS cert (DNS-validated against the prod/dns zone) ----------
resource "aws_acm_certificate" "assets" {
  domain_name       = local.assets_domain
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "assets_cert_validation" {
  for_each = {
    for o in aws_acm_certificate.assets.domain_validation_options : o.domain_name => {
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

resource "aws_acm_certificate_validation" "assets" {
  certificate_arn         = aws_acm_certificate.assets.arn
  validation_record_fqdns = [for r in aws_route53_record.assets_cert_validation : r.fqdn]
}

# ---------- CloudFront ----------
resource "aws_cloudfront_origin_access_control" "assets" {
  name                              = "gradienterp-cloud-assets-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "assets_optimized" {
  name = "Managed-CachingOptimized"
}

resource "aws_cloudfront_distribution" "assets" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "gradienterp.cloud demo assets"
  aliases         = [local.assets_domain]
  price_class     = "PriceClass_100"

  origin {
    domain_name              = aws_s3_bucket.assets.bucket_regional_domain_name
    origin_id                = "s3-assets"
    origin_access_control_id = aws_cloudfront_origin_access_control.assets.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-assets"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = false # gifs are already compressed; gzip just burns edge CPU
    cache_policy_id        = data.aws_cloudfront_cache_policy.assets_optimized.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.assets.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# bucket policy: only this distribution (via OAC) may read the objects — and never the BACKUP/
# prefix (the demo-media backup rides this bucket; the deny keeps it off the CDN, so a guessed
# /BACKUP/* path 403s while operator-cred sync reads/writes it untouched)
data "aws_iam_policy_document" "assets" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.assets.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.assets.arn]
    }
  }

  statement {
    effect    = "Deny"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.assets.arn}/BACKUP/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
  }
}

resource "aws_s3_bucket_policy" "assets" {
  bucket = aws_s3_bucket.assets.id
  policy = data.aws_iam_policy_document.assets.json
}

# ---------- DNS: assets → CloudFront (A + AAAA) ----------
resource "aws_route53_record" "assets_a" {
  zone_id = data.aws_route53_zone.cloud.zone_id
  name    = local.assets_domain
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.assets.domain_name
    zone_id                = local.cloudfront_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "assets_aaaa" {
  zone_id = data.aws_route53_zone.cloud.zone_id
  name    = local.assets_domain
  type    = "AAAA"
  alias {
    name                   = aws_cloudfront_distribution.assets.domain_name
    zone_id                = local.cloudfront_zone_id
    evaluate_target_health = false
  }
}
