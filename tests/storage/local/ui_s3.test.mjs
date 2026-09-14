// The portal's /s3 object routes: a get answers 302 to a presigned URL on the bucket's own endpoint,
// so an object never renders on the portal's origin; a delete refuses a retained document the way
// manage_storage does. The S3 client and the presigner are stood in through the `clients` holder.
import { test } from "node:test";
import assert from "node:assert/strict";

process.env.STORAGE_BUCKET = "cabinet";
process.env.PORTAL_SLUG = "ownerslug";
const ui = await import("../../../modules/storage/lambdas/ui/index.mjs");

function stub({ missing = false, caption = null } = {}) {
  const sent = [];
  ui.clients.s3 = {
    send: async (cmd) => {
      const name = cmd.constructor.name;
      sent.push([name, cmd.input]);
      if (name === "HeadObjectCommand" && missing) throw Object.assign(new Error("NotFound"), { name: "NotFound" });
      if (name === "GetObjectAnnotationCommand") {
        if (!caption) throw new Error("no annotation");
        return { AnnotationPayload: { transformToString: async () => JSON.stringify(caption) } };
      }
      if (name === "ListObjectVersionsCommand") return { Versions: [{ Key: cmd.input.Prefix, VersionId: "v1" }], DeleteMarkers: [] };
      return {};
    },
  };
  const signed = [];
  ui.clients.presign = async (client, cmd, opts) => {
    signed.push([cmd.constructor.name, cmd.input, opts]);
    return `https://cabinet.s3.us-east-1.amazonaws.com/${cmd.input.Key}?X-Amz-Signature=abc`;
  };
  return { sent, signed };
}

const call = (method, q) => ui.handler({ rawPath: "/ownerslug/s3", queryStringParameters: q, requestContext: { http: { method } } });

test("a get answers 302 to a presigned URL on S3, never the bytes", async () => {
  const { sent, signed } = stub();
  const r = await call("GET", { key: "submissions/receipt/1-a/evil.html" });
  assert.equal(r.statusCode, 302);
  assert.match(r.headers.location, /^https:\/\/cabinet\.s3\.us-east-1\.amazonaws\.com\//);
  assert.equal(r.body, "");
  assert.deepEqual(sent.map(([n]) => n), ["HeadObjectCommand"], "the bytes never pass through the lambda");
  const [[name, input, opts]] = signed;
  assert.equal(name, "GetObjectCommand");
  assert.equal(input.Key, "submissions/receipt/1-a/evil.html");
  assert.equal(input.ResponseContentType, ui.CONTENT_TYPES.html);
  assert.equal(opts.expiresIn, ui.GET_URL_SECONDS);
});

test("a missing key is 404 and nothing is signed", async () => {
  const { signed } = stub({ missing: true });
  assert.equal((await call("GET", { key: "nope.pdf" })).statusCode, 404);
  assert.deepEqual(signed, []);
});

test("a retained document refuses the portal's delete and keeps its versions", async () => {
  const { sent } = stub({ caption: { title: "2025 return", retention: "retained" } });
  const r = await call("DELETE", { key: "filed/tax/2025.pdf" });
  assert.equal(r.statusCode, 409);
  assert.deepEqual(JSON.parse(r.body), { status: "refused", reason: "retained document", key: "filed/tax/2025.pdf" });
  assert.ok(!sent.some(([n]) => n === "DeleteObjectsCommand" || n === "DeleteObjectCommand"));
});

test("an unretained document is deleted, every version", async () => {
  const { sent } = stub({ caption: { title: "draft" } });
  const r = await call("DELETE", { key: "outputs/draft.txt" });
  assert.equal(r.statusCode, 200);
  const [, input] = sent.find(([n]) => n === "DeleteObjectsCommand");
  assert.deepEqual(input.Delete.Objects, [{ Key: "outputs/draft.txt", VersionId: "v1" }]);
});

test("every portal response carries the security headers, and the CSP leaves agent scripts alone", async () => {
  stub();
  for (const r of [await call("GET", { key: "a.pdf" }), await call("GET", { key: "gone.pdf" }),
                   await ui.handler({ rawPath: "/wrongslug/x.html", requestContext: { http: { method: "GET" } } })]) {
    assert.equal(r.headers["referrer-policy"], "no-referrer", "the slug stays out of Referer");
    assert.equal(r.headers["x-content-type-options"], "nosniff");
    assert.match(r.headers["content-security-policy"], /frame-ancestors 'none'/);
    assert.doesNotMatch(r.headers["content-security-policy"], /script-src|default-src/, "agent pages keep their inline scripts");
    assert.ok(r.headers["strict-transport-security"] && r.headers["cross-origin-opener-policy"]);
  }
});
