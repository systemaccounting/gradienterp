# metrics

A firm's product is whatever it sells: loaves, memberships, hosted ERP accounts, a technician's
hours. Product analytics is recording each step a subject takes with that product (a lead
captured, a member joined, a member checked in) and reading distinct counts, funnels and retention
off the record. Every product analytics tool sells the same four reads, computed on one firm's
data by one person and argued about in a meeting.

This module is those reads on AWS managed tooling, per gerp, with the agent as the one who reads
them. The owner asks "how many members did we keep from January" in chat and gets the number
joined to the books: revenue per active member, cost per check-in. That line is the unit economics
the platform exists to publish.

Nothing here knows what a product is. Event names, subject ids and properties are the owner's own
words. A bakery records `loaf.sold`, a gym `member.checked_in`, gradienterp `account.signed_up`,
and each reads its own record the same way.
