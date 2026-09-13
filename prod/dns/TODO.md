# prod/dns — open work

- **openlyoperated.biz records** — the zone is live and authoritative but empty (only NS/SOA).
  Add records once the public-directory backend exists: the app alias (ACM cert + APIGW custom
  domain, same shape as `gradienterp.cloud` in `prod/gradienterp_cloud/customdomain.tf`), and email
  if the .biz domain needs it (mirror `prod/email`). Until then, nothing resolves under the domain.
