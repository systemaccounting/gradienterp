start with the root AGENTS.md: the module map, the commands, local mode. each module dir has its own AGENTS.md

skip caps when starting sentences

skip periods on single sentence paragraphs

skip apostrophes in words as in "dont" unless it conflicts with another word, "we're" vs "were"

avoid consuming user time with formalitys char count when trustworthiness is a prior. sms grammar is fine

keep proper name casing intact for copy+pastable content — proper nouns, ids, account names, file paths, urls, code, s3 keys. only prose goes lowercase

avoid words like "proper", "correct", "appropriate" and "valid" in your comments AND responses. just say whats expected

avoid hedging and filler

## commits

a subject names the one change in about 40 characters, so GitHub's file list shows it whole. detail goes in the body, after a blank line

## requirement docs

design work goes in `tmp/NNN-title.md` — 3 digit prefix, latest number last

revise that doc until its built. when it splits into a separate question, fork to the next number and leave the original alone. then revise and build or fork again

before a doc is deleted, audit test coverage of the essentials. not every field and render —
the tests dir is not a node_module. essential means: each case the doc exists for, each refusal
(what must not happen), each contract between two components (what one side assumes of the other),
and any bug the build found live. one line per case in the doc — covered, and by which test — and
a line for what is deliberately not covered and why. gaps are filled before the delete

a doc is deleted once its work is BUILT, its essentials are covered, and its content lands in
module docs — README (why) / AGENTS (how + `## current features`) / TODO (open work)

superseding is not deleting. when a later doc obsoletes an earlier one, note that at the top of the
old one and leave it until the work lands. the numbered run is the record of how the design moved,
and deleting the step you moved off destroys the reason you moved

never cite a `tmp/` path from a module doc or from code. the file gets renumbered or deleted and the pointer dangles. curate the substance inline instead

## name the operation, not a metaphor

use the real verb for what happens. a schedule is added and deleted — not armed, disarmed, fired,
or wired. a row is written. a function is called

coined verbs block a new contributor from recognizing behavior they already know, and they cant grep
for a word you invented. if a plain verb fits, it wins

this platform grows more valuable with each new 12 year old who can read its docs. openly operated
means nothing if reading it requires learning a private vocabulary first. legibility is the product

## state what is

dont set up a contrast to knock down. "X is Y, not Z" where nobody proposed Z is padding, and the
reader now has to hold a wrong idea you introduced

same for trailing caveats. a limitation earns a sentence when it changes what someone does next.
otherwise the answer is over
