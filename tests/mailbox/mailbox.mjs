// mailbox — receive real email in a test, without a human and without an agent.
//
// `prod/email` puts a CATCH-ALL on gradienterp.cloud: SES writes every inbound message to
// s3://gerp-mail-inbound-<operator-acct>/inbound/ (position 1) and only then forwards it
// (position 2). So any address at that domain already receives, with no identity to verify and no
// stack to stand up — this module is just the reader.
//
// That makes it standing tooling rather than a signup fixture. Cognito's confirmation code is the
// first caller, but every provider verification in phase 0 — Stripe, Square and PayPal onboarding,
// Plaid, domain-ownership checks — lands in the same bucket. The tool takes an address and returns
// the message; `code()` and `link()` are extractors over the body, so nothing here knows what
// Cognito is.
//
// Addresses under `test+…@gradienterp.cloud` are skipped by the forwarder, so test traffic stops at
// S3 instead of reaching a personal inbox. Use `testAddress()` to create one.

import { S3Client, ListObjectsV2Command, GetObjectCommand } from "@aws-sdk/client-s3";
import { STSClient, GetCallerIdentityCommand } from "@aws-sdk/client-sts";
import { fromIni } from "@aws-sdk/credential-providers";
import { randomUUID } from "node:crypto";

const REGION = process.env.AWS_REGION || "us-east-1";
const PROFILE = process.env.MAILBOX_PROFILE || process.env.E2E_OPERATOR_PROFILE || "operator-org";
const PREFIX = process.env.MAILBOX_PREFIX || "inbound/";
export const DOMAIN = process.env.MAILBOX_DOMAIN || "gradienterp.cloud";
export const NO_FORWARD_PREFIX = "test+"; // must match prod/email/forwarder NO_FORWARD_PREFIX

let _s3, _bucket;

function s3() {
  return (_s3 ??= new S3Client({ region: REGION, credentials: fromIni({ profile: PROFILE }) }));
}

// The bucket name embeds the operator account id. Resolved from STS rather than written down —
// account ids are on the pre-publish scrub list, so they don't belong in the repo.
async function bucket() {
  if (_bucket) return _bucket;
  if (process.env.MAILBOX_BUCKET) return (_bucket = process.env.MAILBOX_BUCKET);
  const sts = new STSClient({ region: REGION, credentials: fromIni({ profile: PROFILE }) });
  const { Account } = await sts.send(new GetCallerIdentityCommand({}));
  return (_bucket = `gerp-mail-inbound-${Account}`);
}

/** A unique address the forwarder will NOT forward. `testAddress("signup")` → test+signup-<uuid>@… */
export function testAddress(tag = "e2e") {
  return `${NO_FORWARD_PREFIX}${tag}-${randomUUID().slice(0, 8)}@${DOMAIN}`;
}

// ─── MIME ───────────────────────────────────────────────────────────────────
// Hand-rolled rather than pulling a parser tree in: we need headers, a decoded text/plain and a
// decoded text/html, and nothing else. Verified against real messages in the live bucket.

function splitHeaders(buf) {
  const s = buf.toString("binary");
  const i = s.search(/\r?\n\r?\n/);
  if (i < 0) return [s, ""];
  const gap = s.slice(i).match(/^\r?\n\r?\n/)[0].length;
  return [s.slice(0, i), s.slice(i + gap)];
}

function parseHeaders(block) {
  const out = {};
  // unfold: a continuation line starts with space or tab and belongs to the previous header
  for (const line of block.replace(/\r?\n[ \t]+/g, " ").split(/\r?\n/)) {
    const m = line.match(/^([!-9;-~]+):\s*(.*)$/);
    if (!m) continue;
    const k = m[1].toLowerCase();
    out[k] = out[k] ? `${out[k]}, ${m[2]}` : m[2];
  }
  return out;
}

function decodeQP(s) {
  return Buffer.from(
    s.replace(/=\r?\n/g, "").replace(/=([0-9A-Fa-f]{2})/g, (_, h) => String.fromCharCode(parseInt(h, 16))),
    "binary",
  ).toString("utf8");
}

function decodeBody(headers, body) {
  const enc = (headers["content-transfer-encoding"] || "7bit").toLowerCase().trim();
  if (enc === "base64") return Buffer.from(body.replace(/\s/g, ""), "base64").toString("utf8");
  if (enc === "quoted-printable") return decodeQP(body);
  return Buffer.from(body, "binary").toString("utf8");
}

