/**
 * chat — the gerp's own web front door to its agent (Node, native response streaming).
 *
 * Node lambda because Lambda streams natively here (`awslambda.streamifyResponse` +
 * a RESPONSE_STREAM Function URL) — no Web Adapter, no polling side-channel. The chat
 * is the agent's human front door; it lives in modules/agent and reaches the runtime
 * same-account. The Function URL is public, so THIS lambda is the auth gate: it
 * validates the operator-pool JWT itself (native `crypto`, no deps) and resolves role
 * from this gerp's own records (owner via SSM stash, contact via a scan on account_id).
 *
 * Wire protocol to the browser: newline-delimited JSON chunks, each
 *   {type:"session"|"status"|"text"|"form"|"done", ...}
 * The runtime's SSE `data:` frames are relayed VERBATIM (no per-type filtering), so a new
 * chunk type is a runtime+client change, not a relay change. `frame` ({type,spec,interrupt_id})
 * is collect_secret elicitation: the agent paused for the secure field; the client renders it and
 * POSTs {frame_submit:{interrupt_id,tool,args,values_key,values}}. `chat()` invokes manage_secret
 * (the ONLY sink — tagged agent_frame_sink, IAM-gated) with the value and resumes the runtime with
 * only the outcome {ok,status,error} — so the secret never passes through the agent/Memory.
 * Works buffered today (one `text` chunk after the turn) and streams unchanged once the
 * container emits incremental `text` + `status` chunks (Phase 2).
 */

import crypto from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { DynamoDBClient, QueryCommand, GetItemCommand, UpdateItemCommand, DeleteItemCommand } from "@aws-sdk/client-dynamodb";
import { SSMClient, GetParameterCommand } from "@aws-sdk/client-ssm";
import { BedrockAgentCoreClient, InvokeAgentRuntimeCommand, ListEventsCommand, DeleteEventCommand } from "@aws-sdk/client-bedrock-agentcore";
import { LambdaClient, InvokeCommand } from "@aws-sdk/client-lambda";
import { ResourceGroupsTaggingAPIClient, GetResourcesCommand } from "@aws-sdk/client-resource-groups-tagging-api";
import { S3Client, ListObjectsV2Command, DeleteObjectsCommand } from "@aws-sdk/client-s3";

const REGION = process.env.AWS_REGION || "us-east-1";
const POOL = process.env.COGNITO_USER_POOL_ID || "";
const CLIENT_ID = process.env.COGNITO_CLIENT_ID || "";
const DOMAIN_PREFIX = process.env.COGNITO_DOMAIN_PREFIX || "";
const CONTACTS_TABLE = process.env.CONTACTS_TABLE || "";
const OWNER_SUB_PARAM = process.env.OWNER_SUB_PARAM || "";
const CHATS_TABLE = process.env.CHATS_TABLE || "";              // per-user saved-chat index (durable; survives Memory retention)
const MEMORY_ID = process.env.MEMORY_ID || "";                  // AgentCore Memory resource holding the conversation events
const MEMORY_ACTOR_ID = process.env.MEMORY_ACTOR_ID || "agent-session"; // must match the container's AgentCoreMemoryStore.ACTOR_ID
const SESSIONS_BUCKET = process.env.SESSIONS_BUCKET || "";      // S3SessionManager bucket — the agent's authoritative session + checkpoint
const SESSIONS_PREFIX = process.env.SESSIONS_PREFIX || "agent-sessions/"; // must match the container's SESSIONS_PREFIX default
const BUSINESS_NAME = process.env.BUSINESS_NAME || "";          // the gerp's business name — header shows it instead of the login email
const ISSUER = POOL ? `https://cognito-idp.${REGION}.amazonaws.com/${POOL}` : "";
const CLOCK_LEEWAY = 60;

// invoke_agent_runtime wants the RUNTIME arn + the endpoint name as `qualifier`, not
// the full runtime-endpoint arn (which defaults qualifier=DEFAULT and 404s).
const EP_ARN = process.env.AGENT_RUNTIME_ENDPOINT_ARN || "";
let RUNTIME_ARN = EP_ARN, QUALIFIER = "DEFAULT";
if (EP_ARN.includes("/runtime-endpoint/")) [RUNTIME_ARN, QUALIFIER] = EP_ARN.split("/runtime-endpoint/");

