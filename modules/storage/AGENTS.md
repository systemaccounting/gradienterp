# storage module

The per-gerp document store: the `manage_storage` tool over **agent's** encrypted uploads bucket (agent owns the
bucket — the file-field substrate; storage is the management layer). The **caption lives on the object** as an S3
annotation (`caption` = JSON `{title, note, tags, occurred_at, retention}`) — no metadata table — and the **object
path is the index**. A second tool, `inspect_document`, is the one that *opens* an object (Textract) — the seam:
`manage_storage` organizes objects and never reads contents; `inspect_document` reads the bytes. Contract =
each tool's `index.mjs` + `schema.json` + this file; the filing-side rationale is in
[`storage.md`](../../storage.md), the inspection-side design is in `## current features` here + [`TODO.md`](TODO.md).

## current features

- `manage_storage` read is a window: `max_bytes` (default 16KB) from `offset`; the reply carries `size`, `truncated` and `next_offset`. A 40KB document is read a page at a time, and only the page lands in the model's context.

- `manage_storage` lambda (**Node** / `@aws-sdk/client-s3`) — one op-dispatched tool (deliberately not a lambda-per-verb): `file` (caption the object at `key`;
  a portal submission file gets `op=move`'d to its semantic key first, then filed),
  `put` (write a text/markdown/json/html document the AGENT composed — a compliance index, a portal page; ≤256KB, content type from the key's extension),
  `read` (the content back inline — a presigned URL is for humans, the agent can't fetch one; ≤256KB text), `get` (presigned GET link), `describe`
  (caption + HEAD size/type/modified), `find` (`ListObjectsV2` on a prefix → read captions → filter by query /
  tag / `occurred_at`), `move` (copy + delete), `copy`, `delete` (refuses `retention:retained`; purges every VERSION of the
  key, since the bucket is versioned and a plain delete would leave the bytes billed behind a delete
  marker). The bucket is
  versioned, so agent-composed living documents carry their history through plain rewrites.
- an agent gateway tool; human uploads arrive as PORTAL form submissions (multipart files under
  `submissions/<kind>/` — bytes never transit the agent), then move + file to semantic keys.
- `inspect_document` lambda (**Node** / Textract `AnalyzeExpense`) — reads a document's bytes into structured
  fields (receipt: vendor / total / tax / date / line_items + a confidence score); parses Textract's `"$84.23"`
  to floats at the boundary so the agent hands a number straight to `post_journal_entry`. Caches the result as an
  `inspection` annotation on the object (rides `CopyObject`, so it carries staging → the semantic key; `refresh`
  re-runs). **Own least-priv role** — `textract:AnalyzeExpense` + `kms:Decrypt` stay off the filing role.
  A gateway tool: inspect by `key` — a portal submission file before filing, or a filed doc
  (read-it-back). Textract reads the *number* so a
  hallucinated total can't reach the ledger; proven live on `staples-receipt.jpg` (a photo of a screen) at 100%.
- operates on the encrypted uploads bucket + CMK **owned by `modules/agent`** (`uploads.tf`) — passed in as
  `storage_bucket` / `storage_kms_key_arn`. annotations inherit the object's SSE-KMS, so captions are encrypted
  too. standard general-purpose bucket (annotations aren't on directory / Express One Zone buckets).
- `ui` lambda (**Node**, Function URL, `ui.tf`) — the gerp's web server (today: the owner portal; the public site rides it when selfhosting builds): serves
  agent-published pages from the `pages/` prefix (html bodies get the shell: `<base>`, the `ui.data()`
  helper, version-poll auto-refresh), live `/data/tasks` read-through (open-tasks-index), form intake
  (`POST f/<kind>` → `submissions/<kind>/<ts>-<id>.json` + caption — the S3 put can trigger a
  notification to an agent-poke watcher for hands-free handling; none wired today, the agent
  sweeps the prefix on ask), and the tasks-table DDB stream as a second trigger bumping
  `state/portal.version` so open pages refresh. The per-gerp `random_id` slug in the path is the capability, checked in code — wrong slug = the
  same 404 as a missing page. A second slug, `formSlugOf(owner slug)` (an HMAC, so the owner slug can't be read back out of it),
  serves only `pages/forms/`, their `POST f/<kind>` and the version marker; everything else under it is a 404. Publishing needs no
  tool: `manage_storage op=put key=pages/...` (returns the served url via `PORTAL_URL` env; a put under `pages/forms/` also
  returns `form_url`, the link for anyone outside the chat). The public surface design lives in `modules/site/`.
- the `/s3` routes (owner slug only) — `GET ?prefix=` lists (JSON with each key's caption), `GET ?key=` answers
  `302` to a presigned `GetObject` URL on the bucket's own endpoint (five minutes, SigV4; a known extension sets the
  response type) after a `HeadObject` (missing or denied → 404), and `DELETE ?key=` removes every version. An object
  never renders on the portal's origin — a form upload carries whatever `Content-Type` its uploader sent — and the
  get serves past the Function URL's 6 MB response. A page `fetch()`ing an object follows the redirect
  cross-origin: the uploads bucket (`prod/init_customer`) and the email bucket (`modules/agent`) allow `GET` from
  any origin, the signature being the capability. `?bucket=email` reads the inbound-mail bucket. The delete refuses
  a document captioned `retention: "retained"` with the same 409 as `manage_storage op=delete`; deleting under
  `automations/approved/` stays allowed (retiring an automation).
- security headers on every portal response (`SECURITY_HEADERS` in `ui/index.mjs`): agent-written pages and the
  shell run inline scripts, so the CSP leaves scripts alone — `object-src 'none'; base-uri 'self'; frame-ancestors
  'none'` — with `nosniff`, HSTS, `Cross-Origin-Opener-Policy: same-origin-allow-popups`, and `Referrer-Policy:
  no-referrer`, which keeps the slug out of the `Referer` a page's links and images send (they still link and load).
- outputs: `manage_storage_fn_name`, `inspect_document_fn_name`, `portal_url` (surfaced from
  `prod/per_customer` too — the link the agent hands the owner).

## `automations/approved/` is closed to everything but the approver

`manage_storage` writes the whole cabinet, which is what makes it useful and what makes one prefix
dangerous: the agent AUTHORS automation scripts with this tool, and must not be able to bless one.

Two layers, both `s3:PutObject` and nothing else:

- an explicit **Deny** on `automations/approved/*` in this role — a Deny rather than a narrowed
  Allow, because the grant above it is bucket-wide and a Deny is what beats it. It gives the agent a
  legible error.
- a **bucket policy** on the cabinet denying `PutObject` there to every principal except
  `approve_automation`. That is the one that matters: it holds for `modules/labor`'s bucket-wide
  delete and for roles nobody has written yet, and it refused an admin session when tested.

**Put only. Deleting an approved script is how an automation is retired** — the risk being closed is
unreviewed content APPEARING there, not content leaving.

`op=copy` and `op=move` into that prefix are refused by the same rule, because a copy is a
`PutObject` at the DESTINATION. That is the bypass that would matter: staged is agent-writable, so
without it the gate would be one extra call away from decorative.

## the index is the path

The agent files at a semantic key (`receipts/electronics/camera-2026-05.png`) — that hierarchy IS the index.
`find` narrows by prefix, then reads captions on the candidates via `GetObjectAnnotation`; no query engine
(Athena over S3 Metadata tables is a scale-only add-on, see `TODO.md`). The agent removes the filing toil, it
doesn't conceal the store — the paths and the bucket are open to the owner like the rest of the gerp's infra.

## caption = annotation, not a row

`op=file` writes the `caption` annotation on the object at `key`. A human upload arrives as a portal
form submission (`submissions/<kind>/<ts>-<id>/<name>` — the portal lambda writes it; the client never
names a key); the agent `op=move`s it to the semantic key it composes, then files. Because the caption
lives on the object, it rides `CopyObject` (move / copy carry it — the `inspection` cache too) and is
deleted with the object — no row↔key sync, no table. Captions are ≤1 MiB UTF-8 and inherit the
object's KMS encryption.

## status: live on gradienterp

The bucket + CMK stay owned by `modules/agent` (agent is upstream of every domain module, so it can't depend on
storage); `modules/storage` consumes them (`storage_bucket` / `storage_kms_key_arn`), wired into
`prod/per_customer/main.tf` (`module "storage"`, `depends_on = [module.agent]`), and **applied on gradienterp** —
the full op set is proven live against agent's bucket (incl. the portal-submission move+file flow, 2026-07-31).

The object-annotation APIs are new (2026); the lambda is **Node**, bundling the modular `@aws-sdk/client-s3`
(which ships `PutObjectAnnotationCommand` etc. + the `NoSuchAnnotation` error) — no runtime-SDK dependency, and
~16MB vs. a boto3 layer's 30MB. The feature is proven working live: put/get a caption annotation, and **CopyObject
carries it** (so `move` / `copy` keep the caption without re-writing it).
