// AWS-side helpers — the "backend" half of the e2e: read creds + assert against live SSM.
// Profiles come from ~/.aws/config (the same named profiles used elsewhere). operator-org
// reads the e2e creds + operator params; the customer profile reads a gerp's tenant blob.

import { SSMClient, GetParameterCommand } from "@aws-sdk/client-ssm";
import { DynamoDBClient, DeleteItemCommand, GetItemCommand, PutItemCommand, QueryCommand, ScanCommand } from "@aws-sdk/client-dynamodb";
import { LambdaClient, InvokeCommand } from "@aws-sdk/client-lambda";
import { fromIni } from "@aws-sdk/credential-providers";
import { resolveEnv } from "./env.mjs";

const REGION = process.env.AWS_REGION || "us-east-1";
const OPERATOR_PROFILE = process.env.E2E_OPERATOR_PROFILE || "operator-org";
const CUSTOMER_PROFILE = process.env.E2E_CUSTOMER_PROFILE || "gerp-gradienterp";

// Against `local` the two profiles collapse: one emulator, one account, and the table names are the
// deployed ones — `gerp-settings-gradienterp` either way — so every assertion below is unchanged.
const { aws: LOCAL_AWS } = resolveEnv();
const cfg = (profile) => LOCAL_AWS
  ? { region: REGION, ...LOCAL_AWS }
  : { region: REGION, credentials: fromIni({ profile }) };

const _clients = {};
function ssm(profile) {
  return (_clients[LOCAL_AWS ? "local" : profile] ??= new SSMClient(cfg(profile)));
}

const _ddb = {};
function ddb(profile) {
  return (_ddb[LOCAL_AWS ? "local" : profile] ??= new DynamoDBClient(cfg(profile)));
}

export async function getParam(name, { profile = OPERATOR_PROFILE, decrypt = false } = {}) {
  const r = await ssm(profile).send(new GetParameterCommand({ Name: name, WithDecryption: decrypt }));
  return r.Parameter.Value;
}

// The live tenant metadata blob for a gerp (customer account). Holds provisioning metadata
// (business_name, owner_email, …). NOTE: as of the settings→DDB migration this is NO LONGER the
// assertion target for openly_operated — the blob keeps that field only as the provision-time seed
// source (frozen); the live flag is in the settings config table (see getOpenlyOperated).
export async function getTenantBlob(gerpId) {
  return JSON.parse(await getParam(`/gradienterp/customers/${gerpId}`, { profile: CUSTOMER_PROFILE }));
}

// The live openly_operated flag — the settings config table's GERP#openly_operated row (customer
// account). This is what the settings-screen toggle writes (via the gerp gateway's PUT /settings),
// so it's the toggle's real assertion target.
export async function getOpenlyOperated(gerpId) {
  const table = `gerp-settings-${gerpId.replace(/_/g, "-")}`;
  const r = await ddb(CUSTOMER_PROFILE).send(new GetItemCommand({
    TableName: table,
    Key: { gerp_id: { S: gerpId }, sk: { S: "GERP#openly_operated" } },
  }));
  return r.Item?.value?.BOOL ?? false;
}

// The firm's standing instructions — every INSTRUCTION# row on the settings config table, in the
// order the agent's prompt block renders them (the sk's leading ms). This is what the gerp screen's
// instruction list writes, so it's that list's real assertion target.
export async function getInstructions(gerpId) {
  const table = `gerp-settings-${gerpId.replace(/_/g, "-")}`;
  const r = await ddb(CUSTOMER_PROFILE).send(new QueryCommand({
    TableName: table,
    KeyConditionExpression: "gerp_id = :g AND begins_with(sk, :p)",
    ExpressionAttributeValues: { ":g": { S: gerpId }, ":p": { S: "INSTRUCTION#" } },
  }));
  return (r.Items ?? [])
    .sort((a, b) => a.sk.S.localeCompare(b.sk.S))
    .map((i) => i.text?.S ?? "");
}

// e2e owner login creds (operator account): email plain + password SecureString. Set the
// password yourself (see README) — it's never in the repo.
export async function seedGerpRow(sub, { gerp_id, label, status = "", gateway_url = "", download_until = "" }) {
  // LOCAL ONLY. A state the SPA has to render but no read-only run can reach — an abandoned
  // purchase — is written here rather than manufactured against a live account, where it would
  // mean a real gerp row and a real Stripe session.
  if (!LOCAL_AWS) throw new Error("seedGerpRow is local-only — it writes rows");
  const r = await fetch(resolveEnv().baseURL + `/dev/gerp/${sub}`, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ gerp_id, label, status, gateway_url, download_until }) });
  if (!r.ok) throw new Error(`POST /dev/gerp → ${r.status} ${await r.text()}`);

}


/** A seeded row's cleanup — `seedGerpRow` writes through `/dev` and hands nothing back. */
export async function deleteGerpRow(sub, gerp_id) {
  const S = (v) => ({ S: v });
  await ddb().send(new DeleteItemCommand({ TableName: "gerp-customers", Key: { gerp_id: S(gerp_id) } })).catch(() => {});
  await ddb().send(new DeleteItemCommand({ TableName: "gerp-members", Key: { account_id: S(sub), gerp_id: S(gerp_id) } })).catch(() => {});
}

