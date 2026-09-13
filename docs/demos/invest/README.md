# demo · invest — treasury/BOARD, the INVESTOR's side

**"Tanners Coffee Co earned a 20% gross margin over the past quarter — bid 500k to buy a rule
paying us 10% of their monthly net income until we've drawn 550k"**, then (after the puppet
accepts as Tanners, off-screen, over the real rail) **"did they take it?"**.

What it proves: an openly-operated firm is directly investable on real numbers — the investor's
agent reads a published margin and bids; the counterparty's stamp arrives inbound; the deal
settles itself and the outlay (DR INVESTMENTS / CR CASH) books at acceptance. The coordination
cost between a fund and a firm collapses to one turn. Header shows "Westwood Investments"
(recorder override); the gerp underneath is the tenant, which is why "us" in the prompt is the
caller.

**This is THE capital demo.** The issuer-side take (Tanners recording a raise —
`demo-treasury-*.webm`, kept in backup) follows a different script and is retired.

The permanent design (don't re-litigate): the marketplace tool takes `{buyer, seller}`; the caller
stamps its OWN side, so `buyer==self` is a bid and `seller==self` is an ask — side is computed
from stamp order, never a flag.

**Narration is a roll.** The tenant's gerp_id lives inside the thread string and the persona hides
it reliably only most of the time — re-record until nothing internal reaches the screen. Takes on
disk (restore from backup): `demo-invest-FINAL.webm` (2026-07-28, consolidated rails),
`demo-invest-v1-preconsolidation.webm`.

Fixture: `seed.py` (un-raises the capital: the shared-store row, the instrument, the capital
ledger legs; refuses a Westwood decoy contact). Record: `record.mjs --step 5` (puppets Tanners
between the turns).
