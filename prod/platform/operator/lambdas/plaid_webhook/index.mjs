/**
 * plaid_webhook — the operator webhook shim (see modules/accounting/AGENTS.md § bank-feed reconciliation).
 *
 * Plaid signs webhooks with an ES256 JWT (Plaid-Verification header); Python can't verify ECDSA, so
 * this thin Node shim does the crypto with the built-in `crypto` module (zero npm deps — @aws-sdk v3
 * is provided by the nodejs runtime). It never holds the Plaid secret: it asks the Python gateway for
 * the JWK (verification_key op) and, on a verified SYNC_UPDATES_AVAILABLE, tells the gateway to route
 * (webhook_route op resolves item_id → gerp and cross-account-invokes that gerp's reconcile).
 *
 * Behind a Lambda Function URL (auth NONE) — Plaid's JWT is the authentication.
 */
import crypto from "node:crypto";

const GATEWAY = process.env.PLAID_GATEWAY_FN;
const keyCache = new Map(); // by kid

// @aws-sdk v3 is provided by the nodejs runtime, not bundled — import it lazily so the crypto in
// verify() stays importable/testable without the SDK present.
let _sdk = null;
async function invokeGateway(payload) {
  if (!_sdk) {
    const m = await import("@aws-sdk/client-lambda");
    _sdk = { client: new m.LambdaClient({}), Invoke: m.InvokeCommand };
  }
  const out = await _sdk.client.send(new _sdk.Invoke({ FunctionName: GATEWAY, Payload: Buffer.from(JSON.stringify(payload)) }));
  const res = JSON.parse(Buffer.from(out.Payload).toString());
  if (!res.ok) throw new Error(`gateway ${payload.op} failed: ${JSON.stringify(res)}`);
  return res;
}

const b64urlJson = (seg) => JSON.parse(Buffer.from(seg, "base64url").toString());

/**
 * Verify a Plaid webhook. Throws on any failure. `fetchJwk(kid)` returns the JWK for a key id —
 * defaults to the gateway's verification_key op; injected in tests.
 */
export async function verify(rawBody, token, fetchJwk = gatewayJwk) {
  const [h, p, s] = token.split(".");
  const header = b64urlJson(h);
  if (header.alg !== "ES256") throw new Error(`unexpected alg ${header.alg}`);
  if (!header.kid) throw new Error("missing kid");

  let jwk = keyCache.get(header.kid);
  if (!jwk) {
    jwk = await fetchJwk(header.kid);
    keyCache.set(header.kid, jwk);
  }

  // JWT ES256 signatures are raw r||s (IEEE P1363), not DER — tell Node's verifier so.
  const pub = crypto.createPublicKey({ key: jwk, format: "jwk" });
  const ok = crypto.verify("sha256", Buffer.from(`${h}.${p}`), { key: pub, dsaEncoding: "ieee-p1363" }, Buffer.from(s, "base64url"));
  if (!ok) throw new Error("bad signature");

  const claims = b64urlJson(p);
  const now = Math.floor(Date.now() / 1000);
  if (!claims.iat || now - claims.iat > 300) throw new Error("stale token (iat older than 5m)");

  // hash the RAW body exactly as received — whitespace-sensitive
  const bodyHash = crypto.createHash("sha256").update(rawBody).digest("hex");
  const a = Buffer.from(bodyHash);
  const b = Buffer.from(claims.request_body_sha256 || "");
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) throw new Error("body hash mismatch");
}

const gatewayJwk = async (kid) => (await invokeGateway({ op: "verification_key", kid })).key;

export const handler = async (event) => {
  const headers = event.headers || {};
  const token = headers["plaid-verification"] || headers["Plaid-Verification"];
  const rawBody = event.isBase64Encoded ? Buffer.from(event.body || "", "base64").toString("utf8") : (event.body || "");
  if (!token) return { statusCode: 400, body: "missing Plaid-Verification" };

  try {
    await verify(rawBody, token);
  } catch (e) {
    console.error("webhook verify failed:", e.message);
    return { statusCode: 401, body: "verification failed" };
  }

  const wh = JSON.parse(rawBody);
  if (wh.webhook_type === "TRANSACTIONS" && wh.webhook_code === "SYNC_UPDATES_AVAILABLE") {
    await invokeGateway({ op: "webhook_route", item_id: wh.item_id });
  }
  // everything else (LINK SESSION_FINISHED etc.) is acked; connect completion stays poll-based for now
  return { statusCode: 200, body: "ok" };
};
