// Pure-logic tests for storage/ui (the web-serving lambda) (no AWS) — path parse + slug gate, the shell wrap,
// content typing, and form-body parsing. Serving / submissions / the stream bump want the
// live smoke against the applied stack. Run: `bash scripts/test.sh --module storage`.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  parsePath, wrapShell, isFullDoc, parseForm, parseMultipart, safeName, contentTypeFor,
  KIND_RE, handler,
} from "../../../modules/storage/lambdas/ui/index.mjs";

test("parsePath splits slug from rest and defaults index.html", () => {
  assert.deepEqual(parsePath("/abc123/tasks.html"), { slug: "abc123", rest: "tasks.html" });
  assert.deepEqual(parsePath("/abc123"), { slug: "abc123", rest: "index.html" });
  assert.deepEqual(parsePath("/abc123/"), { slug: "abc123", rest: "index.html" });
  assert.deepEqual(parsePath("/abc123/onboarding/"), { slug: "abc123", rest: "onboarding/index.html" });
  assert.deepEqual(parsePath("/abc123/data/tasks"), { slug: "abc123", rest: "data/tasks" });
});

test("parsePath rejects traversal and malformed paths", () => {
  assert.equal(parsePath("/abc123/../secrets"), null);
  assert.equal(parsePath("/abc123/a/../../b"), null);
  assert.equal(parsePath("/abc123//double"), null);
  assert.equal(parsePath("/abc123/./x"), null);
  assert.equal(parsePath("/"), null);
  assert.equal(parsePath(""), null);
});

