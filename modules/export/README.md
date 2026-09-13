# taking your data with you

The terms say you may cancel anytime by closing the account, that your books and documents are
exported to you at closure, and that the account keeps running for 15 days afterward — billed — so
you can back them up. A firm has to keep its accounting records for years. "You may cancel anytime"
is only true if leaving hands them back.

## the 15 days is the design

Closure triggers the export and starts a clock. The account, its bucket and its credentials all keep
working for the window; then everything in it is deleted.

Every awkward question the export had turns out to be answered by "the account is still there" —
where an export survives, how a credential outlives the session that issued it, what happens to a
download that stopped halfway. The window is what makes an export that lives in the customer's own
bucket, and dies with it, the right shape.

## what is in it, and what is not

An export with no arguments takes everything that is theirs. Narrowing is the customer's to ask for,
never the platform's to apply quietly — omitting part of someone's books is the one failure an
export cannot have.

The line the tiers draw is **what the firm created, or is obliged to keep**, against **what the
platform generated about its own operation**:

| | |
|---|---|
| the filing cabinet | every document in the gerp's bucket, under its original keys |
| books | ledger, balances, pending — the retention-obligation set |
| what they sold and bought | invoices with their lines, purchase orders, agreements, treasury, shipping |
| who they deal with | contacts |
| what they hold | inventory items and movements |
| who worked | labor time entries, workers, worker-legal |
| how they run | settings, rule instances — their configuration, not ours |
| the rest of their records | calendar events, notes, tasks, assets, the invoice audit trail |

against the platform's own residue — the webhook dedup log, the payment dead letters, the inbound
event record, agent chat turns, the mail dedup table. Still theirs, still exported by default, but
unbounded and dull: the first thing to drop when someone wants a smaller copy, and the reason the
tiers exist at all.

Two things are never theirs to take. `schema` is gradientERP's field definitions, identical in every
gerp, and ships as a README explaining the JSONL shapes instead. `export-jobs` is the export's own
work list, which changes while the run reads it.

`rules-params` is the one table whose rows differ in whose they are: `pk = GENERAL` is the law —
bracket tables, wage bases, seeded from canonical and identical everywhere — and a contact_id is the
firm's own. A table like that cannot be decided at table granularity, which is worth knowing before
someone writes the next one.

## a link and a script, not a credential

The customer gets a link. They fetch one file and run it, with nothing to paste and nothing to
configure — no AWS account, no console, no long-lived key.

That shape is also the reason this is an agent tool rather than a download button. The credential
inside the script lasts an hour; when it expires, asking for the link again rewrites the script
against the same export rather than making a second copy of the books. Expiry is a sentence, not a
support request.

And a link to a self-contained script is a thing you can hand to something else: "fetch this and run
it" works for a local agent as well as a person. The credential it carries reads one prefix of one
bucket for one hour and does nothing else.

## what it does not do

- **Not a backup.** It is a copy for the customer to keep, not a restore path. Reimporting an export
  into a new gerp is a different job and nobody has asked for it.
- **No filtering.** Everything the gerp holds, `worker-legal` and the payment DLQ bodies included.
  Withholding parts of someone's records to keep the output tidy is the platform deciding what a
  firm may keep.
- **Not the published feed.** openlyoperated.biz is a publication and unrelated to this.

How it works, and why it is built the way it is: `AGENTS.md`.
