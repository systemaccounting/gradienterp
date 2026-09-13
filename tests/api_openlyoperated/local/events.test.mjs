// The SSE relay: a channel under /oob or nothing, every event framed with id/event/data, a heartbeat,
// and the relay ends with `: reconnect` at its deadline.
import { test } from "node:test";
import assert from "node:assert/strict";

process.env.EVENTS_HTTP_HOST = "abc.appsync-api.us-east-1.amazonaws.com";
process.env.EVENTS_REALTIME_HOST = "abc.appsync-realtime-api.us-east-1.amazonaws.com";
process.env.EVENTS_KEY = "da2-public";
globalThis.awslambda = { streamifyResponse: f => f, HttpResponseStream: { from: (s) => s } };
const mod = await import("../../../prod/api_openlyoperated/api/v1/events/handler.mjs");

test("a channel is /oob/counters or /oob/<gerp>/<kind>, kinds with dots and underscores as dashes", () => {
  assert.equal(mod.channelOf({ channel: "/oob/counters" }), "/oob/counters");
  assert.equal(mod.channelOf({ channel: "oob/cafe/journal_entry.posted" }), "/oob/cafe/journal-entry-posted");
  assert.equal(mod.channelOf({ channel: "/jobs/x" }), null);
  assert.equal(mod.channelOf({ channel: "/oob" }), null);
  assert.equal(mod.channelOf({ channel: "/oob/a/b/c" }), null);
  assert.equal(mod.channelOf({}), null);
});

test("the relay subscribes with the key, frames every event, heartbeats and ends at the deadline", async () => {
  let now = 1000;
  mod.deps.now = () => now;
  const sent = [];
  let socket;
  mod.deps.WebSocket = (url, protocols) => {
    assert.equal(url, "wss://abc.appsync-realtime-api.us-east-1.amazonaws.com/event/realtime");
    assert.equal(protocols[0], "aws-appsync-event-ws");
    const auth = JSON.parse(Buffer.from(protocols[1].slice("header-".length).replace(/-/g, "+").replace(/_/g, "/"), "base64").toString());
    assert.deepEqual(auth, { host: "abc.appsync-api.us-east-1.amazonaws.com", "x-api-key": "da2-public" });
    socket = { send: m => sent.push(JSON.parse(m)), close() { this.closed = true; } };
    queueMicrotask(() => socket.onopen());
    return socket;
  };
  const out = [];
  const done = mod.relay("/oob/counters", s => out.push(s), { seconds: 1 });
  await new Promise(r => setTimeout(r, 5));
  assert.deepEqual(sent[0], { type: "connection_init" });
  socket.onmessage({ data: JSON.stringify({ type: "connection_ack", connectionTimeoutMs: 300000 }) });
  assert.equal(sent[1].type, "subscribe");
  assert.equal(sent[1].channel, "/oob/counters");
  assert.equal(sent[1].authorization["x-api-key"], "da2-public");
  socket.onmessage({ data: JSON.stringify({ type: "subscribe_success", id: sent[1].id }) });
  now = 2000;
  socket.onmessage({ data: JSON.stringify({ type: "data", id: sent[1].id, event: JSON.stringify({ key: "revenue", op: "add", magnitude: 40, period: "2026-09" }) }) });
  socket.onmessage({ data: JSON.stringify({ type: "ka" }) });
  await done;
  const text = out.join("");
  assert.ok(text.startsWith(": subscribed /oob/counters\n\n"));
  assert.ok(text.includes('id: 2000\nevent: /oob/counters\ndata: {"key":"revenue","op":"add","magnitude":40,"period":"2026-09"}\n\n'));
  assert.ok(text.endsWith(": reconnect\n\n"), "the relay tells the client to come back");
  assert.ok(socket.closed, "the socket is closed when the relay ends");
});

test("the handler refuses a channel outside /oob before any socket", async () => {
  let opened = false;
  mod.deps.WebSocket = () => { opened = true; return {}; };
  const chunks = []; const stream = { write: c => chunks.push(c), end() { this.ended = true; } };
  await mod.handler({ queryStringParameters: { channel: "/private/x" } }, stream);
  assert.ok(!opened && stream.ended);
  assert.match(chunks.join(""), /channel is \/oob\/counters/);
});
