"""demo 04 fixture — a clean slate for the capital raise.

    .venv/bin/python docs/demos/invest/seed.py            # reset, seed, check
    .venv/bin/python docs/demos/invest/seed.py --reset     # teardown only
    .venv/bin/python docs/demos/invest/seed.py --check     # is this demo recordable right now?

The verbs, the safety properties and the no-side-doors rule are `docs/demos/fixture.py`.

THE PROMPT is the INVESTOR's (`record.mjs --step 5`, header shown as Westwood Investments):

  "Tanners Coffee Co earned a 20% gross margin over the past quarter — bid 500k to buy a rule
   paying us 10% of their monthly net income until we've drawn 550k"

This is the buy side, and it is the half worth filming. Anyone can watch a firm record a raise it
already agreed to; the claim here is that a business's published margin makes it **directly
investable** — an investor reads real numbers and opens a bid against them, with no bank, no
prospectus and no share register in between.

WHAT THE AGENT HAS TO DO, none of it named in the prompt:

  propose_offer   Westwood stamps as BUYER → the row opens as a bid (`opened_as: bid`,
                  computed from stamp order), thread `<target>#<investor>#<product>`
  → offer.proposed crosses to Tanners, who accepts on their own gerp

It stops there, and that is correct: the counterparty's stamp is theirs to give. A bid that settled
itself would be the demo lying. The mirror half — Tanners accepting, the funds landing, the
instrument creation — is `--step 4`, and `settlement` (a DDB stream ESM) is what creates
`DISTRIBUTION#<thread>` once BOTH stamps and the ledger proof exist. Treasury's extra gate over a
plain PO settle: no instrument before the money is in the books.

WHY THIS FIXTURE IS MOSTLY A RESET. The demo needs almost nothing seeded — a counterparty contact
and empty hands. What it needs is for the raise NOT to have happened yet, and the tenant carries the
residue of every previous take: a settled agreement, a live `DISTRIBUTION#` rule instance, and 500k already
sitting in CASH and OWNER_EQUITY. An agent asked to set up a deal that is already set up will
correctly tell you so, and the take is dead.

THE LEDGER IS THE HARD PART. `manage_capital` (op: record_receipt) posts under a deterministic
`entryId = capital-<thread>`, so a re-run is idempotent and never double-books — but "idempotent" is
not "absent". The 500k stays on the balance sheet from the last take, so the next one opens with the
funds already received. The reset therefore deletes the capital entry's own ledger rows, scoped to
this thread. That is a fixture deleting what a fixture wrote; the rest of the books are untouched,
and nothing here goes near the entries demo 01 depends on.

That delete is safe because a ledger ROW IS A WHOLE ENTRY — both legs live inside one item, so
removing it takes the debit and the credit together and the trial balance still balances (verified:
CASH −500,000 and OWNER_EQUITY −500,000, totals 148,276.80 on both sides). Were the ledger one row
per leg, a scoped delete could strand a debit without its credit and quietly unbalance the books.
Check that assumption before copying this teardown to another store.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # docs/demos — the shared harness
import fixture as fx                            # noqa: E402 — the shared harness (§ no side doors)

FIRM = "gradienterp"
INVESTOR = "westwood_investments"               # who the take is filmed AS (recorder header only)
INVESTOR_NAME = "Westwood Investments"
# The seller is a gerp_id the bid is ADDRESSED to, and there is exactly one real gerp on the
# platform — so the counterparty is played by `tests/puppet`, which needs no provisioning and no
# registration. The recorder reads the tenant's own agreements table for the row the bid creates and
# emits `offer.accepted` inbound as this id (`record.mjs` § playTanners), so the acceptance arrives
# over the tenant's REAL rails.
#
# The id is what the AGENT composes from the name in the prompt, not something this fixture picks.
# Seeding it under `puppet-tanners` did not make the agent use it — it addressed
# `tanners_coffee_co`, derived straight from "Tanners Coffee Co", and the bid went somewhere the
# puppet was not listening. The contact matches what the agent will actually write.
TARGET = "tanners_coffee_co"                    # whose margin is being bid against
TARGET_NAME = "Tanners Coffee Co"

# Everything this demo owns is keyed by the counterparty. The threads are composed by the tools
# (`<firm>#<investor>#<product>`), so the id is the only half this fixture controls — the same rule
# as demo 03's item prefix, and the reason the teardown survives a change in product naming.
#
# Matched with separators NORMALIZED. The same counterparty exists in this tenant as both
# `westwood_investments` and `westwood-investments`, and an underscore-only scope left the
# hyphenated thread standing — the agent found it, decided a bid was already open, and spent the
# take asking for a countersignature instead of raising the capital. An id whose spelling varies is
# not a scope; the normalized name is.
# Both names, because the thread is composed `<seller>#<buyer>#<product>` and which name appears
# depends on which side is filmed. The firm-side take produces `gradienterp#westwood_investments#…`
# and the investor-side one produces `puppet-tanners#gradienterp#…` — a scope matching only the
# investor left the other standing, the agent found what looked like a live bid, and spent the take
# asking for a countersignature instead of opening one.
def OWNS(text) -> bool:
    t = str(text).replace("_", "-").lower()
    return "westwood" in t or "tanners" in t


def IS_DECOY(contact_id) -> bool:
    """Only the INVESTOR's contact is a decoy — the target's is seeded on purpose.

    Deliberately narrower than `OWNS`. Reusing the thread scope here flagged the Tanners contact this
    fixture had just written, because a scope built for `<seller>#<buyer>#<product>` threads matches
    both parties by design and a contact check must match exactly one."""
    return "westwood" in str(contact_id).replace("_", "-").lower()

LEDGER_TABLE = "gerp-accounting-gradienterp-ledger"
# The SHARED negotiation store (modules/agreements) — capital rows live here since the 2026-07-28
# consolidation; treasury's own table is pre-consolidation history and nothing reads it.
AGREEMENTS_TABLE = "gerp-agreements-gradienterp"
RULES_TABLE = "gerp-rules-gradienterp-instances"
CONTACTS_TABLE = "gerp-contacts-gradienterp"
CUSTOMERS_TABLE = "gerp-customers"              # the OPERATOR's registry — see `_register_peer`


# ── seed ──────────────────────────────────────────────────────────────────────────────────────────

def seed():
    """The TARGET firm on file, and nothing else.

    Deliberately NOT a contact for Westwood. The take is filmed AS Westwood — the recorder swaps the
    header, but the gerp underneath is this one and there is no second gerp on the platform. So "us"
    in the prompt is the CALLER, and `propose_offer` refuses a request where `GERP_ID` is neither
    party. A contact named Westwood is a decoy that reads exactly like the right answer: the first
    attempt at this take had one, the agent set `buyer` to the contact id, and the tool correctly
    refused with "you (gradienterp) must be the buyer or the seller". `reset` removes it.

    No books are staged. The bid is opened against Tanners' PUBLISHED margin, which is the point —
    an investor reads the public feed, not this tenant's ledger — and the instrument doesn't pay
    until a period closes and the balances computation calls `distribution`, which is a different demo."""
    fx.s.call("contacts", "manage_contacts", {
        "op": "put",
        "contact_id": TARGET, "entity_type": "organization", "name": TARGET_NAME,
        "is_customer": False, "is_vendor": False,
    })
    _register_peer()
    print(f"  wrote      1 counterparty ({TARGET_NAME}) — contact + operator registry row")


def _register_peer():
    """Register the target as a GERP in the operator's registry, not just a contact here.

    A contact is what one firm knows about another; the registry is who the PLATFORM recognizes. The
    bid names a party by gerp_id, and without a row the agent has nothing to resolve — the first
    attempt at this take spent its turns on "let me look up the gerp id" and then placed the bid
    against a name it had guessed.

    No `aws_account_id` on purpose. That field is how the operator dispatcher finds a tenant's inbox;
    with none, an event addressed here resolves, finds no account, and DROPS gracefully — which is
    exactly what a puppet counterparty should do. The acceptance still reaches the tenant, because
    the recorder emits it INBOUND as Tanners (`record.mjs` § playTanners) rather than expecting a
    real firm to answer."""
    import boto3
    tbl = boto3.Session(profile_name="operator-org").resource("dynamodb").Table(CUSTOMERS_TABLE)
    tbl.put_item(Item={"gerp_id": TARGET, "label": TARGET_NAME, "status": "active"})


# ── reset ─────────────────────────────────────────────────────────────────────────────────────────

def reset():
    """Un-raise the capital. Scoped to the counterparty, so a take's own writes come out with it.

    Three stores, because the raise lands in three: the negotiation row, the instrument it creates,
    and the cash it books. Missing any one leaves the agent able to see the deal already exists."""
    # The decoy contact (§ seed). `modules/contacts` has no delete tool — get/put/query/scan/update
    # only — so this is a direct row delete, which is the teardown convention anyway.
    fx.purge(CONTACTS_TABLE, ("contact_id",), lambda r: IS_DECOY(r.get("contact_id")),
             "decoy investor contact(s)")

    # Scoped by counterparty, which also leaves the AGREEMENT#<kind> config rows standing — they
    # are the store's dispatch config, not a take's residue.
    fx.purge(AGREEMENTS_TABLE, ("thread", "terms_hash"),
             lambda r: OWNS(r.get("thread")), "agreement row(s)")

    fx.purge(RULES_TABLE, ("pk", "sk"),
             lambda r: str(r.get("pk", "")).startswith("DISTRIBUTION#") and OWNS(r.get("pk")),
             "instrument rule instance(s)")

    # The money legs, scoped by the counterparty + the word "capital" with separators NORMALIZED —
    # the settle's own ids are `capital-<thread>`, but the agent sometimes books with a slug it
    # composed (`..._capital_outlay_...`), and an underscore-only spelling must not survive a reset.
    fx.purge(LEDGER_TABLE, ("pk", "sk"),
             lambda r: "capital" in str(r.get("sk", "")).replace("_", "-").lower() and OWNS(r.get("sk")),
             "capital ledger row(s)")


# ── check ─────────────────────────────────────────────────────────────────────────────────────────

def check():
    decoys = [r for r in fx.table(CONTACTS_TABLE).scan().get("Items", []) if IS_DECOY(r.get("contact_id"))]
    fx.require(not decoys, f"no {INVESTOR_NAME} CONTACT ({len(decoys)} found) — the take is filmed as "
                           f"Westwood, so a contact by that name is a decoy the agent will bid as")

    rows = fx.table(AGREEMENTS_TABLE).scan().get("Items", [])
    mine = [r for r in rows if OWNS(r.get("thread"))]
    t = fx.s.call("contacts", "manage_contacts", {"op": "get", "contact_id": TARGET})
    t = t.get("contact") or t
    fx.require(t.get("name") == TARGET_NAME, f"{TARGET} is a contact — how this firm knows them")

    import boto3
    peer = boto3.Session(profile_name="operator-org").resource("dynamodb").Table(CUSTOMERS_TABLE) \
        .get_item(Key={"gerp_id": TARGET}).get("Item")
    fx.require(peer is not None,
               f"{TARGET} is in the operator registry — a bid names a party by gerp_id, and without "
               f"this the agent hunts for one and guesses")

    fx.require(not mine, f"no open bid with {INVESTOR_NAME} ({len(mine)} row(s)) — an existing "
                         f"thread makes the agent negotiate against it instead of opening one")

    inst = [r for r in fx.table(RULES_TABLE).scan().get("Items", [])
            if str(r.get("pk", "")).startswith("DISTRIBUTION#") and OWNS(r.get("pk"))]
    fx.require(not inst, f"no instrument issued to {INVESTOR_NAME} yet ({len(inst)} row(s)) — the "
                         f"bid must be un-made for the take to open one")

    cap = [r for r in fx.table(LEDGER_TABLE).scan().get("Items", [])
           if "capital-" in str(r.get("sk", "")) and OWNS(r.get("sk"))]
    fx.require(not cap, f"no capital receipt on file ({len(cap)} ledger row(s)) — a bid that already "
                        f"has its funds booked is a deal, not a bid")

    # Other counterparties' threads are left alone on purpose; if this ever trips, the teardown has
    # widened past what the demo owns.
    others = [r for r in rows if not OWNS(r.get("thread"))]
    print(f"  note    {len(others):>4} agreement row(s) belonging to other counterparties, untouched")


if __name__ == "__main__":
    fx.run("demo 04 · treasury — an investor bids against a published margin", seed, reset, check)
