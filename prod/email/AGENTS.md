# prod/email

Email for `gradienterp.cloud`, off Squarespace and in TF — inbound forwarding, plus the SMTP
credential gerps send OUT through.

**Inbound.** SES receives, stores the raw message in S3, and invokes `gerp-mail-forwarder`
(Lambda), which rewrites the headers so the re-sent copy aligns with the domain's DKIM/DMARC and
`SendRawEmail`s it to a personal inbox. Operator account (185369506315); records land in the
`prod/dns` zone.

Apply with the `default` profile (management → self-assume `OrganizationAccountAccessRole`):

```
cd prod/email && AWS_PROFILE=default terraform apply
```

## Forward destination (kept out of the repo)

The destination is an SSM `String` at `/gradienterp/email/forward_to`. TF creates the parameter
with a placeholder and `ignore_changes = [value]`; set the real value out-of-band so the personal
address never lands in the repo:

```
AWS_PROFILE=operator-org aws ssm put-parameter --name /gradienterp/email/forward_to \
  --value <inbox> --type String --overwrite --region us-east-1 --no-cli-pager
```

The Lambda reads it once per warm container.

## Going live (gated on DNS)

The DNS records (SES verify TXT, 3 DKIM CNAMEs, MX, SPF, DMARC) are inert until
`gradienterp.cloud`'s nameservers point at Route53 — only then does SES domain verification
complete and the MX route mail to SES. So: point NS (see `prod/dns`), wait for verification to
flip to **Success** (`aws ses get-identity-verification-attributes`), then mail flows.

**Sandbox:** the account is in the SES sandbox, so the forwarder can only send to a *verified*
address. Verify the destination once (a click on the confirmation email), or request production
access:

```
AWS_PROFILE=operator-org aws ses verify-email-identity --email-address <inbox> --region us-east-1 --no-cli-pager
```

## SMTP sending credential (`gradienterp-ses-smtp`)

A gerp sends through its firm's own mail server (`modules/agent/infra/mail.tf`), and for
gradienterp that server is SES itself — `email-smtp.us-east-1.amazonaws.com:587` is an ordinary
SMTP host, so the one firm whose mail already lives here needs no special case.

SMTP authenticates with a credential rather than a role, which is what lets a gerp in its own
sub-account send as this account's verified `gradienterp.cloud` identity. IAM could not express
that without cross-account trust; a username and password need only the network.

**The IAM user is terraform's; its access key is not.** A key created in terraform sits in state,
and the SMTP password is derived from it — the same reason the plaid credentials are made out of
band. `aws_iam_user.smtp` + its `ses:SendRawEmail` policy live here; the key does not.

### rotating it

The two keys coexist, so there is no gap — create, switch, then delete.

    aws iam create-access-key --user-name gradienterp-ses-smtp --profile operator-org
    python3 scripts/ses_smtp_password.py <SecretAccessKey> us-east-1

    # the password (SecureString), read by send_email at send time
    aws ssm put-parameter --name /gradienterp/customers/gradienterp/secrets/ses_smtp \
      --value "<derived>" --type SecureString --overwrite \
      --profile customer-gradienterp-via-org --region us-east-1

    # the username is the ACCESS KEY ID and is not secret — it lives on the SENDER# row
    aws lambda invoke --function-name gerp-mail-gradienterp-configure_smtp \
      --cli-binary-format raw-in-base64-out --payload '{"address":"billing@gradienterp.cloud",
      "host":"email-smtp.us-east-1.amazonaws.com","port":587,"username":"<AccessKeyId>",
      "secret_name":"ses_smtp","test_to":"<your address>"}' \
      --profile customer-gradienterp-via-org --region us-east-1 /dev/stdout

    aws iam delete-access-key --user-name gradienterp-ses-smtp --access-key-id <old> \
      --profile operator-org

`configure_smtp` sends a test message before it writes the row, so a mistyped key fails there
rather than at 3am. Delete the old access key only after that comes back `configured`.

Two things the derivation depends on: the password is **per-Region** (a us-east-1 credential does
not authenticate against another region's endpoint), and it is **not** the AWS secret key —
`scripts/ses_smtp_password.py` runs the HMAC chain AWS documents for it.

## Notes

- Catch-all: the receipt rule matches the whole domain, so *any* `*@gradienterp.cloud` forwards.
  Scope `recipients` to specific addresses if catch-all spam becomes a problem.
- This makes SES the domain's sending identity (SPF/DKIM). Anything that still sends via Mailgun
  from `@gradienterp.cloud` would fail the `p=reject`-class DMARC — move it to SES.
- Raw messages in S3 self-expire after 7 days (already forwarded).
