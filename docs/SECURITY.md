# Security

How gradientERP (gradienterp.cloud) protects the data it holds and processes. The code that runs the
platform is this repository, so every measure below can be checked against it. A reviewer who finds
this document short of what they need can open a pull request against it.

## Reporting a vulnerability

Use the private support form at https://gradienterp.cloud/support, not a public issue. A fix ships
before any detail is published.

## Isolation

- Each business's instance runs in its own AWS account, in the region for its country. One business's
  records, credentials and logs are never in another's account.
- Access into an account is through named IAM roles. Each function has its own role, limited to the
  actions and resources that function uses, and defined in terraform in this repository.
- The platform's shared services run in a separate operator account and reach a business's account
  only through IAM roles.

## Authentication

- Account holders sign in through Amazon Cognito. Every `/api` route is behind API Gateway's JWT
  authorizer, and the backend checks that the signed-in account holds the instance a request names.
- Deploys run in GitHub Actions and reach AWS through OpenID Connect into a role limited to this
  repository's `prod` environment, which only the `main` branch can use. No AWS keys are stored in
  GitHub.

## Secrets

- Provider credentials (payments, bank feeds, mail) are set out of band in AWS Systems Manager
  Parameter Store or Secrets Manager, as encrypted values. They are never in terraform state or the
  repository.
- A business's own provider credentials, such as its bank feed's access token, live only in that
  business's account.
- The repository has GitHub secret scanning with push protection, which blocks a commit carrying a
  known credential format.

## Encryption

- In transit: TLS 1.2 or later on every public endpoint (API Gateway custom domains and CloudFront
  distributions), and TLS on every call to AWS and to providers.
- At rest: DynamoDB tables and S3 buckets are encrypted, and encrypted parameters and document
  storage use AWS KMS.

## Payments and bank data

- A card for gradientERP's own billing is entered on Stripe's hosted page or in Stripe's card
  fields and saved at Stripe. The card number never reaches gradientERP's servers.
- Bank credentials never reach gradientERP: a bank is linked through Plaid's hosted Link, and only
  Plaid's access token for that link is kept, in the business's own account.
- Every provider webhook's signature is verified before it is acted on.

## Logging

- AWS CloudTrail records API activity in every account in the organization, through the AWS Control
  Tower organization trail into a separate log archive account.
- Functions log to CloudWatch in the account they run in. Request logs are kept for 90 days, and a
  test on every change fails when a log group would keep them longer.

## Vulnerability management

- The platform runs no servers of its own: AWS Lambda, Amazon Bedrock AgentCore and other managed
  services, whose hosts AWS patches.
- GitHub Dependabot opens a pull request when a dependency has a published vulnerability, and
  weekly when the agent image's base image has a newer version.
- GitHub CodeQL scans the repository's code on every change.
- Amazon Inspector scans the agent image when it is pushed and again as new vulnerabilities are
  published, and a high or critical finding alerts the operator.
- A vulnerability in a component in use, found by any of these or published in an AWS security
  bulletin, is patched within 7 days when critical and within 30 days when high.
- Runtimes and the container base image move to a supported version before AWS's deprecation date.
  AWS's deprecation notices go to the operator.

## Incidents

A personal data breach is reported to the affected business within 48 hours of becoming aware of it,
as the [Data Processing Agreement](DPA.md) sets out.
