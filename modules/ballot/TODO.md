# ballot — TODO (stub)

A governance primitive: the agent **counts votes** arriving through chats + emails and **gates a consequential
action** on the tally before doing it — e.g. sell a treasury distribution rule, hire or terminate someone, adopt
/ retire a policy, approve a large spend.

**Democracy's place.** `modules/treasury/README.md` (§ "the cap table is stock-free" — "strip the vote,
too … discipline by flow, not by governance") deliberately **removes the vote from capital**: a distribution rule
carries no vote, the investor's lever is exit not voice, so no boards / proxy fights / activists riding a
financial claim. that's correct for the *capital* layer. but voting is legitimate for genuine **collective human
decisions** — who to hire, whether to sell an asset, which rule to adopt. `ballot` is where that goes: democracy
over people-decisions, kept out of the ownership instruments treasury cleaned up.

## the requirement: a ballot table

- **ballots** — one row per open question: `{ballot_id, question, gated_action, electorate, threshold
  (majority / supermajority / quorum), opened_at, closes_at, status (open|passed|failed)}`.
- **votes** — `{ballot_id, voter, choice (yes|no|abstain), source (chat|email), raw, received_at}`, one per
  voter. the agent records a vote as it reads one in a chat or an email; keeps the raw message as the audit trail.
- **tally + gate** — on close (deadline or quorum reached), the agent tallies against the threshold and reports
  pass/fail; the gated action proceeds ONLY on pass. the ballot + every recorded vote IS the audit record.

## open questions (spec later)

- **electorate** — who's eligible per ballot (the workers, the partners, a named set)? sourced from `contacts` / a
  roster; one-person-one-vote enforcement.
- **identity + provenance** — a vote arrives as free text; the agent parses intent → a choice, and authenticates
  the voter (email from a known address, chat as the JWT'd owner, a contact match). handling ambiguity /
  changed votes (latest wins?).
- **thresholds** — majority / supermajority / quorum / role-weighted; default simple majority + a quorum.
- **gate vs. act** — ballot emits a "passed" the acting module (treasury, labor) consumes, vs. ballot invokes the
  action directly. lean: ballot gates, the domain module acts.
- **transparency** — a passed governance decision on an openly-operated gerp is a legitimate public act
  (publishable via `oob`); individual voters' identities are personal-namespace and are never served.

Placeholder only — full requirement doc + the module when we come back to it.
