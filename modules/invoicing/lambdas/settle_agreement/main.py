"""settle_agreement — the sell side's settle EFFECT: an agreed PO row becomes a draft invoice.

Invoked by the shared agreements settle with `{"agreement": row}` when a po-kind row carries both
stamps and this firm is its seller. Domain work only — the dispatcher owns every write to the
agreement row itself. The draft carries customer = the buyer and a SALES_REVENUE line at the
agreed total; the journal (DR AR / CR REVENUE_PENDING) posts later at issue (on ship), and the
revenue itself lands when the customer pays (AGENTS.md § realized revenue), exactly as
the existing invoicing flow.
"""

from aws import log
from _helpers import AUTHOR_AGENT, put_invoice, now_ms


def handler(event, context):
    row = event["agreement"]
    _draft_invoice(row)
    return {"settled": [row["thread"]]}


def _draft_invoice(row):
    thread = row["thread"]
    terms = row.get("terms") or {}
    total = terms.get("total")
    invoice = {
        "invoice_id": thread,   # peer-shared agreement key — never location-prefixed
        "location":   str(row.get("seller_location") or "1"),   # this side's, captured at the seller's stamp
        "customer":   row.get("buyer"),
        "lines": [{
            "description": f"from PO {thread}",
            "account":     "SALES_REVENUE",
            "accountType": "REVENUE",
            "amount":      total,
        }],
        "subtotal":   total,
        "status":     "draft",
        "due_date":   "",
        "memo":       f"drafted from agreed PO thread {thread} (terms {row['terms_hash']})",
        # Authorship of an INBOUND sale. Nobody here signed in — the buyer's agent authored it
        # under their account, on their side of the rail, so their subject is meaningless in our
        # books. The counterparty gerp is the honest author, and the thread carries the rest.
        # Blank would say "unknown", which is a worse claim than "another firm did this".
        "authed_by":  AUTHOR_AGENT,
        "created_by": str(row.get("buyer") or AUTHOR_AGENT),
        "created_at": now_ms(),
    }
    put_invoice(invoice)
    log.info("PO agreed; invoice drafted", thread=thread, total=total)
