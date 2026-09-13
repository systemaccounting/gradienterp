/**
 * ui — the gerp's web server (see ../../AGENTS.md § ui; the public surface: modules/site).
 *
 * Serves the owner portal today (slug'd pages/); grows the public site (public/, no slug)
 * when selfhosting builds.
 *
 * One Function-URL lambda, two triggers:
 *   http  — GET  /<slug>/<path>        serve pages/<path> from the cabinet bucket
 *           GET  /<slug>/data/<route>  live read-through data (tables at request time)
 *           GET  /<slug>/version       the change marker the shell polls
 *           POST /<slug>/f/<kind>      form intake → submissions/<kind>/<ts>-<id>.json
 *   ddb stream (tasks table) — bump the version marker so open pages refresh
 *
 * The slug is the per-gerp capability: checked in code, wrong slug = the same 404 as a missing
 * page. Served html gets the SHELL wrapped around the agent-authored body — the refresh physics
 * (focus refetch + visibility-gated version polling, reload skipped while a form is dirty) is
 * plumbing here, never agent composition. A body that is already a full document serves as-is.
 */
import { S3Client, GetObjectCommand, PutObjectCommand, PutObjectAnnotationCommand,
         ListObjectsV2Command, GetObjectAnnotationCommand, DeleteObjectCommand,
         ListObjectVersionsCommand, DeleteObjectsCommand } from "@aws-sdk/client-s3";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, QueryCommand } from "@aws-sdk/lib-dynamodb";
import { createHmac } from "node:crypto";

const s3 = new S3Client({});
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

const BUCKET = process.env.STORAGE_BUCKET;
const EMAIL_BUCKET = process.env.EMAIL_BUCKET || "";
const TASKS_TABLE = process.env.TASKS_TABLE;
const slug = () => process.env.PORTAL_SLUG; // read per request (testable; env is static in prod)

// The owner's slug opens everything: pages, the cabinet, the mail, the task queue. A form goes to
// people outside the chat, so its link carries a second slug made from the owner's — an HMAC, so the
// owner slug can't be read back out of it — under which only pages/forms/ serves and only its
// submits land. manage_storage makes the same slug for the link it returns.
export const formSlugOf = (portalSlug) => createHmac("sha256", portalSlug).update("forms").digest("hex").slice(0, 32);
const formSlug = () => (slug() ? formSlugOf(slug()) : "");

const PAGES = "pages/";
const FORMS = "forms/";      // under PAGES: what the form slug serves
const SUBMISSIONS = "submissions/";
const MARKER_KEY = "state/portal.version";

// ─── pure helpers (exported for tests) ───

export const CONTENT_TYPES = {
  html: "text/html; charset=utf-8", css: "text/css", js: "text/javascript", mjs: "text/javascript",
  json: "application/json", txt: "text/plain; charset=utf-8", md: "text/plain; charset=utf-8",
  csv: "text/csv", svg: "image/svg+xml", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
  gif: "image/gif", webp: "image/webp", ico: "image/x-icon", pdf: "application/pdf",
};
const BINARY = new Set(["png", "jpg", "jpeg", "gif", "webp", "ico", "pdf"]);

export function contentTypeFor(key) {
  const ext = (key.split(".").pop() || "").toLowerCase();
  return { type: CONTENT_TYPES[ext] || "application/octet-stream", binary: BINARY.has(ext) || !CONTENT_TYPES[ext] };
}

/** /<slug>/rest → {slug, rest} with traversal rejected; null = malformed. */
export function parsePath(rawPath) {
  const segs = (rawPath || "/").split("/").slice(1); // drop the leading empty
  if (!segs.length || !segs[0]) return null;
  const slug = segs[0];
  let rest = segs.slice(1).join("/");
  if (rest === "" ) rest = "index.html";
  if (rest.endsWith("/")) rest += "index.html";
  if (rest.split("/").some((s) => s === "" || s === "." || s === "..")) return null;
  return { slug, rest };
}

export function isFullDoc(html) {
  return /^\s*(<!doctype|<html)/i.test(html);
}

function shellScript(slug) {
  return `<script>
(() => {
  const base = ${JSON.stringify(`/${slug}`)};
  window.ui = { data: (route) => fetch(base + "/data/" + route, { cache: "no-store" }).then((r) => r.json()) };
  let v0 = null, dirty = false;
  document.addEventListener("input", () => { dirty = true; }, true);
  async function check() {
    try {
      const v = await (await fetch(base + "/version", { cache: "no-store" })).text();
      if (v0 === null) { v0 = v; return; }
      if (v !== v0 && !dirty) location.reload();
    } catch {}
  }
  check();
  setInterval(() => { if (document.visibilityState === "visible") check(); }, 5000);
  addEventListener("focus", check);
})();
</script>`;
}

