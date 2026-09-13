# purchasing — design notes

## the order is a shared object, not a private record

a PO between two firms is one shared object seen from both ends: a **purchase order** here, a **sales order** (a drafted invoice in `modules/invoicing`) on the vendor's side. the two are projections of one event, causally linked so they can't drift — order-to-cash and procure-to-pay are the two ends of one exchange, not two cycles to reconcile.

## one protocol, local or cross-account

```
inventory.low_stock → purchasing → [vendor] invoicing → inventory → accounting
```

each hop is an addressed event to a handler; the next handler is your own inventory or a stranger's invoicing — the same message, the address just says local or cross-account. the firm boundary isn't special, and there's no separate EDI layer because there was never a separate module-integration layer. the quote phase rides the same shared `modules/agreements` substrate treasury uses (append-only price points, agreed when both sides stamp — one `request`, one `approve`) rather than reinventing it.

## the spend guardrail

agent approval of a `propose_po` is gated by an AgentCore Cedar policy — an owner spend threshold ("don't approve POs over $500 without confirmation"). a business control, not a safety feature: the agent can't overspend. (wiring tracked in `TODO.md`.)
