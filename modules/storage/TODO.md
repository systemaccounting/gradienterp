# storage — open work

## hosting automations (`modules/automation`)

That module stores firm-authored scripts here — the cabinet is the store, and the path is the index
exactly as it is for documents. **Approval is expressed as a path, so it is this module's IAM that
enforces it**, not a flag anyone checks:

- **three reserved prefixes.** `automations/staged/` is where the agent writes a script;
  `automations/approved/external/` and `automations/approved/modules/` are what the two runners read,
  one each. An unapproved script is not at the path a runtime reads, so GetObject failing IS the
  answer and no code consults anything — and a script filed under the wrong prefix fails the same way
  rather than running with the wrong reach.
- **`.py` accepted by `op=put`** (today `put` takes text/markdown/json/html and derives the content
  type from the extension), on the staged prefix only.
- **`approve_automation`'s own role** — read `staged/`, write `approved/`, and the only principal
  holding the second. The copy carries annotations, so review findings ride along as a plain
  `caption`.

Worth knowing while wiring this: `s3:PutObjectAnnotation` is a single action with no
per-annotation-name granularity, and `manage_storage` and `inspect_document` both hold it
bucket-wide. Any role that can write one annotation can write them all, so annotations carry records
here and never controls.

## portal fast-follows

- **the submissions watcher** — hands-free form handling, when a flow needs it: a poke lambda in
  modules/agent (beside continue_poke, same invoke shape) added as a second `lambda_function`
  block on the uploads bucket's notification, `filter_prefix submissions/`. Until then the
  agent sweeps the prefix on ask.
- **data routes** — `/data/tasks` only; add routes as pages need them (balances, inventory).
- **secrets over the portal** — the LAST render_frame vestige: `collect_secret`'s in-chat secure
  field survives because chat is Cognito-authed and the portal's capability slug isn't auth
  enough for credential intake. If the portal ever gains JWT validation, a `secret` form kind
  (portal lambda → SSM direct, value never written to submissions/) retires the frame machinery
  entirely (chat form panel, frame_submit, the sink tag).
- **large uploads** — a portal form file rides the Function URL body (~4.5MB effective after
  base64); a bigger doc needs a presigned-PUT hand-off if it ever comes up.

## Athena / S3 Metadata (scale-out search)

`find` reads captions per-object under a prefix — fine at small doc counts. At scale, enable **S3 Metadata** so
annotations flow into managed annotation tables, and add an Athena-backed cross-object search (find by content
across all prefixes). Optional; not a v1 dependency.

## retention — S3 Object Lock

`delete` refuses a `retention:retained` caption — a soft app-level guard. Harden with S3 Object Lock on retained
keys (I-9 / legal) so retention is enforced by S3, not just the lambda.

## fast-follows

- **inspect_document fast-follows** — what the receipt→ledger loop does today is in [`AGENTS.md`](AGENTS.md).
  Remaining:
  - `id` / `free` schemas — the **Claude-vision** general path for non-receipt docs (Bedrock, `bedrock:InvokeModel`;
    Haiku 4.5 default, maybe a `quality` flag for hard docs). Receipt path stays Textract.
  - **multi-page / >10 MB** — async Textract (S3-sourced, 500 MB / 3000 pages); or downscale oversized images in
    the lambda (`sharp`) before the sync call rather than raise the 10 MB cap.
  - **confidence threshold** — the Textract score below which the agent asks instead of posts; tune on real scans.
  - **billing** — Textract per-page + vision tokens → aws-passthrough + markup (like plaid); the `inspection`
    annotation cache keeps re-inspection free.
  - **committed live integ test** — the S3 / Textract path only has ad-hoc proofs so far.
  - graduate to a `modules/vision` module only if inspection grows past storage's orbit (batch, video, many
    doc-types). Non-goals: monster/non-document files and PII redaction (the personal namespace stays unpublished
    regardless — a retention concern, not an inspection one).
- **attach-to-record** — the consumer (an `accounting` expense, an `invoicing` estimate) stores the key; wire it
  on those tools, not here.
- **live integ test** — the pure logic (`matches` / `captionFrom` / routing) has offline tests
  (`tests/storage/local/manage_storage.test.mjs`, via the JS runner). Add a live integ test of the S3 / annotation
  ops against the applied bucket (put/get + copy-carries already proven via the CLI smoke test).

## if the one lambda gets too general — split

`manage_storage` is one op-dispatched lambda by design (to avoid a lambda-per-verb explosion). If the op union
gets unwieldy, split along the natural seam: the `file` **sink** (write path, frame sink) vs. the read/manage ops
(gateway tool). Not needed yet.