/** The shell: physics only — data helper + version-poll refresh. Never looks.
 *  A body fragment gets the full wrap (incl. <base> so nested pages resolve links from the
 *  portal root); a full document gets the script SPLICED into its head — agents told "make it
 *  look nice" write full documents, and portal.data must exist either way. */
export function wrapShell(body, slug) {
  if (isFullDoc(body)) {
    const m = body.match(/<head[^>]*>/i);
    if (m) return body.replace(m[0], m[0] + shellScript(slug));
    const h = body.match(/<html[^>]*>/i);
    if (h) return body.replace(h[0], h[0] + shellScript(slug));
    return shellScript(slug) + body;
  }
  return `<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><base href="/${slug}/">
${shellScript(slug)}
</head>
<body>
${body}
</body></html>`;
}

/** Form body → fields object. urlencoded and json (multipart parses via parseMultipart). */
export function parseForm(contentType, body) {
  const ct = (contentType || "").split(";")[0].trim().toLowerCase();
  if (ct === "application/json") return JSON.parse(body);
  if (ct === "application/x-www-form-urlencoded") {
    return Object.fromEntries(new URLSearchParams(body));
  }
  return null;
}

/** multipart/form-data → {fields, files: [{field, filename, contentType, data}]}.
 *  Minimal RFC-7578 parse over the raw Buffer: split on the boundary, read each part's
 *  headers, treat parts with a filename as files and the rest as text fields. */
export function parseMultipart(contentType, buf) {
  const m = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType || "");
  if (!m) return null;
  const boundary = Buffer.from(`--${(m[1] || m[2]).trim()}`);
  const fields = {}, files = [];
  let pos = buf.indexOf(boundary);
  while (pos !== -1) {
    const next = buf.indexOf(boundary, pos + boundary.length);
    if (next === -1) break; // the closing "--" tail follows the last boundary
    let part = buf.subarray(pos + boundary.length, next);
    if (part.subarray(0, 2).toString() === "\r\n") part = part.subarray(2);
    const headerEnd = part.indexOf("\r\n\r\n");
    if (headerEnd !== -1) {
      const headers = part.subarray(0, headerEnd).toString();
      const data = part.subarray(headerEnd + 4, part.length - 2); // strip the trailing \r\n
      const cd = /content-disposition:.*?name="([^"]*)"(?:.*?filename="([^"]*)")?/is.exec(headers);
      if (cd) {
        const [, name, filename] = cd;
        if (filename !== undefined) {
          const ctm = /content-type:\s*([^\r\n]+)/i.exec(headers);
          if (filename) files.push({ field: name, filename, contentType: ctm ? ctm[1].trim() : "application/octet-stream", data });
        } else {
          fields[name] = data.toString("utf8");
        }
      }
    }
    pos = next;
  }
  return { fields, files };
}

/** A submitted filename, made safe as one path segment. */
export function safeName(filename) {
  const base = (filename || "file").split(/[\\/]/).pop();
  return base.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 128) || "file";
}

export const KIND_RE = /^[a-z0-9_-]{1,64}$/;

// ─── responses ───

const notFound = () => ({ statusCode: 404, headers: { "content-type": "text/plain" }, body: "not found" });
const text = (statusCode, type, body) => ({ statusCode, headers: { "content-type": type, "cache-control": "no-store" }, body });

// ─── http routes ───

async function serve(rest, servedSlug = slug()) {
  let obj;
  try {
    obj = await s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: PAGES + rest }));
  } catch (e) {
    if (e.name === "NoSuchKey" || e.name === "AccessDenied") return notFound();
    throw e;
  }
  const { type, binary } = contentTypeFor(rest);
  if (binary) {
    const bytes = await obj.Body.transformToByteArray();
    return {
      statusCode: 200, headers: { "content-type": type, "cache-control": "no-store" },
      body: Buffer.from(bytes).toString("base64"), isBase64Encoded: true,
    };
  }
  let body = await obj.Body.transformToString();
  if (type.startsWith("text/html")) body = wrapShell(body, servedSlug);
  return text(200, type, body);
}

async function version() {
  try {
    const obj = await s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: MARKER_KEY }));
    return text(200, "text/plain", await obj.Body.transformToString());
  } catch (e) {
    // no ListBucket on the role, so a missing marker surfaces as AccessDenied, not NoSuchKey
    if (e.name === "NoSuchKey" || e.name === "AccessDenied") return text(200, "text/plain", "0");
    throw e;
  }
}

