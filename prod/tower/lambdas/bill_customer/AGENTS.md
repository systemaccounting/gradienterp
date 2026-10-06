# bill_customer

A daily poll from early in the month. For each gerp, BOTH legs of the same transaction:

    amount    list-invoice-summaries → TotalAmount of each of that gerp's invoices for the period
    evidence  get-invoice-pdf → s3 (modules/storage), one key per invoice, never parsed
    cost      DR COST_OF_GOODS_SOLD / CR ACCOUNTS_PAYABLE, one entry per invoice
    revenue   one fee invoice per gerp and month, a line per AWS invoice at 1.2x, issued through
              gradienterp's OWN gerp (`modules/invoicing`)

**AWS issues one invoice per billing entity with cost.** The services come under `AWS`, the model
(Claude on Bedrock, sold by Anthropic) under `AWS_MARKETPLACE`: a gerp with an agent has two a month,
an account that never used the model has one, an account at ~0 spend has none. Both land on the 1st
in the months seen; the API lists them in no fixed order. Which to expect comes from one Cost Explorer
read on the management session the run holds, `ce:GetCostAndUsage` for the period grouped by
`LINKED_ACCOUNT` and `BILLING_ENTITY` ($0.01 a request): the fee is issued only when a summary is
present for every entity with cost, and until then the gerp is in `waiting` with `waiting_on`
saying which. A fee already issued whose total is under what the period's invoices make is
returned as `underbilled` and logged at WARNING, never rebilled; a legal-entity split (an EU billing
address adds AWS EMEA SARL) is not modelled and would show there. The pdf and summary json are kept
at `vendors/aws/<period>/<gerp>/<invoice id>`. `Entity.BillingEntity` is taken off the raw response
body on botocore's `before-parse` event: the Lambda runtime's botocore predates the field and its
parser drops a member its model lacks.

**A fee under the processor minimum is carried.** Stripe refuses a charge under $0.50
(`MIN_CHARGE`, `amount_too_small`), so a fee under it is not issued: its lines go on the gerp row
as `carried`, each naming its period and AWS invoice, and the next period's invoice opens with
them before its own lines; the issue writes `billing` and removes `carried` in one update. Still
under, it accumulates. The run returns `carried` and logs it; `dry_run` reports what it would
carry. The standing case is a gerp vended late in the month. The daily `underbilled` check is
against the current period's lines, so an invoice carrying an earlier period never reads short.

**The number comes from the API, not the document.** `list-invoice-summaries` returns `TotalAmount`,
`TotalAmountBeforeTax` and an `AmountBreakdown` with subtotals and discounts, so the markup
multiplies a field. The pdf is what makes the COGS entry checkable by someone outside, which is the
point for a firm publishing its cost structure.

**A poll, because nothing announces the invoice.** AWS closes the month and issues invoices in the
first days of the next one, and there is no event to wait on: Billing sends CloudTrail API-call
events to EventBridge (`source: aws.billing`) and the Invoicing feature's are invoice-unit CRUD only,
so nothing fires when an invoice is produced. AWS does email it, but a billing run wired to mail
delivery fails silently when mail does. `list-invoice-units` separates "not issued yet" from "this
gerp has no unit", which otherwise look the same.

`COST_OF_GOODS_SOLD` is the literal classification: gradienterp buys AWS capacity and resells it, so
the bill for a gerp IS the cost of the goods sold to that customer. A dedicated hosting account is
available (`add_classification` extends gradienterp's own registry, not the canonical seed) and is
the wrong shape — it would split resale cost away from the revenue it produced, and the gross margin
per gerp is the number the platform exists to show.

Issuing the fee through the invoicing module rather than composing a bespoke bill is what makes it
an ordinary receivable, the same object a gerp's own sales produce. Collection is not tower's
concern — it is the payer's rule instances (`modules/payments` § money in).

One cost entry per AWS invoice, carrying `dimensions={"gerp": <gerp_id>}`. That lands in `dims_private`, because
`_PUBLISHABLE_DIMS` is an allowlist and unknown keys go private — so total COGS publishes and the
per-customer split does not, without a decision here. Publishing which customer costs what would
take adding `gerp` to that frozenset.


**Three kinds of account, three treatments.** A customer's gerp books COGS and gets an invoice at
1.2x; an expensed one books UTILITIES_EXPENSE and gets none. The platform accounts (operator, management — the shared serving layer, listed in
`PLATFORM_ACCOUNTS` because they are not gerps and not in `gerp-customers`) book COGS too, since
serving everyone is the indirect half of cost of sale, but there is nobody to bill. Gross margin is
the fees against both.

**An expensed gerp is an expense, not a sale.** `expensed` on the `gerp-customers` row says the
gerp is the operator's own: gradienterp's books, the staging pairs. Set by hand, never by the vend.
Nothing was sold, so the cost books `DR UTILITIES_EXPENSE / CR ACCOUNTS_PAYABLE` and no invoice is
raised — billing gradienterp's own would put it on both sides of one document. `COST_OF_GOODS_SOLD`
is for a CUSTOMER's instance. `SELLER_GERP` names only the gerp the fee is invoiced from.

**Idempotent two ways, because it runs daily against one month.** The cost entry carries a
deterministic `entryId` AND timestamp — accounting dedups on `(pk, sk)` with the timestamp baked
into `sk`, so `entryId` alone would let a second run post a second entry. The fee invoice uses a
deterministic id that is READ BEFORE WRITE: `put_invoice` is an unconditional put, so re-creating it
would overwrite an already-issued invoice with a fresh draft and re-post its journal entry.

`get_invoice_pdf` returns a STRUCTURE with a presigned `DocumentUrl` (~15 min), not bytes — the
document is fetched over that URL and stored. The amount never comes from it.

Live. `{"dry_run": true}` reads and reports without writing.
