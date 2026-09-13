# schemas

## the schema is data, not code

a coffee stand stocks beans by the pound. an auto shop stocks parts by fitment, OEM cross-reference, and bin location. a candle maker stocks wax, wick, and jars that compose into a finished good. same `inventory` module, same DynamoDB table, same code path — three vocabularies that share almost nothing.

conventional ERP handles this by forking: a restaurant edition, an auto edition, a manufacturing edition — each a rigid schema baked into the code, each customization a migration and a consultant. the schema is a build-time decision, so serving a new shape means a new build.

here the schema is a **row**. field shapes live in a per-customer registry table, and the agent writes to it at runtime. onboarding an auto shop, the agent adds `fitment`, `oem_cross_ref`, `bin` to the item vocabulary — a single `extend_schema` write — and the next part it files carries them. no new module, no `ALTER TABLE`, no backfill, no redeploy. one codebase serves every business because the part that varies per business isn't in the code.

## the NoSQL dividend

DynamoDB stores a record as whatever keys it's handed — there is no column set to migrate. that's the exploit: a new attribute is a write, not a schema change. adding `core_charge` to a brake rotor is the same operation as setting its price. the storage layer never has to know a shape in advance, and never has to change when the shape does.

the registry doesn't fight this — it rides it. it gates field *names* (so `quantiy` never slips in next to `quantity`, and so the vocabulary stays clean enough to compare across customers) while the domain tables hold the actual records with no shape imposed. the validation is a **coherence gate, not a storage constraint**: NoSQL would take anything; the registry just keeps what it takes legible.

## the vocabulary self-assembles

extensions don't stay local. every `extend_schema` emits `platform.schema.extended.v1` on the operator bus, and when enough businesses independently land on one row on the same field — a dozen auto shops all adding `fitment` — the operator promotes it into the canonical baseline every future shop is seeded with. the field vocabulary isn't designed top-down by the platform; it **emerges bottom-up from what real businesses actually track**, and hardens into canon by use.

that's the same move as everything else here: don't impose a 19th-century template — the "auto-shop chart of accounts," the "restaurant item schema" — on top of the business. measure what the business actually has, in the open, and let the shared vocabulary precipitate out of the measurements. the registry is where the platform's ontology grows itself.

## the mechanism

three layers (canonical baseline in `data/` → per-customer DDB → promotion), four agent-mediated flows, six lambdas — the DDB row schema, the extend / pull / promote flows, and the tool set are all in [`AGENTS.md`](AGENTS.md). the canonical per-business payoff: an auto mechanic's parts carry rich per-part attributes (part #, OEM cross-ref, fitment, bin, core charge) via agent-extended `item_fields` — zero migration.
