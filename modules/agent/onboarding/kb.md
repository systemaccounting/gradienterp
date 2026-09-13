# onboarding a new business — the first conversation

When the owner says "onboard my business", or the books are empty and nobody has been
interviewed, this is the walk. Configuration, not transactions: post nothing. Ask in order, in
plain words, three or four exchanges; capture what the owner volunteers. Skip what the business
does not have.

1. **What does the business do? Existing books or a fresh start?** `instruct` the category.
   Books elsewhere → after this walk, `search_guides("cutover walk")` to the trial-balance gate.
2. **Where does it run?** `set_timezone`, the IANA name (`America/Los_Angeles`, never "PST").
   `manage_locations update ordinal=1` with the city and label; `add` each further site.
3. **Public or private?** One sentence: a business publishes by default, that is how capital
   and labor find it; personal use runs private. The switch is the owner's, on their gerp
   screen — say where, do not flip it.
4. **What do you sell, how do you charge?** `manage_stock create_item` per product or service,
   each with its canonical account. Nothing sold → skip.
5. **Who supplies you, what do you buy?** `manage_contacts` per vendor. None → skip.
6. **Employees, how paid?** `manage_labor` when there are any.
7. **Payment processor?** Stripe, Square, PayPal → `search_guides` for its setup and run it.
   Cash, checks, transfers only → nothing to connect.
8. **Statements how often?** Monthly is set at creation; another cadence is an `instruct` line.

Then the rules the business described (`rule_params` catalog, `manage_rules add` on a yes);
`instruct` the durable facts about the business, `remember` those about the person. Close:
setup is done, the next conversation is bookkeeping.
