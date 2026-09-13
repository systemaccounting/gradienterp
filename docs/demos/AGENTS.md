# recording login-page demos

The five `.demo-row` clips on gradienterp.cloud (CFO · CMO · COO · CPO · BOARD) are real recordings
of the live agent answering the caption prompts. Each demo owns a directory here with everything
that reproduces it; the media itself (takes, cuts, stills) is generated, gitignored, zip-excluded,
and served from `assets.gradienterp.cloud` — the working tree stays scripts-only.

## current features

- `record.mjs` — the recorder. Playwright: fresh login → chat → type (real CDP keys, 85 ms/char) →
  wait → `out/demo-<mod>.webm`. `--step <n>` picks the demo; multi-turn steps drive the puppet
  counterparty between turns; `--probe` screenshots the framing without an agent turn.
- `cut.sh` — the cutter: `bash cut.sh <in.webm> <out.gif> START-END@SPEED...` + `HOLD=`. General,
  never edited per demo.
- `fixture.py` — the fixture harness: `fx.run(name, seed, reset, check)` gives every demo the same
  CLI and safety properties (below).
- **per-demo dirs** (`accounting` · `contacts` · `labor` · `invest` · `reorder`) — each holds its
  `cut.sh` (segments + beat map + stills emission), its fixture (`seed.py` / `analysis_fixtures.py`)
  and README where the demo has a storyboard. The demo's data ships with the demo.
- **stills for agents** — each `cut.sh` also emits PNG frames into `prod/gradienterp_cloud/assets/`
  (short demos ship the END frame only — the whole conversation on one screen, the readable
  transcript; long demos add a MID frame for the exchange the end frame scrolls past — never a
  start frame, which is just empty chat). `llms.txt § demos` points at them.
- **shipping** — `bash scripts/deploy.sh assets` etag-diffs `prod/gradienterp_cloud/assets/` to the
  CDN bucket and invalidates what changed. The login page's `DEMOS` array in
  `prod/gradienterp_cloud/web/app.js` maps rows to gifs (`gif:` overrides the module name).

## media: backup & restore

Takes and cuts live off-machine on the assets bucket under a `BACKUP/` prefix (a bucket-policy Deny
keeps `/BACKUP/*` off the CDN; only operator creds read it). The working tree carries no media.

```
# restore everything (before a re-cut — cut.sh needs the webm):
AWS_PROFILE=operator-org aws s3 sync s3://gradienterp-cloud-assets-185369506315/BACKUP/docs/demos docs/demos --no-cli-pager

# back up after a new take (sync-diffed; only the delta uploads):
AWS_PROFILE=operator-org aws s3 sync docs/demos s3://gradienterp-cloud-assets-185369506315/BACKUP/docs/demos \
  --exclude "*" --include "*.webm" --include "*.gif" --include "*.png" --no-cli-pager
```

**Keep every webm.** A pacing fix is a re-cut from the webm, not a re-record, and some takes are
expensive non-deterministic rolls. Superseded takes stay with a suffix (`-v1-...`).

## record a take

Setup (any Mac): Node ≥ 18 · `cd tests/e2e && npm install && npm run setup` · `brew install ffmpeg` ·
the `operator-org` profile · e2e login creds in SSM (`tests/AGENTS.md` § browser e2e; the owner must
own the gerp under test).

```
.venv/bin/python <demo dir>/seed.py        # reset + seed + check — ALWAYS before a take
node docs/demos/record.mjs --step <n>      # the take → out/demo-<mod>.webm
```

- **Type through Playwright (CDP), never the OS** — the automation browser has no key window, so OS
  keystrokes have no responder. Consequence: the recorder needs zero macOS permissions; if a demo
  "won't type" it's a selector or login issue.
- **Frame in-camera, no crop**: viewport ~880×720 (≈ the 760px chat bubble + margin),
  `deviceScaleFactor: 2`, `recordVideo.size = viewport × 2` same aspect. Cognito renders the login
  form twice — target `input[name="username"]:visible`. Iterate framing on `--probe`, not live takes.

## webm → gif

**No duration limit — budget by content.** A total-seconds target forces one speedup across beats
that need opposite treatment; cut each beat to what a viewer needs, let the total land.

