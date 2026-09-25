# agreements — open work

- [ ] **location per side** — a `location` on an agreement row is the row creator's (the
      buyer's on a `create_po`, the seller's on a `return_quote`), and each side's settle reads
      that one value: the buyer-side settle opens the PO at it, the seller-side settle pins the
      invoice to "1". Capture each firm's location when its slot stamps (request for the
      proposer, accept for the acceptor) so the buyer's PO and the seller's invoice each open at
      their own branch. Until then a quote thread's PO and every cross-firm invoice land at main
