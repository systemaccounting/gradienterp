// GET /events?channel=/oob/counters | /oob/<gerp_id>/<kind> — the stream as server-sent events.
//
// Subscribes to the Events API on the consumer's behalf over WebSocket (the api's public key) and
// relays every event as SSE: `id:` a timestamp, `event:` the channel, `data:` the event JSON, a
// comment heartbeat every 15 s, and the relay ends short of the streaming ceiling so the client
// reconnects. SSE is the stream contract: plain HTTP, any client. The route is keyed on the api
// (x-api-key), since a held lambda is a cost a request throttle cannot see.
const HTTP_HOST = process.env.EVENTS_HTTP_HOST || "";          // the Events API's HTTP host — what the key signs for
const REALTIME_HOST = process.env.EVENTS_REALTIME_HOST || "";  // its WebSocket host
const EVENTS_KEY = process.env.EVENTS_KEY || "";               // the public subscribe key
const RELAY_SECONDS = Number(process.env.RELAY_SECONDS || 840);
const HEARTBEAT_MS = 15000;

export const deps = { WebSocket: (...a) => new globalThis.WebSocket(...a), now: () => Date.now() };

export function channelOf(query) {
  const raw = (query.channel || "").trim();
  if (!raw) return null;
  // a channel is /oob/counters or /oob/<gerp_id>/<kind>; kinds are written with dots and
  // underscores, the channel carries them as dashes
  const segs = raw.replace(/^\/+/, "").split("/").map(s => s.replace(/[^A-Za-z0-9-]/g, "-").replace(/^-+|-+$/g, "").slice(0, 50));
  if (segs[0] !== "oob" || segs.length < 2 || segs.length > 3 || segs.some(s => !s)) return null;
  return "/" + segs.join("/");
}

const b64url = s => Buffer.from(s).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export function frame(id, channel, data) {
  return `id: ${id}\nevent: ${channel}\ndata: ${JSON.stringify(data)}\n\n`;
}

// relay until the deadline; `write` takes text. Resolves when done, rejects on a connection fault.
export function relay(channel, write, { seconds = RELAY_SECONDS } = {}) {
  return new Promise((resolve, reject) => {
    const auth = { host: HTTP_HOST, "x-api-key": EVENTS_KEY };
    const ws = deps.WebSocket(`wss://${REALTIME_HOST}/event/realtime`, ["aws-appsync-event-ws", `header-${b64url(JSON.stringify(auth))}`]);
    const id = "s" + deps.now().toString(36);
    const started = deps.now();
    let beat = null, stop = null, done = false;
    const finish = (err) => {
      if (done) return; done = true;
      clearInterval(beat); clearTimeout(stop);
      try { ws.close(); } catch {}
      err ? reject(err) : resolve();
    };
    ws.onopen = () => { ws.send(JSON.stringify({ type: "connection_init" })); };
    ws.onmessage = (m) => {
      let msg; try { msg = JSON.parse(m.data); } catch { return; }
      if (msg.type === "connection_ack") {
        ws.send(JSON.stringify({ type: "subscribe", id, channel, authorization: auth }));
      } else if (msg.type === "subscribe_success") {
        write(`: subscribed ${channel}\n\n`);
        beat = setInterval(() => write(`: ${new Date(deps.now()).toISOString()}\n\n`), HEARTBEAT_MS);
        stop = setTimeout(() => { write(`: reconnect\n\n`); finish(); }, Math.max(1000, seconds * 1000 - (deps.now() - started)));
      } else if (msg.type === "data") {
        const events = Array.isArray(msg.event) ? msg.event : [msg.event];
        for (const e of events) { let d; try { d = JSON.parse(e); } catch { d = e; } write(frame(deps.now(), channel, d)); }
      } else if (msg.type === "subscribe_error" || msg.type === "connection_error") {
        finish(new Error(JSON.stringify(msg.errors || msg)));
      }
    };
    ws.onerror = (e) => finish(new Error(e?.message || "websocket error"));
    ws.onclose = () => finish();
  });
}

export const handler = awslambda.streamifyResponse(async (event, responseStream) => {
  const channel = channelOf(event.queryStringParameters || {});
  if (!channel) {
    const s = awslambda.HttpResponseStream.from(responseStream, { statusCode: 400, headers: { "content-type": "application/json" } });
    s.write(JSON.stringify({ error: "channel is /oob/counters or /oob/<gerp_id>/<kind>" })); s.end(); return;
  }
  const s = awslambda.HttpResponseStream.from(responseStream, { statusCode: 200, headers: { "content-type": "text/event-stream", "cache-control": "no-cache", "x-accel-buffering": "no", "access-control-allow-origin": "*" } });
  try { await relay(channel, chunk => s.write(chunk)); }
  catch (e) { s.write(`event: error\ndata: ${JSON.stringify({ error: e.message })}\n\n`); }
  s.end();
});
