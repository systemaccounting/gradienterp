# demos — open work

- **puppet capture infra is still APPLIED** on the operator account (the `tanners`-namespace
  queue + rule). Tear down with
  `bash scripts/puppet.sh --acctid 185369506315 --namespace tanners --destroy`, or keep it for
  more takes. (A `--send` needs no capture infra — only `--poll`/`--receive` do.)
- **a `tanners-coffee-co` contact + operator-registry row are seeded** on the dogfood tenant (the
  invest fixture writes them). Harmless; remove for a pristine tenant.
- **a sixth take: the offer-to-automate beat** — the reorder demo can't show the agent OFFERING
  the reorder rule (par must already exist for its loop). A take on an item with NO rule would:
  owner describes structure → agent offers → `manage_rules` (op: add) writes two rows on yes.
- **a closing beat for reorder** — a fourth turn ("the beans just showed up") ends the loop on the
  receipt booking the payable and moving the shelf; needs a third puppet-free turn in step 6.
- **two-section landing layout** — the page ships one list of five rows; the design idea of
  setting the two cross-firm rows visually apart (the punchline section) is unbuilt.
- **live dogfood header** — the chat header falls back to the account email; a real
  `business_name` means setting the tenant blob in SSM AND an image rebuild (it feeds the
  persona's `{{ business_name }}`).
