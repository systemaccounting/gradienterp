// GET /gerps/{gerp_id}/sources            — a gerp's own catalog, the same list the directory carries
// GET /gerps/{gerp_id}/sources/{source}   — the read-through: the gerp's own /oob/<source>, byte for byte
//
// The api adds no shape: resolve the gerp through the operator's row (`published`, `gateway_url`),
// fetch the gerp's own read with the query string passed through, and stream the body on as it
// arrives — the gerp's 404 (unpublished at the source) and 429 (its own throttle) are the
// consumer's. Node because streaming is native here (`streamifyResponse`); API Gateway's
// integration is STREAM, so a 20 MB file is an ordinary resource.
const CUSTOMERS_TABLE = process.env.CUSTOMERS_TABLE || "gerp-customers";
const PUBLIC_BASE = process.env.PUBLIC_BASE || "https://api.openlyoperated.biz/v1";
const PASS_HEADERS = ["content-type", "cache-control", "etag", "last-modified"];

export const deps = {
  ddb: null,
  fetch: (...a) => globalThis.fetch(...a),
};

// the SDK is the runtime's (nodejs22.x ships v3); loaded on first use so a test with a fake client never needs it
async function ddb() {
  if (deps.ddb) return deps.ddb;
  const { DynamoDBClient, GetItemCommand } = await import("@aws-sdk/client-dynamodb");
  const client = new DynamoDBClient({});
  deps.ddb = { send: cmd => client.send(new GetItemCommand(cmd.input)) };
  return deps.ddb;
}

export async function resolveGerp(gerpId) {
  const out = await (await ddb()).send({ input: { TableName: CUSTOMERS_TABLE, Key: { gerp_id: { S: gerpId } },
    ProjectionExpression: "#s, gateway_url, published", ExpressionAttributeNames: { "#s": "status" } } });
  const it = out.Item || {};
  const status = it.status?.S || "";
  const base = it.gateway_url?.S || "";
  const published = it.published?.BOOL;
  if (!base || status !== "active" || published === false) return null;
  return { gerpId, base, published };   // published undefined = a row from before the stamp: the source decides
}

// what to fetch for a request, or an error the caller answers with
export function plan(event) {
  const p = event.pathParameters || {};
  const gerpId = p.gerp_id || "";
  const source = p.source || "";
  if (!gerpId) return { error: { status: 400, body: { error: "gerp_id required" } } };
  if (source && !/^[a-z0-9_-]+$/i.test(source)) return { error: { status: 400, body: { error: "source is a catalog key" } } };
  const qs = new URLSearchParams();
  for (const [k, vs] of Object.entries(event.multiValueQueryStringParameters || {})) for (const v of vs || []) qs.append(k, v);
  const query = qs.toString();
  return { gerpId, source, path: source ? `/oob/${source}` : "/oob", query };
}

export function catalogRows(body) {
  return (body.sources || []).map(s => ({ key: (s.path || "").split("/").pop() || s.label || "", kind: s.kind || "", label: s.label || "" }));
}

// the whole read as a plain object — the streaming wrapper below writes it out; tests call this
export async function read(event) {
  const p = plan(event);
  if (p.error) return { status: p.error.status, headers: { "content-type": "application/json" }, body: JSON.stringify(p.error.body) };
  const gerp = await resolveGerp(p.gerpId);
  if (!gerp) return { status: 404, headers: { "content-type": "application/json" }, body: JSON.stringify({ error: "no such published gerp" }) };
  const url = `${gerp.base}${p.path}${p.query ? "?" + p.query : ""}`;
  // the source builds the curl it names in its own body from this: the api's url, not its host
  const publicUrl = `${PUBLIC_BASE}/gerps/${p.gerpId}/sources${p.source ? "/" + p.source : ""}`;
  const r = await deps.fetch(url, { headers: { accept: "application/json", "x-public-url": publicUrl } });
  const headers = {};
  for (const h of PASS_HEADERS) if (r.headers.get(h)) headers[h] = r.headers.get(h);
  if (!p.source) {
    if (r.status !== 200) return { status: r.status, headers: { "content-type": "application/json" }, body: JSON.stringify({ error: r.status === 404 ? "not published" : "the gerp did not answer" }) };
    return { status: 200, headers: { "content-type": "application/json" }, body: JSON.stringify({ gerp_id: p.gerpId, sources: catalogRows(await r.json()) }) };
  }
  return { status: r.status, headers, stream: r.body };   // byte for byte, as it arrives
}

async function pump(readable, responseStream) {
  const reader = readable.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    responseStream.write(value);
  }
}

export const handler = awslambda.streamifyResponse(async (event, responseStream) => {
  let out;
  try { out = await read(event); }
  catch (e) { out = { status: 502, headers: { "content-type": "application/json" }, body: JSON.stringify({ error: `the gerp did not answer: ${e.message}` }) }; }
  // any origin may read: the page is one browser among the consumers
  const stream = awslambda.HttpResponseStream.from(responseStream, { statusCode: out.status, headers: { ...out.headers, "access-control-allow-origin": "*" } });
  if (out.stream) await pump(out.stream, stream); else stream.write(out.body);
  stream.end();
});
