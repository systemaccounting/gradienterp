# bill_customer

A daily poll from early in the month. For each gerp, BOTH legs of the same transaction:

    amount    list-invoice-summaries → TotalAmount for that gerp's invoice unit
    evidence  get-invoice-pdf → s3 (modules/storage), never parsed
    cost      DR COST_OF_GOODS_SOLD / CR ACCOUNTS_PAYABLE
    revenue   that amount x 1.2, issued through gradienterp's OWN gerp (`modules/invoicing`)

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

One entry per gerp, carrying `dimensions={"gerp": <gerp_id>}`. That lands in `dims_private`, because
`_PUBLISHABLE_DIMS` is an allowlist and unknown keys go private — so total COGS publishes and the
per-customer split does not, without a decision here. Publishing which customer costs what would
take adding `gerp` to that frozenset.


**Three kinds of account, three treatments.** A customer's gerp books COGS and gets an invoice at
1.2x. The platform accounts (operator, management — the shared serving layer, listed in
`PLATFORM_ACCOUNTS` because they are not gerps and not in `gerp-customers`) book COGS too, since
serving everyone is the indirect half of cost of sale, but there is nobody to bill. Gross margin is
the fees against both.

**The seller's own instance is an expense, not a sale.** Nothing was sold, so gradienterp's own gerp
books `DR UTILITIES_EXPENSE / CR ACCOUNTS_PAYABLE` and raises no invoice — billing it would put
gradienterp on both sides of one document. `COST_OF_GOODS_SOLD` is for a CUSTOMER's instance.

**Idempotent two ways, because it runs daily against one month.** The cost entry carries a
deterministic `entryId` AND timestamp — accounting dedups on `(pk, sk)` with the timestamp baked
into `sk`, so `entryId` alone would let a second run post a second entry. The fee invoice uses a
deterministic id that is READ BEFORE WRITE: `put_invoice` is an unconditional put, so re-creating it
would overwrite an already-issued invoice with a fresh draft and re-post its journal entry.

`get_invoice_pdf` returns a STRUCTURE with a presigned `DocumentUrl` (~15 min), not bytes — the
document is fetched over that URL and stored. The amount never comes from it.

Live. `{"dry_run": true}` reads and reports without writing.