const HTML = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "index.html"), "utf8");

const ddb = new DynamoDBClient({});
const ssm = new SSMClient({});
const agentcore = new BedrockAgentCoreClient({});
const lambda = new LambdaClient({});
const tagging = new ResourceGroupsTaggingAPIClient({});
const s3 = new S3Client({});

// form sinks: lambdas tagged agent_frame_sink=true (manage_secret is the only one) invoked with
// user-entered form values the agent never sees. Resolved at runtime (the sinks deploy AFTER this
// module — agent owns the gateway they register onto) by one cached tag query; the tag also gates
// the invoke via IAM. Map is {tool_name -> arn}; cached for the container lifetime.
let _frameSinks = null;
export async function frameSinkArns(client = tagging) {
  if (_frameSinks) return _frameSinks;
  const arns = [];
  let token;
  do {
    const r = await client.send(new GetResourcesCommand({
      ResourceTypeFilters: ["lambda:function"],
      TagFilters: [{ Key: "agent_frame_sink", Values: ["true"] }],
      PaginationToken: token,
    }));
    for (const m of r.ResourceTagMappingList || []) if (m.ResourceARN) arns.push(m.ResourceARN);
    token = r.PaginationToken;
  } while (token);
  if (arns.length) _frameSinks = arns;   // an empty answer is asked again next time, never kept for the container's life
  return arns;
}

// Resolve a form target tool -> its sink lambda ARN. Function names are
// <stack>-<module>-<gerp>-<tool>, so the tool is the unique `-<tool>` suffix. Returns null if the
// tool isn't a tagged sink (the agent tried to route values somewhere not allowlisted).
//
// A sink that's ALSO a gateway tool would reach the form under its
// AgentCore-namespaced name `<target>___<tool>` (hyphenated target); the sink is the bare tool. Take
// the segment after the last `___` — a no-op for a sink that arrives bare (manage_secret).
export async function resolveSink(tool, client = tagging) {
  const bare = String(tool || "").split("___").pop();
  if (!/^[a-zA-Z0-9_]+$/.test(bare)) return null;
  return (await frameSinkArns(client)).find((a) => a.endsWith("-" + bare)) || null;
}

// Set the user's collected values into the tool's input at `values_key` (dotted path; merges into
// any object the agent already put there), or shallow-merge at top level when no key. Pure.
// A path segment that names an object's prototype would write onto every object in the container.
const UNSAFE_KEYS = new Set(["__proto__", "prototype", "constructor"]);

export function buildToolInput(args, valuesKey, values) {
  const input = JSON.parse(JSON.stringify(args && typeof args === "object" ? args : {}));
  const vals = values && typeof values === "object" ? values : {};
  if (!valuesKey) return { ...input, ...vals };
  const parts = String(valuesKey).split(".");
  if (parts.some((k) => UNSAFE_KEYS.has(k))) throw new Error("values_key names a prototype");
  let cur = input;
  for (let i = 0; i < parts.length - 1; i++) cur = cur[parts[i]] = cur[parts[i]] || {};
  cur[parts[parts.length - 1]] = { ...(cur[parts[parts.length - 1]] || {}), ...vals };
  return input;
}

// ── jwt verification (native crypto) ──────────────────────────────────────────

let _jwks = null;
async function loadJwks(force) {
  if (_jwks && !force) return _jwks;
  const r = await fetch(`${ISSUER}/.well-known/jwks.json`);
  const data = await r.json();
  _jwks = Object.fromEntries((data.keys || []).map((k) => [k.kid, k]));
  return _jwks;
}

const b64urlJson = (s) => JSON.parse(Buffer.from(s, "base64url").toString("utf8"));