async function dataTasks() {
  // The open queue — the same read manage_tasks query serves the agent (open-tasks-index is sparse:
  // open + has a due_date). Soonest due first.
  const r = await ddb.send(new QueryCommand({
    TableName: TASKS_TABLE, IndexName: "open-tasks-index",
    KeyConditionExpression: "open_flag = :o", ExpressionAttributeValues: { ":o": "1" },
  }));
  return text(200, "application/json", JSON.stringify({ tasks: r.Items || [] }));
}

const DATA_ROUTES = { tasks: dataTasks };

async function submit(kind, event) {
  if (!KIND_RE.test(kind)) return notFound();
  const contentType = event.headers?.["content-type"] || "";
  const rawBuf = Buffer.from(event.body || "", event.isBase64Encoded ? "base64" : "utf8");

  let fields = null, parts = [];
  try {
    if (contentType.toLowerCase().startsWith("multipart/form-data")) {
      const mp = parseMultipart(contentType, rawBuf);
      if (mp) ({ fields, files: parts } = mp);
    } else {
      fields = parseForm(contentType, rawBuf.toString("utf8"));
    }
  } catch {
    fields = null;
  }
  if (!fields || typeof fields !== "object") return text(415, "text/plain", "unsupported form body");

  const submitted_at = Date.now();
  const stem = `${submitted_at}-${Math.random().toString(36).slice(2, 10)}`;
  const key = `${SUBMISSIONS}${kind}/${stem}.json`;
  const files = [];
  for (const p of parts) {
    const fkey = `${SUBMISSIONS}${kind}/${stem}/${safeName(p.filename)}`;
    await s3.send(new PutObjectCommand({
      Bucket: BUCKET, Key: fkey, ContentType: p.contentType, Body: p.data,
    }));
    files.push({ field: p.field, key: fkey, size: p.data.length });
  }
  await s3.send(new PutObjectCommand({
    Bucket: BUCKET, Key: key, ContentType: "application/json",
    Body: JSON.stringify({ kind, fields, files, submitted_at, source: "portal" }),
  }));
  // caption so manage_storage find surfaces it like any filed doc
  await s3.send(new PutObjectAnnotationCommand({
    Bucket: BUCKET, Key: key, AnnotationName: "caption",
    AnnotationPayload: new TextEncoder().encode(JSON.stringify({
      title: `form ${kind}`, occurred_at: new Date(submitted_at).toISOString().slice(0, 10), tags: ["portal"],
    })),
  }));

  const referer = event.headers?.referer;
  if (referer) return { statusCode: 303, headers: { location: referer, "cache-control": "no-store" }, body: "" };
  return text(200, "text/html; charset=utf-8", "<!doctype html><html><body>submitted — u can close this tab</body></html>");
}

// ─── ddb stream branch: bump the marker ───

async function bump() {
  await s3.send(new PutObjectCommand({
    Bucket: BUCKET, Key: MARKER_KEY, ContentType: "text/plain", Body: String(Date.now()),
  }));
}

// ─── handler ───

/* ── /s3 — the generic read surface ───────────────────────────────────────────
 *
 * The browser has no AWS credentials, so a page cannot call S3 itself. This is the one
 * hop that can, and it is deliberately GENERIC: list a prefix, get a key. Everything an
 * agent-written page wants to show — the inbox, filed documents, outputs, submissions —
 * comes from these two, so a new kind of page needs no change here.
 *
 *   GET /<slug>/s3?prefix=in/&bucket=email     → {objects:[{key,size,modified,caption}]}
 *   GET /<slug>/s3?key=in/abc123&bucket=email  → the object itself
 *
 * List and get differ and are not merged: a listing is metadata as JSON (plus the caption
 * annotation, which is where a document's title and note live), while a get is the bytes
 * with their own content type — an html document, a pdf, a raw .eml. Collapsing them would
 * mean base64 in JSON for every binary.
 *
 *   DELETE /<slug>/s3?key=…                    remove it, versions and all
 *
 * It is the firm's bucket and they can delete their own things. Delete means delete: on the
 * versioned cabinet a plain DeleteObject only writes a delete marker, so the bytes stay billed
 * forever while looking gone. This lists the key's versions and removes them in one batch, so
 * what disappears from the listing is actually gone.
 */
const LIST_MAX = 1000;

function bucketFor(q) {
  if ((q.bucket || "") === "email") return EMAIL_BUCKET || null;
  return BUCKET;
}