test("wrapShell wraps a body fragment with base, data helper and version poll", () => {
  const out = wrapShell("<h1>tasks</h1>", "abc123");
  assert.match(out, /^<!doctype html>/);
  assert.match(out, /<base href="\/abc123\/">/);
  assert.match(out, /<h1>tasks<\/h1>/);
  assert.match(out, /ui = \{ data:/);
  assert.match(out, /\/abc123/);
  assert.match(out, /version/);
  assert.match(out, /visibilityState/);
});

test("wrapShell splices the script into a full document's head", () => {
  const doc = '<!doctype html><html><head><title>mine</title></head><body>mine</body></html>';
  const out = wrapShell(doc, "abc123");
  assert.match(out, /<head><script>/, "script lands right after <head>");
  assert.match(out, /ui = \{ data:/);
  assert.match(out, /<title>mine<\/title>/, "author's head intact");
  assert.doesNotMatch(out, /<base /, "no base forced on a full doc");

  const headless = "<html><body>x</body></html>";
  assert.match(wrapShell(headless, "abc123"), /<html><script>/, "falls back to after <html>");
  assert.equal(isFullDoc("  <HTML>"), true);
  assert.equal(isFullDoc("<h1>frag</h1>"), false);
});

test("contentTypeFor types and binary flags", () => {
  assert.deepEqual(contentTypeFor("tasks.html"), { type: "text/html; charset=utf-8", binary: false });
  assert.equal(contentTypeFor("style.css").type, "text/css");
  assert.equal(contentTypeFor("logo.png").binary, true);
  assert.equal(contentTypeFor("weird.bin").binary, true, "unknown ext treated binary");
});

test("parseForm handles urlencoded and json, rejects others", () => {
  assert.deepEqual(parseForm("application/x-www-form-urlencoded", "task_id=t-1&hours=2.5"), { task_id: "t-1", hours: "2.5" });
  assert.deepEqual(parseForm("application/json; charset=utf-8", '{"a":1}'), { a: 1 });
  assert.equal(parseForm("multipart/form-data; boundary=x", "..."), null);
  assert.equal(parseForm(undefined, "x=1"), null);
});

test("parseMultipart splits fields and files", () => {
  const b = "xYzBoundary";
  const body = Buffer.from([
    `--${b}\r\ncontent-disposition: form-data; name="worker"\r\n\r\nsam\r\n`,
    `--${b}\r\ncontent-disposition: form-data; name="doc"; filename="w4 2026.pdf"\r\n`,
    `content-type: application/pdf\r\n\r\nPDFBYTES\u0000\u0001\r\n`,
    `--${b}--\r\n`,
  ].join(""), "latin1");
  const out = parseMultipart(`multipart/form-data; boundary=${b}`, body);
  assert.deepEqual(out.fields, { worker: "sam" });
  assert.equal(out.files.length, 1);
  assert.equal(out.files[0].field, "doc");
  assert.equal(out.files[0].filename, "w4 2026.pdf");
  assert.equal(out.files[0].contentType, "application/pdf");
  assert.equal(out.files[0].data.toString("latin1"), "PDFBYTES\u0000\u0001", "binary bytes intact");
});

test("parseMultipart tolerates empty file inputs and quoted boundaries", () => {
  const b = "q";
  const body = Buffer.from(
    `--${b}\r\ncontent-disposition: form-data; name="doc"; filename=""\r\n\r\n\r\n--${b}--\r\n`);
  const out = parseMultipart(`multipart/form-data; boundary="${b}"`, body);
  assert.deepEqual(out.files, [], "an empty-filename part (no file chosen) is skipped");
  assert.equal(parseMultipart("multipart/form-data", Buffer.from("")), null, "no boundary → null");
});

test("safeName flattens paths and strange chars", () => {
  assert.equal(safeName("w4 2026.pdf"), "w4_2026.pdf");
  assert.equal(safeName("../../etc/passwd"), "passwd");
  assert.equal(safeName("C:\\docs\\id.png"), "id.png");
  assert.equal(safeName(""), "file");
});

test("kind names are constrained", () => {
  assert.equal(KIND_RE.test("log_hours"), true);
  assert.equal(KIND_RE.test("Log Hours"), false);
  assert.equal(KIND_RE.test("a/b"), false);
});

test("handler 404s a wrong slug before any AWS call", async () => {
  process.env.PORTAL_SLUG = "rightslug";
  const r = await handler({ rawPath: "/wrongslug/tasks.html", requestContext: { http: { method: "GET" } } });
  assert.equal(r.statusCode, 404);
});

test("handler 404s non-GET non-form methods", async () => {
  process.env.PORTAL_SLUG = "rightslug";
  const r = await handler({ rawPath: "/rightslug/tasks.html", requestContext: { http: { method: "DELETE" } } });
  assert.equal(r.statusCode, 404);
});

test("the form slug is an HMAC of the owner slug, shared with manage_storage", async () => {
  const { formSlugOf } = await import("../../../modules/storage/lambdas/ui/index.mjs");
  const ms = await import("../../../modules/storage/lambdas/manage_storage/index.mjs");
  assert.equal(formSlugOf("abc123"), "a48eea8d2a7dc9360f23e63bbae90556");
  assert.equal(ms.formSlugOf("abc123"), formSlugOf("abc123"));
  assert.notEqual(formSlugOf("abc123"), "abc123");
});

test("the form slug reaches forms and their submits, and 404s the cabinet, the mail and the data", async () => {
  process.env.PORTAL_SLUG = "ownerslug";
  const { formSlugOf } = await import("../../../modules/storage/lambdas/ui/index.mjs");
  const fs = formSlugOf("ownerslug");
  const call = (method, path, q) => handler({ rawPath: path, queryStringParameters: q, requestContext: { http: { method } } });
  for (const [method, path, q] of [["GET", `/${fs}/s3`, { prefix: "" }], ["DELETE", `/${fs}/s3`, { key: "pages/index.html" }],
                                   ["GET", `/${fs}/s3`, { prefix: "in/", bucket: "email" }], ["GET", `/${fs}/data/tasks`],
                                   ["PUT", `/${fs}/forms/x.html`]]) {
    assert.equal((await call(method, path, q)).statusCode, 404, `${method} ${path}`);
  }
  // the owner slug is not the form slug, and a form slug for another owner slug opens nothing
  assert.equal((await call("GET", `/${formSlugOf("someoneelse")}/log_hours.html`)).statusCode, 404);
});