export async function verifyJwt(token) {
  if (!ISSUER) throw new Error("auth not configured");
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("malformed token");
  const [h, p, sig] = parts;
  if (b64urlJson(h).alg !== "RS256") throw new Error("unexpected alg"); // reject none/HS256 alg-confusion
  const kid = b64urlJson(h).kid;
  // own properties only: a kid like "__proto__" must not find something the object inherited
  const own = (jwks) => (Object.hasOwn(jwks, kid) ? jwks[kid] : undefined);
  const jwk = own(await loadJwks()) || own(await loadJwks(true)); // keys may have rotated
  if (!jwk) throw new Error("unknown signing key");
  const key = crypto.createPublicKey({ key: jwk, format: "jwk" });
  if (!crypto.verify("RSA-SHA256", Buffer.from(`${h}.${p}`), key, Buffer.from(sig, "base64url")))
    throw new Error("bad signature");
  const claims = b64urlJson(p);
  const now = Math.floor(Date.now() / 1000);
  if ((claims.exp || 0) < now - CLOCK_LEEWAY) throw new Error("token expired");
  if (claims.iss !== ISSUER) throw new Error("bad issuer");
  if (claims.token_use === "id") {
    if (CLIENT_ID && claims.aud !== CLIENT_ID) throw new Error("bad audience");
  } else if (claims.token_use === "access") {
    if (CLIENT_ID && claims.client_id !== CLIENT_ID) throw new Error("bad client_id");
  } else throw new Error("bad token_use");
  return claims;
}

// ── role resolution (this gerp's own records, same-account) ───────────────────

let _ownerSub = null;
async function ownerSub() {
  if (_ownerSub) return _ownerSub;
  if (!OWNER_SUB_PARAM) return process.env.LOCAL_OWNER_SUB || null;
  try {
    const r = await ssm.send(new GetParameterCommand({ Name: OWNER_SUB_PARAM }));
    if (r.Parameter?.Value) _ownerSub = r.Parameter.Value;
  } catch { /* not stashed yet → no owner */ }
  return _ownerSub;
}

async function contactRole(accountId) {
  if (!CONTACTS_TABLE) return null;
  // Query the sparse account-index (public profile id → contact) — one round-trip, no Scan.
  // A person's profile is keyed by their account sub, so the caller's JWT subject IS the id.
  const r = await ddb.send(new QueryCommand({
    TableName: CONTACTS_TABLE,
    IndexName: "account-index",
    KeyConditionExpression: "#pid = :a",
    ExpressionAttributeNames: { "#pid": "gerp_profile_id" },
    ExpressionAttributeValues: { ":a": { S: accountId } },
    Limit: 1,
  }));
  const it = (r.Items || [])[0];
  if (!it) return null;
  const flag = (n) => it[n]?.BOOL === true;
  if (flag("is_employee")) return "employee";
  if (flag("is_customer")) return "customer";
  if (flag("is_vendor")) return "vendor";
  return "contact";
}

export async function resolveRole(accountId) {
  if (!accountId) return null;
  if (accountId === (await ownerSub())) return "owner";
  return contactRole(accountId);
}

// ── oauth code exchange (server-side; public client, no secret/PKCE/CORS) ─────

