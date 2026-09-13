// Pure-logic tests for storage/manage_storage (no AWS) — the caption assembly, the find filter,
// and op routing. The S3 / annotation calls want a live integ test against the applied bucket
// (put/get + copy-carries already proven via the CLI smoke test). Run: `bash scripts/test.sh --module storage`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { matches, captionFrom, handler, clients } from "../../../modules/storage/lambdas/manage_storage/index.mjs";

test("captionFrom keeps only the caption fields", () => {
  assert.deepEqual(
    captionFrom({ op: "file", key: "x", title: "Camera", note: "receipt", tags: ["cam"], occurred_at: "2026-05-01", junk: "no" }),
    { title: "Camera", note: "receipt", tags: ["cam"], occurred_at: "2026-05-01" },
  );
});

test("matches filters by query / tag / occurred_at range", () => {
  const cap = { title: "camera purchase", note: "best buy", tags: ["camera", "electronics"], occurred_at: "2026-05-01" };
  assert.equal(matches(cap, "camera purchase", null, null, null), true, "text query");
  assert.equal(matches(cap, "camera", "electronics", "2026-04-01", "2026-06-01"), true, "query + tag + window");
  assert.equal(matches(cap, "tripod", null, null, null), false, "non-matching query rejected");
  assert.equal(matches(cap, null, "food", null, null), false, "non-matching tag rejected");
  assert.equal(matches(cap, null, null, "2026-06-01", null), false, "after-window excludes earlier occurred_at");
  assert.equal(matches(cap, null, null, null, "2026-04-01"), false, "before-window excludes later occurred_at");
});

test("matches passes an empty caption when no filter is given (plain prefix list)", () => {
  assert.equal(matches({}, null, null, null, null), true);
});

test("handler routes an unknown op to 400", async () => {
  const r = await handler({ op: "bogus" });
  assert.equal(r.statusCode, 400);
});

test("put rejects missing / empty / oversized content before any AWS call", async () => {
  let r = await handler({ op: "put", key: "compliance/_index.md" });
  assert.equal(r.statusCode, 400);
  assert.match(JSON.parse(r.body).error, /content/);

  r = await handler({ op: "put", key: "compliance/_index.md", content: "" });
  assert.equal(r.statusCode, 400);

  r = await handler({ op: "put", key: "big.md", content: "x".repeat(256 * 1024 + 1) });
  assert.equal(r.statusCode, 400);
  assert.match(JSON.parse(r.body).error, /file blobs via op=file/);
});

// ---- read is a window: max_bytes from offset, size + next_offset on the reply ----
function fakeS3(bytes) {
  const calls = [];
  return {
    calls,
    async send(cmd) {
      calls.push(cmd);
      const name = cmd.constructor.name;
      if (name === "HeadObjectCommand") return { ContentLength: bytes.length, ContentType: "text/plain" };
      if (name === "GetObjectCommand") {
        const m = /^bytes=(\d+)-(\d+)$/.exec(cmd.input.Range || "");
        const slice = m ? bytes.subarray(Number(m[1]), Number(m[2]) + 1) : bytes;
        return { Body: { transformToString: async () => Buffer.from(slice).toString("utf8") } };
      }
      throw new Error(`unexpected ${name}`);
    },
  };
}

test("read hands back a 16KB window by default, and says where the next one starts", async () => {
  const doc = Buffer.from("x".repeat(40 * 1024));
  const real = clients.s3; clients.s3 = fakeS3(doc);
  try {
    const r = JSON.parse((await handler({ op: "read", key: "docs/big.txt" })).body);
    assert.equal(r.content.length, 16 * 1024);
    assert.equal(r.size, 40 * 1024);
    assert.equal(r.truncated, true);
    assert.equal(r.next_offset, 16 * 1024);
    const get = clients.s3.calls.find((c) => c.constructor.name === "GetObjectCommand");
    assert.equal(get.input.Range, "bytes=0-16383", "only the window crosses the wire");

    const last = JSON.parse((await handler({ op: "read", key: "docs/big.txt", offset: r.next_offset, max_bytes: 100 * 1024 })).body);
    assert.equal(last.content.length, 24 * 1024);
    assert.equal(last.truncated, false);
    assert.equal(last.next_offset, null);

    const past = JSON.parse((await handler({ op: "read", key: "docs/big.txt", offset: 50 * 1024 })).body);
    assert.equal(past.content, "");
    assert.equal(past.truncated, false);
  } finally { clients.s3 = real; }
});

test("a put under pages/forms/ returns the form link beside the owner's", async () => {
  const { formSlugOf } = await import("../../../modules/storage/lambdas/manage_storage/index.mjs");
  process.env.PORTAL_URL = "https://fn.lambda-url.test/ownerslug";
  const sent = [];
  const saved = clients.s3.send;
  clients.s3.send = async (cmd) => { sent.push(cmd); return {}; };
  try {
    const r = await handler({ op: "put", key: "pages/forms/log_hours.html", content: "<form method=post action=\"f/log_hours\"></form>" });
    const body = JSON.parse(r.body);
    assert.equal(body.url, "https://fn.lambda-url.test/ownerslug/forms/log_hours.html");
    assert.equal(body.form_url, `https://fn.lambda-url.test/${formSlugOf("ownerslug")}/log_hours.html`);
    assert.ok(!body.form_url.includes("ownerslug"), "the form link carries no owner slug");
    const page = JSON.parse((await handler({ op: "put", key: "pages/tasks.html", content: "<p>x</p>" })).body);
    assert.equal(page.form_url, undefined, "a plain page gets no form link");
  } finally {
    clients.s3.send = saved;
  }
});
