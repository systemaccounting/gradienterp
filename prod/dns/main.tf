# Authoritative DNS for the two operator domains. Registration stays at Squarespace;
# point each domain's nameservers (outputs below) at Route53 to make these zones live.
# App stacks add their own records by looking the zone up via data "aws_route53_zone".

resource "aws_route53_zone" "cloud" {
  name    = "gradienterp.cloud"
  comment = "gradientERP owner app (gerp-website)"
}

resource "aws_route53_zone" "biz" {
  name    = "openlyoperated.biz"
  comment = "Openly Operated public directory"
}

output "cloud_nameservers" {
  description = "Set these as gradienterp.cloud's nameservers at Squarespace."
  value       = aws_route53_zone.cloud.name_servers
}

output "biz_nameservers" {
  description = "Set these as openlyoperated.biz's nameservers at Squarespace."
  value       = aws_route53_zone.biz.name_servers
}

output "cloud_zone_id" { value = aws_route53_zone.cloud.zone_id }
output "biz_zone_id" { value = aws_route53_zone.biz.zone_id }