export async function exchangeCode(code, redirectUri) {
  if (!DOMAIN_PREFIX || !CLIENT_ID) return null;
  const r = await fetch(`https://${DOMAIN_PREFIX}.auth.${REGION}.amazoncognito.com/oauth2/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", client_id: CLIENT_ID, code, redirect_uri: redirectUri }),
  });
  if (!r.ok) return null;
  const d = await r.json();
  return d.id_token ? { id_token: d.id_token, refresh_token: d.refresh_token || "" } : null;
}

export function servePage(injectedToken, injectedRefresh = "") {
  const cfg = JSON.stringify({ issuer: ISSUER, clientId: CLIENT_ID, domainPrefix: DOMAIN_PREFIX, region: REGION, injectedToken: injectedToken || "", injectedRefresh: injectedRefresh || "", businessName: BUSINESS_NAME });
  return HTML.replace("{{CONFIG}}", cfg);
}

// ── saved chats (durable per-user index + Memory-backed replay) ────────────────
//
// The chat history itself lives in AgentCore Memory (events per session). This table
// is just the per-user INDEX — {account_id → session_id, title, updated_at} — so we can
// list a user's chats, sort by recency, title them, and keep the list even after Memory's
// retention window expires the underlying events. It's also the access-control gate:
// loadHistory only reads Memory for a session this user owns a row for.

async function authClaims(headers) {
  const auth = headers.authorization || "";
  if (!auth.toLowerCase().startsWith("bearer ")) return { error: 401, message: "missing bearer token" };
  try { return { claims: await verifyJwt(auth.slice(7).trim()) }; }
  catch (e) { return { error: 401, message: `auth failed: ${e.message}` }; }
}

async function listChats(accountId) {
  if (!CHATS_TABLE) return [];
  const r = await ddb.send(new QueryCommand({
    TableName: CHATS_TABLE,
    KeyConditionExpression: "account_id = :a",
    ExpressionAttributeValues: { ":a": { S: accountId } },
  }));
  return (r.Items || [])
    .map((it) => ({ session_id: it.session_id.S, title: it.title?.S || "untitled", updated_at: Number(it.updated_at?.N || 0) }))
    .sort((a, b) => b.updated_at - a.updated_at);
}

// Upsert on each turn: bump updated_at always; set title + created_at only the first time
// (if_not_exists), deriving the title from the opening message. Best-effort — a failed
// index write must not break the chat response.
// Whether this account's chat index holds the session. Off Lambda with no chats table (the local
// stack, the tests) there is no index to check against.
export async function ownsSession(accountId, sessionId) {
  if (!CHATS_TABLE) return true;
  const got = await ddb.send(new GetItemCommand({
    TableName: CHATS_TABLE, Key: { account_id: { S: accountId }, session_id: { S: sessionId } },
  }));
  return Boolean(got.Item);
}

async function touchChat(accountId, sessionId, message) {
  if (!CHATS_TABLE) return;
  const now = String(Date.now());
  const title = (message.split("\n")[0] || "untitled").slice(0, 80);
  try {
    await ddb.send(new UpdateItemCommand({
      TableName: CHATS_TABLE,
      Key: { account_id: { S: accountId }, session_id: { S: sessionId } },
      UpdateExpression: "SET updated_at = :now, title = if_not_exists(title, :t), created_at = if_not_exists(created_at, :now)",
      ExpressionAttributeValues: { ":now": { N: now }, ":t": { S: title } },
    }));
  } catch (e) { console.log("touchChat failed:", e?.name); }
}

// Purge every object S3SessionManager wrote for this session — the agent's AUTHORITATIVE
// history + interrupt checkpoint, which (unlike Memory) hold the full message content. The
// container keys sessions at `${prefix}session_${session_id}/…`, with session_id passed
// through verbatim by AgentCore, so the row's session_id is an exact folder prefix. No
// per-session delete API — list under the prefix, batch-delete (≤1000/call), paginate.
async function deleteSession(sessionId) {
  if (!SESSIONS_BUCKET || !sessionId) return;
  const prefix = `${SESSIONS_PREFIX}session_${sessionId}/`;
  try {
    let token;
    do {
      const r = await s3.send(new ListObjectsV2Command({ Bucket: SESSIONS_BUCKET, Prefix: prefix, ContinuationToken: token }));
      const objs = (r.Contents || []).map((o) => ({ Key: o.Key }));
      if (objs.length) {
        await s3.send(new DeleteObjectsCommand({ Bucket: SESSIONS_BUCKET, Delete: { Objects: objs, Quiet: true } }));
      }
      token = r.IsTruncated ? r.NextContinuationToken : undefined;
    } while (token);
  } catch (e) { console.log("deleteSession s3 purge failed:", e?.name); }
}

// Hard delete: drop the index row AND purge BOTH stores — the Memory transcript events and the
// S3 session — so "delete" means gone now, not hidden until the retention TTL. Ownership-gated:
// Memory events sit under a fixed actorId, so without this check a caller could pass someone
// else's session_id and erase their history. Best-effort purge (no DeleteSession API for either
// store — list ids/keys, then delete each); the row is removed regardless, and the TTL backstops
// any partial purge.
async function deleteChat(accountId, sessionId) {
  if (!CHATS_TABLE || !sessionId) return;
  const owned = await ddb.send(new GetItemCommand({
    TableName: CHATS_TABLE, Key: { account_id: { S: accountId }, session_id: { S: sessionId } },
  }));
  if (!owned.Item) return;                                       // not this caller's chat → no-op

  try {
    await ddb.send(new DeleteItemCommand({
      TableName: CHATS_TABLE, Key: { account_id: { S: accountId }, session_id: { S: sessionId } },
    }));
  } catch (e) { console.log("deleteChat row failed:", e?.name); }

  await deleteSession(sessionId);        // purge the S3 session (full history + checkpoint)

  if (!MEMORY_ID) return;
  try {
    const ids = [];
    let nextToken;
    do {                                                         // collect all event ids first (don't mutate while paginating)
      const r = await agentcore.send(new ListEventsCommand({
        memoryId: MEMORY_ID, sessionId, actorId: MEMORY_ACTOR_ID, includePayloads: false, maxResults: 100, nextToken,
      }));
      for (const ev of r.events || []) if (ev.eventId) ids.push(ev.eventId);
      nextToken = r.nextToken;
    } while (nextToken);
    for (const eventId of ids) {
      await agentcore.send(new DeleteEventCommand({ memoryId: MEMORY_ID, sessionId, actorId: MEMORY_ACTOR_ID, eventId }));
    }
  } catch (e) { console.log("deleteChat memory purge failed:", e?.name); }
}

// Replay: confirm the session is this user's (table row), then read its Memory events and
// flatten them into a UI transcript [{role:"user"|"assistant", text} | {role:"tool", name}].
async function loadHistory(accountId, sessionId) {
  if (!CHATS_TABLE || !MEMORY_ID || !sessionId) return null;
  const owned = await ddb.send(new GetItemCommand({
    TableName: CHATS_TABLE, Key: { account_id: { S: accountId }, session_id: { S: sessionId } },
  }));
  if (!owned.Item) return null;                                  // not this user's chat → 404

  const events = [];
  let nextToken;
  do {
    const r = await agentcore.send(new ListEventsCommand({
      memoryId: MEMORY_ID, sessionId, actorId: MEMORY_ACTOR_ID, includePayloads: true, maxResults: 100, nextToken,
    }));
    for (const ev of r.events || []) {
      for (const item of ev.payload || []) {
        if (item.blob == null) continue;
        let msg; try { msg = typeof item.blob === "string" ? JSON.parse(item.blob) : item.blob; } catch { continue; }
        if (msg && msg.role) events.push(msg);
      }
    }
    nextToken = r.nextToken;
  } while (nextToken);
  events.reverse();                                              // list_events is newest-first → chronological
  return toTranscript(events);
}

// Stored messages are Bedrock-Converse-shaped (content = [{text}|{toolUse}|{toolResult}]),
// with some legacy Anthropic-shaped blocks ({type:text|tool_use}). One bubble per message's
// concatenated text; one tool line per tool call; toolResult blocks are intermediate, skipped.
function toTranscript(messages) {
  const out = [];
  const blockText = (b) => (typeof b === "string" ? b : b && b.text != null && b.toolResult == null ? b.text : "");
  // pass the RAW tool name through; the web client's friendly() maps it to a phrase — the same map
  // it applies to live status events, so saved-chat replay and the live trail read identically.
  const toolName  = (b) => (b && b.toolUse ? b.toolUse.name : b && b.type === "tool_use" ? b.name : null);
  for (const m of messages) {
    const content = Array.isArray(m.content) ? m.content : typeof m.content === "string" ? [{ text: m.content }] : [];
    const text = content.map(blockText).join("").trim();
    if (m.role === "user") {
      if (text) out.push({ role: "user", text });
    } else if (m.role === "assistant") {
      for (const b of content) { const n = toolName(b); if (n) out.push({ role: "tool", name: n }); }
      if (text) out.push({ role: "assistant", text });
    }
  }
  return out;
}

// ── handler (streaming) ───────────────────────────────────────────────────────

async function handlerImpl(event, responseStream) {
  const rc = event.requestContext || {};
  const method = rc.http?.method || "GET";
  const path = event.rawPath || rc.http?.path || "/";
  const headers = Object.fromEntries(Object.entries(event.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
  let started = false;
  const open = (statusCode, contentType, extra = {}) => {
    started = true;
    return awslambda.HttpResponseStream.from(responseStream, { statusCode, headers: { "content-type": contentType, ...extra } });
  };
  const finish = (statusCode, obj) => { const s = open(statusCode, "application/json"); s.write(JSON.stringify(obj)); s.end(); };

  try {
    if (method === "GET" && !path.startsWith("/api/")) {
      let token = "", refresh = "";
      const code = (event.queryStringParameters || {}).code;
      if (code) {
        try { const got = await exchangeCode(code, `https://${headers.host}/`); token = got?.id_token || ""; refresh = got?.refresh_token || ""; }
        catch (e) { console.log("code exchange failed:", e?.name); }
      }
      const s = open(200, "text/html; charset=utf-8", { "cache-control": "no-store" });
      s.write(servePage(token, refresh)); s.end();
      return;
    }
    if (path.startsWith("/api/")) {
      // one auth gate for every /api route: validate the JWT (identity) + resolve role (membership)
      const a = await authClaims(headers);
      if (a.error) return finish(a.error, { error: a.message || "unauthorized" });
      const accountId = a.claims.sub;
      const role = await resolveRole(accountId);
      if (role === null) return finish(403, { error: "not a member of this workspace" });
      // The container gives every turn the owner's prompt and tools whatever role arrives, so until
      // a per-role tool gate exists, this door answers the owner only.
      if (role !== "owner") return finish(403, { error: "this chat answers the owner only for now" });
      const sid = (event.queryStringParameters || {}).session_id || "";

      if (method === "POST"   && path === "/api/chat")    return await chat(event, accountId, role, open);
      if (method === "GET"    && path === "/api/chats")   return finish(200, { chats: await listChats(accountId) });
      if (method === "GET"    && path === "/api/history") {
        const msgs = await loadHistory(accountId, sid);
        return msgs === null ? finish(404, { error: "not found" }) : finish(200, { messages: msgs });
      }
      if (method === "DELETE" && path === "/api/chats")   { await deleteChat(accountId, sid); return finish(200, { ok: true }); }
    }
    finish(404, { error: "not found" });
  } catch (e) {
    console.log("handler error:", e);
    if (!started) finish(500, { error: "internal error" });
  }
}