/** Walk a (possibly multipart) message, collecting the first text/plain and text/html found. */
function walk(headers, body, acc) {
  const ctype = headers["content-type"] || "text/plain";
  const boundary = ctype.match(/boundary="?([^";]+)"?/i)?.[1];

  if (/^multipart\//i.test(ctype) && boundary) {
    // NON-capturing on purpose: String.split interleaves capture groups into its output, so a
    // capturing `(--)?` yields an `undefined` between every part.
    const parts = body.split(new RegExp(`--${boundary.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:--)?\\r?\\n`));
    for (const part of parts.slice(1)) {
      if (!part || !part.trim()) continue;
      const [h, b] = splitHeaders(Buffer.from(part, "binary"));
      walk(parseHeaders(h), b, acc);
    }
    return acc;
  }
  const text = decodeBody(headers, body);
  if (/^text\/html/i.test(ctype)) acc.html ??= text;
  else acc.text ??= text;
  return acc;
}

export function parse(raw) {
  const [h, b] = splitHeaders(raw);
  const headers = parseHeaders(h);
  const { text, html } = walk(headers, b, {});
  return {
    headers,
    to: headers.to || "",
    from: headers.from || "",
    subject: headers.subject || "",
    text: text || "",
    html: html || "",
    body: text || html || "",
    raw,
  };
}

// ─── read ───────────────────────────────────────────────────────────────────

/** Every message in the bucket written at/after `since` (ms epoch), newest first. */
export async function listSince(since = 0) {
  const Bucket = await bucket();
  const out = [];
  let ContinuationToken;
  do {
    const page = await s3().send(new ListObjectsV2Command({ Bucket, Prefix: PREFIX, ContinuationToken }));
    for (const o of page.Contents || []) {
      if (o.LastModified && o.LastModified.getTime() >= since) out.push(o);
    }
    ContinuationToken = page.IsTruncated ? page.NextContinuationToken : undefined;
  } while (ContinuationToken);
  return out.sort((a, b) => b.LastModified - a.LastModified);
}

export async function fetch(key) {
  const Bucket = await bucket();
  const r = await s3().send(new GetObjectCommand({ Bucket, Key: key }));
  const msg = parse(Buffer.from(await r.Body.transformToByteArray()));
  msg.key = key;
  return msg;
}

/**
 * Wait for a message addressed to `to`.
 *
 * SES keys objects by an opaque message id, not by recipient, so this lists what is new and filters
 * on the parsed recipient headers. Pass `since` from BEFORE the action that triggers the mail —
 * default is call time, which is right when you await immediately after clicking.
 */
export async function waitForMail(to, { since = Date.now(), timeoutMs = 120_000, pollMs = 3_000, subject } = {}) {
  const want = to.toLowerCase();
  const deadline = Date.now() + timeoutMs;
  const seen = new Set();
  for (;;) {
    for (const obj of await listSince(since - 5_000)) {
      if (seen.has(obj.Key)) continue;
      seen.add(obj.Key);
      const msg = await fetch(obj.Key);
      const rcpt = [msg.to, msg.headers["delivered-to"], msg.headers["x-original-to"], msg.headers.cc]
        .filter(Boolean).join(",").toLowerCase();
      if (!rcpt.includes(want)) continue;
      if (subject && !msg.subject.includes(subject)) continue;
      return msg;
    }
    if (Date.now() > deadline) {
      throw new Error(`mailbox: no message for ${to} within ${Math.round(timeoutMs / 1000)}s`);
    }
    await new Promise((r) => setTimeout(r, pollMs));
  }
}

// ─── extractors ─────────────────────────────────────────────────────────────

// Both extractors scan text AND html, not `body`. `body` is text-or-html, and providers routinely
// put the link only in the HTML part — searching one part silently misses it.
const searchable = (msg) => `${msg.text || ""}\n${msg.html || ""}`;

/** The first standalone run of digits — Cognito's confirmation code is 6. */
export function code(msg, { digits = 6 } = {}) {
  const m = searchable(msg).replace(/<[^>]+>/g, " ").match(new RegExp(`\\b\\d{${digits}}\\b`));
  if (!m) throw new Error(`mailbox: no ${digits}-digit code in "${msg.subject}"`);
  return m[0];
}

/** The first URL, optionally the first one matching `match`. */
export function link(msg, { match } = {}) {
  const urls = [...searchable(msg).matchAll(/https?:\/\/[^\s"'<>)\]]+/g)].map((m) => m[0]);
  const hit = match ? urls.find((u) => (match instanceof RegExp ? match.test(u) : u.includes(match))) : urls[0];
  if (!hit) throw new Error(`mailbox: no link${match ? ` matching ${match}` : ""} in "${msg.subject}"`);
  return hit.replace(/[.,;]+$/, "");
}
