/**
 * Local tests for the Node chat lambda (modules/agent/lambdas/chat/index.mjs).
 * Exercises the security-critical JWT verify end-to-end with a real generated
 * keypair (JWKS mocked via globalThis.fetch) + role resolution + servePage.
 * Run: node --test tests/agent/local/chat.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";

process.env.AWS_REGION = "us-east-1";
process.env.COGNITO_USER_POOL_ID = "us-east-1_testpool";
process.env.COGNITO_CLIENT_ID = "testclient123";
process.env.COGNITO_DOMAIN_PREFIX = "gerp-auth";
process.env.LOCAL_OWNER_SUB = "owner-sub-1";

const ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_testpool";
const { publicKey, privateKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
const JWK = { ...publicKey.export({ format: "jwk" }), kid: "testkid", alg: "RS256", use: "sig" };

// mock the JWKS fetch — the real RSA verify still runs
globalThis.fetch = async () => ({ ok: true, json: async () => ({ keys: [JWK] }) });

const { verifyJwt, resolveRole, servePage, frameSinkArns, resolveSink, buildToolInput } = await import("../../../modules/agent/lambdas/chat/index.mjs");

const b64url = (o) => Buffer.from(typeof o === "string" ? o : JSON.stringify(o)).toString("base64url");
function makeToken(claims, { kid = "testkid", alg = "RS256" } = {}) {
  const h = b64url({ alg, kid, typ: "JWT" });
  const p = b64url(claims);
  const sig = crypto.sign("RSA-SHA256", Buffer.from(`${h}.${p}`), privateKey).toString("base64url");
  return `${h}.${p}.${sig}`;
}
const claims = (o = {}) => ({ sub: "owner-sub-1", iss: ISSUER, aud: "testclient123", token_use: "id", exp: 9999999999, ...o });
const rejects = (fn) => assert.rejects(fn);

test("valid id token", async () => {
  const c = await verifyJwt(makeToken(claims()));
  assert.equal(c.sub, "owner-sub-1");
});
test("valid access token", async () => {
  const c = await verifyJwt(makeToken(claims({ token_use: "access", client_id: "testclient123", aud: undefined })));
  assert.equal(c.sub, "owner-sub-1");
});
test("expired rejected", () => rejects(() => verifyJwt(makeToken(claims({ exp: 1 })))));
test("bad issuer rejected", () => rejects(() => verifyJwt(makeToken(claims({ iss: "https://evil" })))));
test("bad audience rejected", () => rejects(() => verifyJwt(makeToken(claims({ aud: "other" })))));
test("alg none rejected", () => rejects(() => verifyJwt(makeToken(claims(), { alg: "none" }))));
test("alg HS256 rejected", () => rejects(() => verifyJwt(makeToken(claims(), { alg: "HS256" }))));
test("unknown kid rejected", () => rejects(() => verifyJwt(makeToken(claims(), { kid: "rotated" }))));
test("tampered payload rejected", () => {
  const [h, , s] = makeToken(claims()).split(".");
  const forged = b64url(claims({ sub: "admin" }));
  return rejects(() => verifyJwt(`${h}.${forged}.${s}`));
});

test("role owner via local stash", async () => assert.equal(await resolveRole("owner-sub-1"), "owner"));
test("role none for stranger", async () => assert.equal(await resolveRole("nobody"), null));

test("servePage injects config + no placeholder", () => {
  const html = servePage("");
  assert.ok(!html.includes("{{CONFIG}}"));
  assert.ok(html.includes("testclient123"));
});
test("servePage injects a token when present", () => {
  assert.ok(servePage("injected-tok-xyz").includes("injected-tok-xyz"));
  // the refresh token rides beside it, and the page's refresh path is there to use it
  const withRefresh = servePage("injected-tok-xyz", "refresh-abc");
  assert.ok(withRefresh.includes('"injectedRefresh":"refresh-abc"'));
  assert.ok(withRefresh.includes("REFRESH_TOKEN_AUTH") && withRefresh.includes("gerp_chat_refresh"));
  assert.ok(servePage("").includes('"injectedRefresh":""'));
});

test("an empty sink tag query is not cached; the next call asks again", async () => {
  const answers = [[], ["arn:aws:lambda:us-east-1:1:function:gerp-secrets-x-manage_secret"]];
  let asked = 0;
  const client = { send: async () => ({ ResourceTagMappingList: (answers[asked++] || answers[1]).map((a) => ({ ResourceARN: a })) }) };
  assert.deepEqual(await frameSinkArns(client), []);
  assert.deepEqual(await frameSinkArns(client), answers[1]);
  assert.deepEqual(await frameSinkArns(client), answers[1]);
  assert.equal(asked, 2);   // the non-empty answer is the one kept
});

test("the form sink resolves to the manage_secret lambda by its tag, and to nothing else", async () => {
  const arn = "arn:aws:lambda:us-east-1:1:function:gerp-secrets-x-manage_secret";
  const client = { send: async () => ({ ResourceTagMappingList: [{ ResourceARN: arn }] }) };
  assert.equal(await resolveSink("manage_secret", client), arn);
  assert.equal(await resolveSink("put_secret", client), null);
  assert.equal(await resolveSink("manage_mcp", client), null);
});

test("a values_key naming a prototype is refused and pollutes nothing", () => {
  assert.throws(() => buildToolInput({}, "__proto__.testkid", { kty: "RSA" }), /prototype/);
  assert.throws(() => buildToolInput({}, "a.constructor.prototype", { x: 1 }), /prototype/);
  assert.equal(({}).testkid, undefined);
  assert.equal(({}).kty, undefined);
  assert.deepEqual(buildToolInput({ op: "put" }, "values.fields", { k: "v" }), { op: "put", values: { fields: { k: "v" } } });
});

test("a token whose kid names an inherited property is refused", async () => {
  for (const kid of ["__proto__", "constructor", "toString", "hasOwnProperty"]) {
    await rejects(() => verifyJwt(makeToken(claims(), { kid })));
  }
});

test("the api gate answers the owner only, and a session is continued only by its account", async () => {
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../../modules/agent/lambdas/chat/index.mjs", import.meta.url), "utf8");
  const gate = src.slice(src.indexOf('if (path.startsWith("/api/"))'), src.indexOf('if (method === "POST"   && path === "/api/chat")'));
  assert.match(gate, /if \(role !== "owner"\) return finish\(403/);
  const chat = src.slice(src.indexOf("async function chat("), src.indexOf("// The form values go straight"));
  assert.match(chat, /if \(!\(await ownsSession\(accountId, sessionId\)\)\) return finishJson\(open, 404/);
  assert.match(chat, /sessionId\.length < 33 \|\| !\(await ownsSession\(accountId, sessionId\)\)/);
});