async function chat(event, accountId, role, open) {
  let body = {};
  try {
    let raw = event.body || "{}";
    if (event.isBase64Encoded) raw = Buffer.from(raw, "base64").toString("utf8");
    body = JSON.parse(raw);
  } catch { /* empty */ }
  const message = (body.message || "").trim();
  // A secret-form submission resumes a paused turn: {interrupt_id, tool, args, values_key, values}.
  // It MUST target the existing paused session (a fresh one has no interrupt) — so we never create a
  // session id for it, unlike a normal first message.
  const fs = (body.frame_submit && typeof body.frame_submit === "object") ? body.frame_submit : null;
  if (!message && !fs) return finishJson(open, 400, { error: "message or frame_submit required" });

  let sessionId = body.session_id || "";
  if (fs) {
    if (sessionId.length < 33) return finishJson(open, 400, { error: "frame_submit needs the paused session_id" });
    // a paused turn is resumed only by the account whose chat it is
    if (!(await ownsSession(accountId, sessionId))) return finishJson(open, 404, { error: "no such chat" });
  } else if (sessionId.length < 33 || !(await ownsSession(accountId, sessionId))) {
    // a session id this account doesn't hold starts a new chat: continuing one needs its row
    sessionId = "chat-" + crypto.randomUUID().replace(/-/g, "") + crypto.randomUUID().slice(0, 4);
  }

  // The form values go straight into the sink tool's lambda — they never pass through the agent.
  // We invoke BEFORE opening the stream so a failure is a clean error; then resume the agent with
  // only the outcome (never the values: manage_labor echoes the item, so we send {ok,status,error}).
  let interruptResponse = null;
  if (fs && fs.cancelled) {
    // The user bailed (the [×]): resume the paused agent with a cancellation — no sink call, no values.
    interruptResponse = { interrupt_id: fs.interrupt_id, response: { ok: false, cancelled: true } };
  } else if (fs) {
    const arn = fs.tool === "manage_secret" ? await resolveSink(fs.tool) : null;
    let outcome;
    if (!arn) {
      // Resume the paused turn WITH the failure (not a raw client 400) so the agent notices, tells the
      // owner, and can recover — the same shape as a sink that returns {ok:false}.
      outcome = { ok: false, status: 400, error: `'${fs.tool}' is not a form sink (manage_secret only)` };
    } else {
      try {
        const input = buildToolInput(fs.args, fs.values_key, fs.values);
        const r = await lambda.send(new InvokeCommand({ FunctionName: arn, Payload: JSON.stringify(input) }));
        const out0 = JSON.parse(Buffer.from(r.Payload || []).toString() || "{}");   // {statusCode, body}
        const ok = out0.statusCode === 200;
        let sinkBody = {};
        try { sinkBody = JSON.parse(out0.body || "{}"); } catch { /* non-JSON body */ }
        outcome = { ok, status: out0.statusCode };
        if (!ok) outcome.error = sinkBody.error || "tool failed";
        // Default: the sink's body is DROPPED — manage_labor etc. echo the user's typed input, which must
        // never re-enter the agent/Memory. A sink whose output is DERIVED data the agent needs (not user
        // input) opts in with agent_visible:true, surfaced as `result`. inspect_document is the case:
        // bytes never transit the agent, but its extracted fields do (see modules/storage/AGENTS.md).
        else if (sinkBody.agent_visible) { const { agent_visible, ...result } = sinkBody; outcome.result = result; }
      } catch (e) {
        console.log("sink invoke failed:", e?.name);
        outcome = { ok: false, status: 502, error: `invoke failed: ${e?.name || "error"}` };
      }
    }
    interruptResponse = { interrupt_id: fs.interrupt_id, response: outcome };
  }

  // newline-delimited JSON chunks: {session} then {status|text|frame}* then {done}
  const out = open(200, "application/x-ndjson", { "cache-control": "no-store" });
  const chunk = (o) => out.write(JSON.stringify(o) + "\n");
  chunk({ type: "session", session_id: sessionId, role });

  const inner = interruptResponse
    ? { interrupt_response: interruptResponse, role, account_id: accountId, source: "chat", stream: true }
    : { prompt: message, role, account_id: accountId, source: "chat", stream: true };
  const payload = new TextEncoder().encode(JSON.stringify(inner));
  try {
    const resp = await agentcore.send(new InvokeAgentRuntimeCommand({
      agentRuntimeArn: RUNTIME_ARN, qualifier: QUALIFIER, runtimeSessionId: sessionId,
      payload, contentType: "application/json", accept: "text/event-stream",
    }));
    // The container streams SSE frames (`data: <our json chunk>\n\n`); parse them and
    // re-emit each chunk as NDJSON to the browser. Buffered fallback: a non-SSE body
    // (no `data:` frames) is forwarded as one text chunk.
    let sse = "", any = false;
    const dec = new TextDecoder();
    if (resp.response) {
      for await (const part of resp.response) {
        sse += dec.decode(part, { stream: true });
        let i;
        while ((i = sse.indexOf("\n\n")) >= 0) {
          const frame = sse.slice(0, i); sse = sse.slice(i + 2);
          for (const line of frame.split("\n")) {
            if (line.startsWith("data:")) {
              const data = line.slice(5).trim();
              if (data) { out.write(data + "\n"); any = true; } // data IS our json chunk
            }
          }
        }
      }
    }
    if (!any) {                                                  // not SSE → one buffered body
      let text = sse;
      try { const d = JSON.parse(sse); text = d.response || d.error || sse; } catch { /* not json */ }
      if (text) chunk({ type: "text", text });
    }
  } catch (e) {
    console.log("invoke error:", e);
    chunk({ type: "text", text: `request failed: ${e.name || "error"}` });
  }
  await touchChat(accountId, sessionId, message);   // record/update this chat in the per-user index
  chunk({ type: "done" });
  out.end();
}

function finishJson(open, statusCode, obj) { const s = open(statusCode, "application/json"); s.write(JSON.stringify(obj)); s.end(); }

// `awslambda` only exists in the streaming-enabled Lambda runtime; fall back to an
// identity wrapper so the module imports locally for unit-testing the pure functions.
const streamify = globalThis.awslambda?.streamifyResponse || ((fn) => fn);
export const handler = streamify(handlerImpl);