- **read beats at 1×**, sized to the text (~2.5s a one-liner, 4s+ multi-line). Typing and ⚙ beats
  compress to ~6–12× — legible texture, never smeared away: the ⚙ lines are the audit trail. Dead
  thinking pauses cut out entirely.
- **one typing RATE across the set (`@1.5`)** — a long prompt takes proportionally longer, which is
  correct. (A card may run a long prompt `@2`; note the deviation in the script.)
- **open on ~1s of still empty chat** (the gif loops) and **give a beat before send** (slow the
  finished prompt, e.g. `@0.3`).
- **multi-turn: the between-turn pause (~2s) sits on the DARK composer**; the field lighting up is
  the cut into the next turn's typing.
- **never end on an active composer** — stop before the border relights. If the answer's last words
  and the relight share a frame, the complete closer wins; a re-record with a longer refocus delay
  is the real fix.
- **end with `HOLD=<s>`, never a slowed last segment** — GIF stores per-frame delay, so the hold
  costs one frame; a slowed still is duplicate bytes. (`mpdecimate` is deliberately absent — it
  deletes duration.)
- **card vs lightbox: one boundary list, two speed columns.** `demo-<name>.gif` = the card (first
  movement, light, ~10-25s); `demo-<name>-full.gif` = the lightbox source (every prompt, 1×
  bubbles). A single-movement demo skips the split. One shared palette per gif (per-segment
  palettes shift colors at every cut). What the page actually loads: the rows use `-thumb.gif`
  (~50-140K, still animated) and the lightbox streams `-full.mp4` (~10x lighter than the gif,
  which stays as its fallback) — cut.sh emits both from its gifs.

**Find the beats off contact sheets, never by guessing** — then the numbers ship ONLY in the demo's
`cut.sh`, with the beat map and any deviation as its comments (a doc repeating the numbers drifts):

```
ffmpeg -v error -i take.webm -vf "fps=1/6,scale=260:-1,tile=6x4" -frames:v 1 sheet.png   # whole take
ffmpeg -v error -ss 6 -to 18 -i take.webm -vf "fps=2,scale=280:-1,tile=6x4" -frames:v 1 fine.png
# tile n is at t = start + (n-1)/fps
```

Judge a cut by WATCHING it (does each bubble hold, do the ⚙ lines read, does it survive a
phone-size render) — not by its duration. Output 880px @ 15fps, no crop.

## demo data

Two fixture layers; mixing them produces takes that look fine and are wrong:

| | what | where |
|---|---|---|
| **the business** | the coherent cafe — chart, customers, vendors, items, roster, balanced books | `docs/demos/seed.py` (thin consumer of `scripts/seed_dev.py`) |
| **one demo's premise** | the data that demo's claim depends on | `<demo dir>/seed.py` on the harness |

```
.venv/bin/python <demo dir>/seed.py            # reset, then seed, then check
.venv/bin/python <demo dir>/seed.py --reset    # tear down only
.venv/bin/python <demo dir>/seed.py --check    # is this demo recordable right now?
```

The rules the harness encodes, which every new fixture follows:

- **`--seed` always resets first, and a take is always preceded by it.** The tenant is shared: the
  last demo's leftovers are what the next demo's agent finds — and the agent goes looking, that's
  the product. A failed take leaves writes too.
- **Seeding writes through the tools a real tenant uses** (`seed_dev.call`, `fx.file_document` —
  the caption written by `manage_storage op=file` IS what `find` matches). Bytes placed by a side
  door are invisible to the agent and undeletable by name.
- **Teardown deletes rows directly, then re-reads to prove the state is clean.** Never report
  success by counting calls; count what's left. `--check` asserts the demo's PREMISE, not row
  counts, and exits nonzero.
- **Scope the reset to the state surface, not to what the seed wrote** — the take's own writes
  (bookings, POs, filed documents) must come out with it. **Key teardown off what YOU control**
  (item ids, worker ids, tables), never off strings the agent chose (sources, memos, entry ids —
  it picks them differently every take).
- **The fixture carries the story.** A demo's claim is a claim about data: distinguishable records
  are what let the agent explain itself unprompted. When the agent says it can't do something,
  that is usually a fixture gap, not a model failure.

Full tenant reset + the cafe, when the books themselves are stale:

```
bash scripts/reset-dev.sh --yes          # wipe data, keep config
.venv/bin/python docs/demos/seed.py      # the cafe: balanced TB, lively P&L, roster, AR/AP aging
```
