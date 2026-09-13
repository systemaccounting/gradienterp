// The read-through: resolve the gerp off the operator's row, fetch its own /oob read with the query
// string passed through, hand the body on byte for byte with the gerp's own status.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.awslambda = { streamifyResponse: f => f, HttpResponseStream: { from: (s) => s } };
const mod = await import("../../../prod/api_openlyoperated/api/v1/gerps_sources/handler.mjs");

const rows = {
  cafe: { Item: { status: { S: "active" }, gateway_url: { S: "https://gw.example" }, published: { BOOL: true } } },
  quiet: { Item: { status: { S: "active" }, gateway_url: { S: "https://quiet.example" } } },   // no stamp
  shut: { Item: { status: { S: "active" }, gateway_url: { S: "https://shut.example" }, published: { BOOL: false } } },
  building: { Item: { status: { S: "provisioning" } } },
};
mod.deps.ddb = { send: async cmd => rows[cmd.input.Key.gerp_id.S] || {} };

const calls = [];
const respond = (status, body, type = "application/json") => ({
  status, headers: { get: h => (h === "content-type" ? type : h === "cache-control" ? "public, max-age=60" : null) },
  json: async () => JSON.parse(body), body: new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(body)); c.close(); } }),
});
const sentHeaders = [];
mod.deps.fetch = async (url, init) => {
  calls.push(url); sentHeaders.push(init?.headers || {});
  if (url.startsWith("https://quiet.example")) return respond(404, '{"error":"not published"}');
  if (url.endsWith("/oob")) return respond(200, JSON.stringify({ sources: [{ kind: "ledger", label: "financials", path: "/oob/financials" }, { kind: "metrics", label: "metrics", path: "/oob/metrics" }] }));
  if (url.includes("/oob/financials")) return respond(200, '{"revenue": 38351.0, "trend": [1,2,3]}');
  return respond(429, '{"error":"slow down"}');
};

async function drain(stream) {
  const reader = stream.getReader(); let s = "";
  for (;;) { const { done, value } = await reader.read(); if (done) break; s += new TextDecoder().decode(value); }
  return s;
}

test("the catalog comes back as rows, and the read-through streams the gerp's bytes with its status", async () => {
  const cat = await mod.read({ pathParameters: { gerp_id: "cafe" } });
  assert.equal(cat.status, 200);
  assert.deepEqual(JSON.parse(cat.body), { gerp_id: "cafe", sources: [{ key: "financials", kind: "ledger", label: "financials" }, { key: "metrics", kind: "metrics", label: "metrics" }] });
  const fin = await mod.read({ pathParameters: { gerp_id: "cafe", source: "financials" }, multiValueQueryStringParameters: { window: ["30"], profile: ["p1"] } });
  assert.equal(fin.status, 200);
  assert.equal(fin.headers["content-type"], "application/json");
  assert.equal(await drain(fin.stream), '{"revenue": 38351.0, "trend": [1,2,3]}');
  assert.ok(calls.some(u => u === "https://gw.example/oob/financials?window=30&profile=p1"), "the query string passes through untouched");
  assert.ok(sentHeaders.some(h => h["x-public-url"] === "https://api.openlyoperated.biz/v1/gerps/cafe/sources/financials"), "the source learns its public url");
  const slow = await mod.read({ pathParameters: { gerp_id: "cafe", source: "inventory" } });
  assert.equal(slow.status, 429, "the gerp's own throttle is the consumer's");
});

test("an unpublished, unknown, or unvended gerp is 404 before any fetch; a stampless row is asked at the source", async () => {
  calls.length = 0;
  for (const g of ["shut", "building", "ghost"]) {
    const out = await mod.read({ pathParameters: { gerp_id: g, source: "financials" } });
    assert.equal(out.status, 404, g);
  }
  assert.equal(calls.length, 0, "no fetch for a gerp the row rules out");
  const quiet = await mod.read({ pathParameters: { gerp_id: "quiet" } });
  assert.equal(quiet.status, 404);
  assert.equal(JSON.parse(quiet.body).error, "not published");
  assert.equal((await mod.read({ pathParameters: { gerp_id: "cafe", source: "../etc" } })).status, 400);
});
