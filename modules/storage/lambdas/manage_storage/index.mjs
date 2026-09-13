/**
 * manage_storage — the agent's filing-cabinet tool (see storage.md).
 *
 * One op-dispatched lambda over the per-gerp encrypted uploads bucket. The caption lives ON each
 * object as an S3 annotation named `caption` (JSON {title, note, tags, occurred_at, retention}) — no
 * metadata table, and the object path is the index. Bytes never transit the agent: uploads land via a
 * presigned PUT (the render_frame `file` field / chat lambda), reads hand back a presigned GET link.
 *
 * ops (event.op): file | put | read | get | describe | find | move | copy | delete.
 *
 * put/read close the agent's own loop: `file` handles human uploads (bytes never transit the
 * agent), while `put` writes a document the agent COMPOSED (a compliance index, a filing JSON)
 * and `read` hands the content back inline — a presigned URL is for humans; the agent can't
 * fetch one. Text-sized documents only (PUT_MAX / READ_MAX).
 *
 * Node (not Python) because the annotation API is new: the modular @aws-sdk/client-s3 bundles small
 * (~16MB) vs. boto3's monolith, and matches the repo's other Node lambdas (chat, plaid_webhook). The
 * caption inherits the object's SSE-KMS, so it's encrypted like the blob.
 */
import {
  S3Client, PutObjectAnnotationCommand, GetObjectAnnotationCommand, HeadObjectCommand,
  ListObjectsV2Command, CopyObjectCommand, DeleteObjectCommand, GetObjectCommand, PutObjectCommand,
  ListObjectVersionsCommand, DeleteObjectsCommand,
  NoSuchAnnotation,
} from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { createHmac } from "node:crypto";

// the form slug the ui lambda serves pages/forms/ under — the same derivation as its `formSlugOf`
export const formSlugOf = (portalSlug) => createHmac("sha256", portalSlug).update("forms").digest("hex").slice(0, 32);

// exported as a holder so a test can stand in a client; the handler reads it per call
export const clients = { s3: new S3Client({}) };
const BUCKET = process.env.STORAGE_BUCKET;
const CAPTION = "caption"; // the annotation carrying {title, note, tags, occurred_at, retention}
const GET_TTL = 3600;      // presigned GET link lifetime (seconds)

const resp = (body, statusCode = 200) => ({ statusCode, body: JSON.stringify(body) });
const copySource = (key) => `${BUCKET}/${key.split("/").map(encodeURIComponent).join("/")}`;

// ─── caption annotation (the metadata, living on the object) ───

async function putCaption(key, caption) {
  await clients.s3.send(new PutObjectAnnotationCommand({
    Bucket: BUCKET, Key: key, AnnotationName: CAPTION,
    AnnotationPayload: new TextEncoder().encode(JSON.stringify(caption)), // UTF-8, ≤ 1 MiB; inherits SSE-KMS
  }));
}

async function getCaption(key) {
  try {
    const r = await clients.s3.send(new GetObjectAnnotationCommand({ Bucket: BUCKET, Key: key, AnnotationName: CAPTION }));
    return JSON.parse(await r.AnnotationPayload.transformToString());
  } catch (e) {
    if (e instanceof NoSuchAnnotation || e.name === "NoSuchAnnotation") return {}; // uploaded but uncaptioned
    throw e;
  }
}

export function captionFrom(e) {
  const c = {};
  for (const k of ["title", "note", "tags", "occurred_at", "retention"]) if (e[k] != null) c[k] = e[k];
  return c;
}

export function matches(cap, query, tag, after, before) {
  if (tag && !(cap.tags || []).includes(tag)) return false;
  const occ = cap.occurred_at;
  if (after && (!occ || occ < after)) return false;
  if (before && (!occ || occ > before)) return false;
  if (query) {
    const hay = [cap.title || "", cap.note || "", (cap.tags || []).join(" ")].join(" ").toLowerCase();
    if (!query.toLowerCase().split(/\s+/).filter(Boolean).every((t) => hay.includes(t))) return false;
  }
  return true;
}

// ─── ops ───

const opFile = async (e) => {
  // caption the object at `key` (a portal submission file gets op=move'd to its semantic key
  // first, then filed here)
  await putCaption(e.key, captionFrom(e));
  return resp({ status: "filed", key: e.key });
};

const PUT_MAX = 256 * 1024;  // a composed document is prose/JSON, not a blob — blobs go via `file`
const READ_MAX = 256 * 1024;
const TEXT_TYPES = {
  md: "text/markdown", json: "application/json", txt: "text/plain", csv: "text/csv",
  html: "text/html", css: "text/css", js: "text/javascript",
  py: "text/x-python", // automation scripts staged under automations/staged/
};

