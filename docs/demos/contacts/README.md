# demo · contacts — CMO

**"who are our top 5 customers and how much did they spend?"** One beat: ⚙ reading your invoices →
a single sorted list, basis labeled ("top 5 by paid invoices"): Riverside $3,500 · Delta $2,000 ·
Lofthouse $1,500 · Summit $1,250 · Nguyen $1,000.

Riverside reads $3,500 (not $2,650) because the $850 migrated invoice WAS collected — including it
is right for "paid invoices", and the agent states its basis on screen.

Depends on the persona's `## never do the arithmetic yourself` rule: sorting is arithmetic, so it
routes to the sandbox and one clean list comes back — no visible re-sorting.

Single-movement, so one gif serves both card and lightbox. Data comes from the cafe seed
(`docs/demos/seed.py`); no demo-specific fixture. Record: `record.mjs --step 2`.
