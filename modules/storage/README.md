# storage module

The agent's filing cabinet. The curated subset of the owner's documents they hand the agent to file, find, and
pull back — receipts, estimate photos, contracts, a vendor W-9 — while the bulk stays in google drive / onedrive.
Filing is back-office toil; the agent absorbs it: "file this" and it names + places the doc, "pull that up" and it
retrieves. Full rationale + the requirement spec: [`storage.md`](../../storage.md).

The deeper role: the published books are only as credible as their **source documents**. A booked expense that
points at its receipt, a PO at its signed quote, a payout at its bank statement — storage is the evidence layer
under the transparent books.

Encrypted per-tenant, **bytes never transit the agent** (presigned PUT / GET). Deliberately not a DMS competing
with drive — the agent's working shelf, indexed by the path the agent itself files under, captioned with native
S3 object annotations rather than a side database.