const opPut = async (e) => {
  if (typeof e.content !== "string" || !e.content) return resp({ error: "content (a non-empty string) is required for put" }, 400);
  if (e.content.length > PUT_MAX) return resp({ error: `content exceeds ${PUT_MAX} bytes — put is for composed documents; file blobs via op=file` }, 400);
  const ext = (e.key.split(".").pop() || "").toLowerCase();
  await clients.s3.send(new PutObjectCommand({
    Bucket: BUCKET, Key: e.key, Body: e.content, ContentType: TEXT_TYPES[ext] || "text/plain",
  }));
  await putCaption(e.key, captionFrom(e));
  const out = { status: "put", key: e.key, size: e.content.length };
  // keys under pages/ serve on the owner portal — hand back the link the owner can open. A page under
  // pages/forms/ also gets the form link: the one to send anyone outside the chat, which opens that
  // form and nothing else
  if (process.env.PORTAL_URL && e.key.startsWith("pages/")) {
    out.url = `${process.env.PORTAL_URL}/${e.key.slice("pages/".length)}`;
    if (e.key.startsWith("pages/forms/")) {
      const base = process.env.PORTAL_URL.replace(/\/+$/, "");
      const owner = base.slice(base.lastIndexOf("/") + 1);
      out.form_url = `${base.slice(0, base.lastIndexOf("/"))}/${formSlugOf(owner)}/${e.key.slice("pages/forms/".length)}`;
    }
  }
  return resp(out);
};

// a read is a window: max_bytes (default 16KB) from offset. A 40KB document read whole for one
// figure is 40KB in the model's context on every later round trip of the session; a window is
// what a reader needs, and `next_offset` is how it turns the page.
const READ_WINDOW = 16 * 1024;

const opRead = async (e) => {
  const head = await clients.s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: e.key }));
  const size = head.ContentLength ?? 0;
  if (size > READ_MAX) {
    return resp({ error: `object is ${size} bytes — read is for text documents; hand the owner a link via op=get` }, 400);
  }
  const offset = Math.max(0, Number(e.offset) || 0);
  const maxBytes = Math.max(1, Number(e.max_bytes) || READ_WINDOW);
  if (offset >= size) {
    return resp({ key: e.key, content: "", content_type: head.ContentType, size, offset, truncated: false, next_offset: null });
  }
  const end = Math.min(size, offset + maxBytes);
  const r = await clients.s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: e.key, Range: `bytes=${offset}-${end - 1}` }));
  const truncated = end < size;
  return resp({ key: e.key, content: await r.Body.transformToString(), content_type: head.ContentType,
                size, offset, truncated, next_offset: truncated ? end : null });
};

const opGet = async (e) => {
  const url = await getSignedUrl(s3, new GetObjectCommand({ Bucket: BUCKET, Key: e.key }), { expiresIn: GET_TTL });
  return resp({ key: e.key, url });
};

const opDescribe = async (e) => {
  const head = await clients.s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: e.key }));
  return resp({
    key: e.key, caption: await getCaption(e.key),
    size: head.ContentLength, content_type: head.ContentType, last_modified: head.LastModified?.toISOString(),
  });
};

const opFind = async (e) => {
  const { prefix = "", query, tag, after, before } = e;
  const hits = [];
  let token;
  do {
    const page = await clients.s3.send(new ListObjectsV2Command({ Bucket: BUCKET, Prefix: prefix, MaxKeys: 1000, ContinuationToken: token }));
    for (const obj of page.Contents || []) {
      const cap = await getCaption(obj.Key); // the agent narrows `prefix` first, so this stays a small fan-out
      if (matches(cap, query, tag, after, before)) hits.push({ key: obj.Key, caption: cap, size: obj.Size });
    }
    token = page.NextContinuationToken;
  } while (token);
  return resp({ count: hits.length, documents: hits });
};

async function s3copy(src, dst) {
  await clients.s3.send(new CopyObjectCommand({ Bucket: BUCKET, Key: dst, CopySource: copySource(src) })); // annotation rides the copy
}

const opMove = async (e) => { await s3copy(e.from, e.to); await clients.s3.send(new DeleteObjectCommand({ Bucket: BUCKET, Key: e.from })); return resp({ status: "moved", from: e.from, to: e.to }); };
const opCopy = async (e) => { await s3copy(e.from, e.to); return resp({ status: "copied", from: e.from, to: e.to }); };

// Delete means delete. The bucket is versioned, so a plain DeleteObject only writes a delete
// marker: the object vanishes from every listing while its bytes stay billed forever. This lists
// the key's versions and removes them in one batch. Prefix matches partially, so filter to the
// exact key — deleting `invoice.pdf` must not take `invoice.pdf.bak` with it.
const opDelete = async (e) => {
  if ((await getCaption(e.key)).retention === "retained") return resp({ status: "refused", reason: "retained document", key: e.key }, 409);
  const v = await clients.s3.send(new ListObjectVersionsCommand({ Bucket: BUCKET, Prefix: e.key }));
  const objects = [...(v.Versions || []), ...(v.DeleteMarkers || [])]
    .filter((o) => o.Key === e.key)
    .map((o) => ({ Key: o.Key, VersionId: o.VersionId }));
  if (objects.length) await clients.s3.send(new DeleteObjectsCommand({ Bucket: BUCKET, Delete: { Objects: objects } }));
  else await clients.s3.send(new DeleteObjectCommand({ Bucket: BUCKET, Key: e.key }));
  return resp({ status: "deleted", key: e.key, versions: objects.length });
};

const OPS = { file: opFile, put: opPut, read: opRead, get: opGet, describe: opDescribe, find: opFind, move: opMove, copy: opCopy, delete: opDelete };

export const handler = async (event) => {
  const body = typeof event.body === "string" ? JSON.parse(event.body) : event;
  const fn = OPS[body.op];
  if (!fn) return resp({ error: `unknown op ${JSON.stringify(body.op)}` }, 400);
  return fn(body);
};