/** The gerp-customers row, raw. */
const plain = (v) => v.S ?? v.N ?? v.BOOL ?? (v.M ? Object.fromEntries(Object.entries(v.M).map(([k, x]) => [k, plain(x)])) : v.L ? v.L.map(plain) : undefined);
export async function getGerpRow(gerpId) {
  const r = await ddb().send(new GetItemCommand({
    TableName: "gerp-customers", Key: { gerp_id: { S: gerpId } },
  }));
  return Object.fromEntries(Object.entries(r.Item || {}).map(([k, v]) => [k, plain(v)]));
}

/**
 * A gerp without vending one. `_create_gerp` does two separable things — write the gerp-customers
 * row plus the gerp-members owner row, then async-invoke the provisioner. This is the first half:
 * everything operator-side, instant, quota-free, and torn down with two deletes. The vend is
 * Control Tower and belongs to tower's own e2e, not the client walk.
 */
export async function fixtureGerp(sub, label, { account = "000000000000",
                                                gateway_url = "http://localhost:8080" } = {}) {
  if (!LOCAL_AWS) throw new Error("fixtureGerp is local-only — it writes rows");
  const gerp_id = `${label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}-${Math.floor(Math.random() * 1e6)}`;
  const S = (v) => ({ S: v });
  await ddb().send(new PutItemCommand({
    TableName: "gerp-customers",
    // a gateway_url is what makes it READY — without one the home screen renders it as
    // provisioning, which is deliberately not clickable, so no spec could enter it
    Item: { gerp_id: S(gerp_id), owner_sub: S(sub), label: S(label), aws_account_id: S(account),
            gateway_url: S(gateway_url) },
  }));
  await ddb().send(new PutItemCommand({
    TableName: "gerp-members",
    Item: { account_id: S(sub), gerp_id: S(gerp_id), role: S("owner") },
  }));
  return {
    gerp_id, label,
    cleanup: async () => {
      await ddb().send(new DeleteItemCommand({ TableName: "gerp-customers", Key: { gerp_id: S(gerp_id) } })).catch(() => {});
      await ddb().send(new DeleteItemCommand({ TableName: "gerp-members", Key: { account_id: S(sub), gerp_id: S(gerp_id) } })).catch(() => {});
    },
  };
}

export async function getOwnerCreds() {
  if (LOCAL_AWS) {
    // No SSM to read and no password to type — `login()` injects the session locally. The SUB has
    // to be the one the bff seeded from LOCAL_GERPS, or the account owns no gerps and every screen
    // past the home list is empty; read it off the row rather than configuring it twice.
    // Not `Limit: 1`: by the time a gerp-screen spec runs, the account and purchase specs have
    // left fixture rows behind, and whichever one moto hands back first owns no provisioned gerp.
    // The seeded row is the one with a chat_url — provisioning writes it, a fixture never does.
    const r = await ddb().send(new ScanCommand({ TableName: "gerp-customers" }));
    // a fixture row a spec left behind — a closed gerp, an unpaid one — owns nothing usable
    const rows = (r.Items || []).filter((i) => i.owner_sub?.S && !i.closed_at && i.status?.S !== "awaiting_payment");
    const seeded = rows.find((i) => i.chat_url?.S) || rows.find((i) => i.gateway_url?.S) || rows[0];
    const sub = seeded?.owner_sub?.S;
    if (!sub) {
      throw new Error("gerp-customers is empty on the emulator — is LOCAL_GERPS set, and did " +
                      "`bash scripts/local-dev.sh --start` seed it?");
    }
    return { email: process.env.E2E_LOCAL_EMAIL || "owner@localhost", password: null, sub };
  }
  const [email, password] = await Promise.all([
    getParam("/gradienterp/test/e2e/owner_email"),
    getParam("/gradienterp/test/e2e/owner_password", { decrypt: true }),
  ]);
  return { email, password };
}

// A journal entry on a gerp's own ledger, through its post_journal_entry — the known ledger fact a
// public surface must then show. Returns the entry the lambda answered with.
export async function postJournalEntry(gerpId, { amount, memo = "e2e" }) {
  const client = new LambdaClient(cfg(CUSTOMER_PROFILE));
  const body = { lineItems: [
    { account: "CASH", accountType: "ASSET", side: "DEBIT", amount },
    { account: "SALES_REVENUE", accountType: "REVENUE", side: "CREDIT", amount },
  ], memo, source: "manual" };
  const r = await client.send(new InvokeCommand({ FunctionName: `gerp-accounting-${gerpId}-post_journal_entry`, Payload: JSON.stringify(body) }));
  const out = JSON.parse(Buffer.from(r.Payload).toString());
  if (out.statusCode !== 200) throw new Error(`post_journal_entry: ${JSON.stringify(out)}`);
  return JSON.parse(out.body);
}
