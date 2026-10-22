#!/usr/bin/env node
// The Node lambdas, locally. `_image.py` runs a Python handler by importing its main.py; this runs
// an index.mjs the same way: the function's snapshotted env applied, the handler imported from
// source, called with the event the deployed function receives. scripts/local-dev.sh starts one
// per Function URL:
//
//   node tests/server/node_lambda.mjs --port 8082 --front modules/agent/lambdas/chat
//
// Two doors on the port:
//   POST /invoke/<function name, or the last segment of its src dir>
//        the body is the event; the answer is what the handler returned, or for a streaming
//        handler what it wrote, as {statusCode, headers, body}. dev/tools.py sends a Node tool's
//        event here, and `_image.py` a Node function's
//   everything else — the --front function's Function URL: the request becomes a payload-format-2.0
//        event, and what the handler writes streams back, status and headers included
//
// Env: the function's snapshotted env is applied before its import, and a value the shell set wins
// over it. That is how AWS_ENDPOINT_URL, COGNITO_ISSUER and an emptied MEMORY_ID arrive. A handler
// is re-imported when its file changes, so an edit shows on the next request.

import { randomUUID } from "node:crypto";
import { existsSync, readFileSync, statSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const argv = process.argv.slice(2);
const arg = (name, dflt) => { const i = argv.indexOf(`--${name}`); return i >= 0 ? argv[i + 1] : dflt; };
const PORT = Number(arg("port", 8082));
const IMAGE = path.resolve(REPO, arg("image", "tests/server/per_customer/image.json"));
const image = JSON.parse(readFileSync(IMAGE, "utf8"));

// the Node functions in the image: a src dir holding an index.mjs
const functions = Object.entries(image.functions)
  .filter(([, cfg]) => cfg.src_dir && existsSync(path.resolve(REPO, cfg.src_dir, "index.mjs")))
  .map(([name, cfg]) => ({ name, src: cfg.src_dir, file: path.resolve(REPO, cfg.src_dir, "index.mjs"), env: cfg.env || {} }));
const byKey = new Map();
for (const f of functions) { byKey.set(f.name, f); byKey.set(f.src, f); byKey.set(path.basename(f.src), f); }
const FRONT = arg("front", "");
const front = FRONT ? byKey.get(FRONT) : null;
if (FRONT && !front) { console.error(`[node] no Node function ${FRONT} in ${IMAGE}`); process.exit(1); }

// the shell's env wins over the snapshot's
const shell = new Set(Object.keys(process.env));
function applyEnv(fn) {
  for (const [k, v] of Object.entries(fn.env)) if (!shell.has(k)) process.env[k] = v;
}

// what the streaming-enabled Lambda runtime provides: `streamifyResponse` wraps an
// (event, responseStream) handler, `HttpResponseStream.from` puts the status and headers on the stream
globalThis.awslambda = {
  streamifyResponse: (fn) => fn,
  HttpResponseStream: { from(stream, meta) { stream.open(meta?.statusCode || 200, meta?.headers || {}); return stream; } },
};

const loaded = new Map();   // name → {mtime, handler}
async function handlerOf(fn) {
  const mtime = statSync(fn.file).mtimeMs;
  const have = loaded.get(fn.name);
  if (have && have.mtime === mtime) return have.handler;
  applyEnv(fn);
  const mod = await import(`${pathToFileURL(fn.file).href}?v=${mtime}`);
  loaded.set(fn.name, { mtime, handler: mod.handler });
  return mod.handler;
}

// The handler's second argument. A streaming handler writes to it; a plain one reads it as the
// context. One object serves both, so neither handler shape has to be told apart.
function context(fn) {
  return { awsRequestId: randomUUID(), functionName: fn.name, getRemainingTimeInMillis: () => 60_000 };
}
function streamTo(res, fn) {              // the front: written through to the browser
  return {
    ...context(fn), started: false,
    open(status, headers) { this.started = true; res.writeHead(status, headers); },
    write(chunk) { res.write(chunk); },
    end(chunk) { res.end(chunk); },
  };
}
function collector(fn) {                  // /invoke: collected, then answered as the lambda's result
  const chunks = [];
  return {
    ...context(fn), started: false, status: 200, headers: {},
    open(status, headers) { this.started = true; this.status = status; this.headers = headers; },
    write(chunk) { chunks.push(Buffer.from(chunk)); },
    end(chunk) { if (chunk) chunks.push(Buffer.from(chunk)); },
    result() { return { statusCode: this.status, headers: this.headers, body: Buffer.concat(chunks).toString("utf8") }; },
  };
}

const readBody = (req) => new Promise((resolve) => {
  const parts = [];
  req.on("data", (c) => parts.push(c));
  req.on("end", () => resolve(Buffer.concat(parts)));
});

// the payload-format-2.0 event a Function URL hands its function
function urlEvent(req, body) {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const headers = { ...req.headers };
  const event = {
    version: "2.0", routeKey: "$default", rawPath: url.pathname, rawQueryString: url.search.slice(1), headers,
    requestContext: {
      http: { method: req.method, path: url.pathname, protocol: "HTTP/1.1",
              sourceIp: req.socket.remoteAddress || "", userAgent: headers["user-agent"] || "" },
      domainName: headers.host || "", requestId: randomUUID(), timeEpoch: Date.now(),
    },
    isBase64Encoded: false,
  };
  if (url.search.length > 1) event.queryStringParameters = Object.fromEntries(url.searchParams);
  if (headers.cookie) event.cookies = headers.cookie.split(/;\s*/);
  if (body.length) event.body = body.toString("utf8");
  return event;
}

function sendJson(res, status, obj) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(obj));
}

