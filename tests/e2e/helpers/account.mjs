// Local: the stack's own /dev verbs (tests/server/bff/dev.py) — one implementation of "an account
// in state X", shared with whoever curls it.
const DEV = () => resolveEnv().baseURL + "/dev";
const dev = async (method, path, body) => {
  const r = await fetch(DEV() + path, { method, headers: { "Content-Type": "application/json" },
                                        body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error(`${method} /dev${path} → ${r.status} ${await r.text()}`);
  return r.json();
};
async function localFixtureAccount({ tag = "e2e" } = {}) {
  const { sub, email } = await dev("POST", "/account", { tag });
  return { email, sub, password: null, poolId: null, clientId: null,
           cleanup: () => dev("DELETE", `/account/${sub}`).catch(() => {}) };
}
/** A card saved through the Stripe stand-in, end to end. Local only. */
export async function localCard(sub, number = "4242") {
  return dev("POST", `/account/${sub}/card`, { number });
}

// Throwaway platform accounts — create a real one, use it, delete it.
//
// "Real" is the point. `fixtureAccount()` drives the production path end to end: Cognito SignUp
// sends a verification code through SES, `tests/mailbox` reads it off the live catch-all,
// ConfirmSignUp fires the `PostConfirmation` trigger, and the real
// `tower-cognito-post-confirmation` lambda seeds the `gerp-accounts` row. Nothing is stubbed, so a
// spec that passes against a fixture account proves the same path a stranger walks.
//
// Teardown is exactly two deletes, because signup creates exactly two things — the Cognito user and
// one `gerp-accounts` row. The trigger deliberately vends no sub-account ("account created = the
// Cognito identity; provisioning is an explicit gerp-instance capability action"). That is what
// makes accounts disposable: the moment a spec toggles `erp_instance` it vends a real AWS account
// through Control Tower, which is quota-bound and NOT throwaway — reuse a parked gerp for that.
//
// Pool and client are resolved BY NAME, never hardcoded: pool ids are environment-specific and the
// account id embedded elsewhere is on the pre-publish scrub list.

import {
  CognitoIdentityProviderClient, SignUpCommand, ConfirmSignUpCommand, AdminGetUserCommand,
  AdminDeleteUserCommand, ListUserPoolsCommand, ListUserPoolClientsCommand,
} from "@aws-sdk/client-cognito-identity-provider";
import { DynamoDBClient, DeleteItemCommand, GetItemCommand, PutItemCommand, UpdateItemCommand } from "@aws-sdk/client-dynamodb";
import { fromIni } from "@aws-sdk/credential-providers";
import { testAddress, waitForMail, code } from "../../mailbox/mailbox.mjs";
import { isLocal, resolveEnv } from "./env.mjs";
import { randomBytes } from "node:crypto";

const REGION = process.env.AWS_REGION || "us-east-1";
const PROFILE = process.env.E2E_OPERATOR_PROFILE || "operator-org";
const POOL_NAME = process.env.E2E_POOL_NAME || "gradienterp";
// The smoke-test client allows ALLOW_USER_PASSWORD_AUTH, so a spec can authenticate without a
// browser. The SPA client stays SRP-only on purpose — don't switch this to it.
const CLIENT_NAME = process.env.E2E_POOL_CLIENT || "smoke-test";
const ACCOUNTS_TABLE = process.env.E2E_ACCOUNTS_TABLE || "gerp-accounts";

// Meets the pool's policy: >=8, upper, lower, number. Made once per run: these users live in the
// live pool for seconds, and one a failed cleanup leaves behind has a password nobody can read.
export const FIXTURE_PASSWORD = "Aa1-" + randomBytes(12).toString("hex");

let _idp, _ddb, _ids;
// Against `local` these point at moto with dummy creds — same switch as helpers/aws.mjs, and the
// table name is the deployed one either way, so `getAccount()` reads the same row shape.
const { aws: LOCAL_AWS } = resolveEnv();
const clientCfg = () => LOCAL_AWS ? { region: REGION, ...LOCAL_AWS }
                                  : { region: REGION, credentials: fromIni({ profile: PROFILE }) };
const idp = () => (_idp ??= new CognitoIdentityProviderClient(clientCfg()));
const ddb = () => (_ddb ??= new DynamoDBClient(clientCfg()));

async function ids() {
  if (_ids) return _ids;
  const pools = await idp().send(new ListUserPoolsCommand({ MaxResults: 60 }));
  const pool = pools.UserPools?.find((p) => p.Name === POOL_NAME);
  if (!pool) throw new Error(`account: no user pool named ${POOL_NAME}`);
  const clients = await idp().send(new ListUserPoolClientsCommand({ UserPoolId: pool.Id, MaxResults: 60 }));
  const client = clients.UserPoolClients?.find((c) => c.ClientName === CLIENT_NAME);
  if (!client) throw new Error(`account: no pool client named ${CLIENT_NAME} in ${pool.Id}`);
  return (_ids = { poolId: pool.Id, clientId: client.ClientId });
}

/** The private account-profile row the PostConfirmation trigger seeds. null until it lands. */
/** The seller gerp's side of the record: the contact its customers/upsert hook wrote. The row lives
 *  in the seller's own account, which only the local stack lets a test read — through /dev. */
export async function getSellerContact(sub) {
  if (!isLocal()) return null;
  return (await dev("GET", `/account/${sub}`)).contact;
}

export async function getAccount(sub) {
  const r = await ddb().send(new GetItemCommand({
    TableName: ACCOUNTS_TABLE, Key: { account_id: { S: sub } },
  }));
  if (!r.Item) return null;
  return Object.fromEntries(Object.entries(r.Item).map(([k, v]) => [k, v.S ?? v.N ?? v.BOOL]));
}

/**
 * Create a confirmed throwaway account. Returns `{ email, sub, password, poolId, clientId, cleanup }`.
 *
 * `cleanup()` is idempotent — call it from an afterEach and don't worry about whether the spec got
 * far enough to need it.
 */
/** Put a saved card on an account row, the way an account-scoped save-card does. */
export async function putAccountCard(sub, { brand, last4, exp, customer = "cus_test", pm = "pm_test" }) {
  await ddb().send(new UpdateItemCommand({
    TableName: ACCOUNTS_TABLE,
    Key: { account_id: { S: sub } },
    UpdateExpression: "SET card_brand=:b, card_last4=:l, card_exp=:e, "
                    + "stripe_customer_id=:c, stripe_payment_method_id=:p",
    ExpressionAttributeValues: {
      ":b": { S: brand }, ":l": { S: last4 }, ":e": { S: exp },
      ":c": { S: customer }, ":p": { S: pm },
    },
  }));
}

// The full private record the create and publish gates require. Fixtures carry it by default so
// a spec about gerps or profiles is not first a spec about the account; `complete: false` is for
// the specs that are.
export const FULL_RECORD = { first_name: "Ada", last_name: "Lovelace", phone: "+1 555 0100",
  street: "1 Analytical Way", city: "London", state: "LDN", zip: "N1", country: "GB" };

export async function completeAccount(sub, record = FULL_RECORD) {
  if (isLocal() && record === FULL_RECORD) return dev("POST", `/account/${sub}/complete`);
  const names = Object.fromEntries(Object.keys(record).map((k) => [`#${k}`, k]));
  const values = Object.fromEntries(Object.entries(record).map(([k, v]) => [`:${k}`, { S: v }]));
  await ddb().send(new UpdateItemCommand({
    TableName: ACCOUNTS_TABLE, Key: { account_id: { S: sub } },
    UpdateExpression: "SET " + Object.keys(record).map((k) => `#${k} = :${k}`).join(", "),
    ExpressionAttributeNames: names, ExpressionAttributeValues: values,
  }));
}

export async function fixtureAccount({ tag = "e2e", timeoutMs = 120_000, complete = true } = {}) {
  if (isLocal()) {
    const acct = await localFixtureAccount({ tag });
    if (complete) await completeAccount(acct.sub);
    return acct;
  }
  const { poolId, clientId } = await ids();
  const email = testAddress(tag);
  const since = Date.now();

  await idp().send(new SignUpCommand({ ClientId: clientId, Username: email, Password: FIXTURE_PASSWORD }));

  // The code is only ever read from the live catch-all — same route a real signup takes.
  const msg = await waitForMail(email, { since, timeoutMs });
  await idp().send(new ConfirmSignUpCommand({
    ClientId: clientId, Username: email, ConfirmationCode: code(msg),
  }));

  const user = await idp().send(new AdminGetUserCommand({ UserPoolId: poolId, Username: email }));
  const sub = user.UserAttributes?.find((a) => a.Name === "sub")?.Value;
  if (!sub) throw new Error(`account: confirmed ${email} but it has no sub`);
  if (complete) await completeAccount(sub);

  return { email, sub, password: FIXTURE_PASSWORD, poolId, clientId, cleanup: () => deleteAccount({ email, sub }) };
}

/**
 * The Cognito sub for an address, or null if there is no such user. Needed when the signup happened
 * in the BROWSER rather than through `fixtureAccount()` — the spec never saw the sub, but teardown
 * needs it to delete the `gerp-accounts` row (which is keyed on it, not on email).
 */
/**
 * The local fixture: an account is the gerp-accounts row plus a session token.
 *
 * Deliberately NOT a signup walk. Signing up is Cognito's `SignUp` + a verification code delivered
 * by SES, and neither has a local form — but no spec downstream of the login gate cares HOW the
 * account came to exist, only that it does and that the browser is it. `signup.spec` is the one
 * that cares, and it stays prod-only because the mail round-trip is its actual claim.
 *
 * The row is what `cognito_post_confirmation` writes for a real signup, so a spec reading
 * `getAccount(sub)` sees the same shape either way.
 */

export async function lookupSub(email) {
  const { poolId } = await ids();
  try {
    const u = await idp().send(new AdminGetUserCommand({ UserPoolId: poolId, Username: email }));
    return u.UserAttributes?.find((a) => a.Name === "sub")?.Value ?? null;
  } catch (e) {
    if (e.name === "UserNotFoundException") return null;
    throw e;
  }
}

/**
 * Delete both things a signup creates. Idempotent and never throws — teardown that fails a passing
 * spec is worse than teardown that leaves a row behind, and both artifacts are inert.
 */
/** The pool's attributes for an address — what Cognito holds, as {name: value}. */
export async function poolAttributes(email) {
  const { poolId } = await ids();
  const u = await idp().send(new AdminGetUserCommand({ UserPoolId: poolId, Username: email }));
  return Object.fromEntries((u.UserAttributes || []).map((a) => [a.Name, a.Value]));
}

export async function deleteAccount({ email, sub }) {
  const { poolId } = await ids();
  const problems = [];
  // The sub is the pool's real Username; the email is an alias, and an alias that has been changed
  // no longer resolves — deleting by email after an email change would leave the user behind.
  for (const username of [sub, email].filter(Boolean)) {
    try {
      await idp().send(new AdminDeleteUserCommand({ UserPoolId: poolId, Username: username }));
      break;
    } catch (e) {
      if (e.name !== "UserNotFoundException") { problems.push(`cognito: ${e.name}`); break; }
    }
  }
  if (sub) {
    try {
      await ddb().send(new DeleteItemCommand({ TableName: ACCOUNTS_TABLE, Key: { account_id: { S: sub } } }));
    } catch (e) {
      problems.push(`ddb: ${e.name}`);
    }
  }
  if (problems.length) console.warn(`account cleanup left residue for ${email}: ${problems.join(", ")}`);
  // The S3 copy of the verification email is deliberately NOT deleted — prod/email expires it at 7
  // days on its own, and it is the only record of a failed run worth reading afterwards.
}
