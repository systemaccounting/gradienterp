# prod/dns

Authoritative Route53 DNS for the two operator domains: `gradienterp.cloud` (owner app) and
`openlyoperated.biz` (public directory). Operator account (185369506315).

Apply with the `default` profile (management → self-assume `OrganizationAccountAccessRole`),
same as `gradienterp_cloud`/`tower`:

```
bash scripts/apply.sh --stack dns        # --plan to stop after the plan
```

## Going live (manual, one-time per domain)

Registration stays at **Squarespace**; only DNS moves here. After `apply`:

1. Read `cloud_nameservers` / `biz_nameservers` from the outputs (4 NS each).
2. **Before flipping NS**, recreate every record Squarespace currently serves into this zone —
   especially **MX (email), TXT (SPF/verifications), and any existing CNAMEs** — or they break
   when Route53 becomes authoritative.
3. At Squarespace, set the domain's nameservers to the Route53 NS. Propagation is minutes-to-hours.

A registrar transfer to Route53 Domains is **optional and not Terraform** — it's a console/CLI
`TransferDomain` flow (watch the 60-day-since-registration/migration lock). NS-first means it's a
risk-free admin move whenever you want it; DNS keeps working throughout.

## Records belong to the app stacks

App stacks add their own records (ACM validation, API aliases) by looking the zone up:
`data "aws_route53_zone" "cloud" { name = "gradienterp.cloud." }`. ACM DNS validation only
completes **after** the NS are pointed (the validation CNAME must resolve), so wire custom
domains as a second step once propagation is done.