// a lambda-shaped result → the response the Function URL would send
function sendResult(res, r) {
  if (r === undefined || r === null) return sendJson(res, 200, null);
  if (typeof r !== "object" || !("statusCode" in r)) return sendJson(res, 200, r);
  const headers = { ...(r.headers || {}) };
  if (r.cookies) headers["set-cookie"] = r.cookies;
  res.writeHead(r.statusCode || 200, headers);
  return res.end(r.isBase64Encoded ? Buffer.from(r.body || "", "base64") : (r.body ?? ""));
}

const server = http.createServer(async (req, res) => {
  const body = await readBody(req);
  const t0 = Date.now();
  const log = (fn, status) => console.log(`[node] ${fn ? fn.name : "-"} ${req.method} ${req.url} ${status} ${Date.now() - t0}ms`);
  let fn = null;
  try {
    const m = req.url.match(/^\/invoke\/([^/?]+)/);
    if (m) {
      fn = byKey.get(decodeURIComponent(m[1]));
      if (!fn) { sendJson(res, 404, { error: `no Node function ${m[1]}`, known: functions.map((f) => f.name) }); return log(null, 404); }
      const event = body.length ? JSON.parse(body.toString("utf8")) : {};
      const out = collector(fn);
      const r = await (await handlerOf(fn))(event, out);
      sendJson(res, 200, out.started ? out.result() : (r === undefined ? null : r));
      return log(fn, 200);
    }
    if (!front) { sendJson(res, 404, { error: "no --front function", invoke: functions.map((f) => `/invoke/${f.name}`) }); return log(null, 404); }
    fn = front;
    const out = streamTo(res, fn);
    const r = await (await handlerOf(fn))(urlEvent(req, body), out);
    if (!out.started) sendResult(res, r);
    return log(fn, res.statusCode);
  } catch (e) {
    console.error(`[node] ${fn ? fn.name : "-"} ${req.method} ${req.url} failed:`, e);
    if (!res.headersSent) sendJson(res, 500, { error: `${e?.name || "Error"}: ${e?.message || e}` });
    else res.end();
    return log(fn, 500);
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[node] :${PORT} · ${functions.length} Node functions: ${functions.map((f) => f.name).join(", ")}`
    + (front ? ` · front ${front.name} (${front.src})` : ""));
});
