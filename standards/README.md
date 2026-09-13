# standards — the platform's own practice notes

Business decisions gradientERP takes a position on, authored here so they are reviewable and
arguable in public: a pull request is how you disagree with one.

These upload to the shared standards corpus root under the `gerp` scope
(`prod/tower/standards_corpus.tf`), where agents read them with
`get_standard("gerp/<domain>.md")` — the same call and the same key shape as a jurisdiction's
rules or a GAAP definition. They differ only in AUTHORITY: a statute has one right answer, a
professional convention has an agreed one, and a note in here is a recommendation with its
alternatives named.

What belongs here: a decision a business makes that has defensible variants, where the platform
argues for a default and names what each alternative commits you to.

What does NOT: how a tool works (that's the module's `kb.md`), what a jurisdiction requires
(that's `us/`, `ohio/` — contributed and curated, not authored here), or anything about a
particular firm. A firm electing a different practice records that as its own instruction; it
never edits a note here.