async function s3list(bucket, prefix) {
  const out = await s3.send(new ListObjectsV2Command({
    Bucket: bucket, Prefix: prefix, MaxKeys: LIST_MAX,
  }));
  const objects = await Promise.all((out.Contents || []).map(async (o) => {
    const row = { key: o.Key, size: o.Size, modified: o.LastModified?.toISOString() };
    if (bucket === BUCKET) {
      // the caption is an S3 annotation, so a listing shows titles and notes rather than keys
      try {
        const a = await s3.send(new GetObjectAnnotationCommand({
          Bucket: bucket, Key: o.Key, AnnotationName: "caption",
        }));
        row.caption = JSON.parse(await a.AnnotationPayload.transformToString());
      } catch { /* no caption filed; the key is all there is */ }
    }
    return row;
  }));
  return text(200, "application/json", JSON.stringify({
    prefix, objects, truncated: Boolean(out.IsTruncated),
  }));
}

async function s3get(bucket, key) {
  const r = await s3.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
  const ext = key.split(".").pop()?.toLowerCase() || "";
  const type = r.ContentType || CONTENT_TYPES[ext] || "application/octet-stream";
  if (BINARY.has(ext)) {
    const bytes = await r.Body.transformToByteArray();
    return {
      statusCode: 200, headers: { "content-type": type, "cache-control": "no-store" },
      body: Buffer.from(bytes).toString("base64"), isBase64Encoded: true,
    };
  }
  return text(200, type, await r.Body.transformToString());
}

async function s3delete(q) {
  const bucket = bucketFor(q);
  const key = q.key || "";
  if (!bucket || !key || key.includes("..")) return notFound();
  try {
    // Prefix matches partially, so filter to the exact key — deleting "invoice.pdf" must not
    // take "invoice.pdf.bak" with it.
    const v = await s3.send(new ListObjectVersionsCommand({ Bucket: bucket, Prefix: key }));
    const objects = [...(v.Versions || []), ...(v.DeleteMarkers || [])]
      .filter((o) => o.Key === key)
      .map((o) => ({ Key: o.Key, VersionId: o.VersionId }));

    if (objects.length) {
      await s3.send(new DeleteObjectsCommand({ Bucket: bucket, Delete: { Objects: objects } }));
    } else {
      await s3.send(new DeleteObjectCommand({ Bucket: bucket, Key: key })); // unversioned bucket
    }
    await bump();                                       // open pages notice it went
    return text(200, "application/json", JSON.stringify({ deleted: key, versions: objects.length }));
  } catch {
    return notFound();
  }
}

async function s3read(q) {
  const bucket = bucketFor(q);
  if (!bucket) return notFound();                       // email asked for, front door is off
  const key = q.key || "";
  const prefix = q.prefix || "";
  if (key.includes("..") || prefix.includes("..")) return notFound();
  try {
    return key ? await s3get(bucket, key) : await s3list(bucket, prefix);
  } catch {
    return notFound();                                  // missing key, denied prefix — same 404
  }
}

export const handler = async (event) => {
  if (event.Records) {                                  // tasks-table stream batch → one bump
    try { await bump(); return { batchItemFailures: [] }; }
    catch (e) {
      // the whole batch is one bump, so the whole batch is reported: the mapping retries it
      // alone and parks it when the marker write keeps failing
      console.error(JSON.stringify({ msg: "portal bump failed", gerp_id: process.env.GERP_ID || "",
        function: process.env.AWS_LAMBDA_FUNCTION_NAME || "", error: String(e) }));
      return { batchItemFailures: event.Records.map((r) => ({ itemIdentifier: r.dynamodb?.SequenceNumber || r.eventID })) };
    }
  }

  const method = event.requestContext?.http?.method || "GET";
  const parsed = parsePath(event.rawPath);
  if (!parsed) return notFound();
  if (formSlug() && parsed.slug === formSlug()) {
    // the form slug: the pages under pages/forms/, their submits and the change marker — nothing of
    // the cabinet, the mail or the data routes
    const { rest } = parsed;
    if (method === "POST" && rest.startsWith("f/")) return submit(rest.slice(2), event);
    if (method !== "GET" || rest === "s3" || rest.startsWith("data/")) return notFound();
    if (rest === "version") return version();
    return serve(FORMS + rest, parsed.slug);
  }
  if (parsed.slug !== slug()) return notFound(); // wrong slug = same 404 as a missing page

  const { rest } = parsed;
  if (method === "POST" && rest.startsWith("f/")) return submit(rest.slice(2), event);
  if (method === "DELETE" && rest === "s3") return s3delete(event.queryStringParameters || {});
  if (method !== "GET") return notFound();
  if (rest === "version") return version();
  if (rest === "s3") return s3read(event.queryStringParameters || {});
  if (rest.startsWith("data/")) {
    const route = DATA_ROUTES[rest.slice(5)];
    return route ? route() : notFound();
  }
  return serve(rest);
};
