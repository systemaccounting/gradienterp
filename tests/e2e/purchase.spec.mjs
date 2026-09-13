import { test, expect } from "@playwright/test";
import { login } from "./helpers/login.mjs";
import { getOwnerCreds, seedGerpRow, getGerpRow } from "./helpers/aws.mjs";
import { isLocal, resolveEnv } from "./helpers/env.mjs";
import { fixtureAccount, localCard } from "./helpers/account.mjs";
import { DynamoDBClient, GetItemCommand } from "@aws-sdk/client-dynamodb";

// The purchase disclosure. A gerp costs money — an AWS account billed on usage at cost plus 20% —
// and until this screen said so, the first statement of that was Stripe's own line on a page the
// buyer had already been redirected to.
//
// Local only, and deliberately non-mutating: it asserts the gate, never passes it. Creating a gerp
// writes a row and creates a live Stripe session, which is not what a smoke run should leave behind.

async function openCreate(page) {
  await login(page, await getOwnerCreds());
  await page.locator('[data-view="homeScreen"] .card', { hasText: "Create a ∇ERP" }).click();
  await expect(page.locator('[data-view="createGerpScreen"]')).toBeVisible();
}

test("the purchase disclosure states the five facts a buyer needs", { tag: ["@smoke"] }, async ({ page }) => {
  await openCreate(page);
  const terms = page.locator('[data-view="purchaseTerms"]');
  await expect(terms).toBeVisible();

  // what they get, whose organization it is in, that the bill is usage-based, what the markup is,
  // that leaving is unilateral, and what leaving does to their data. Each is a thing a buyer would
  // otherwise discover late.
  await expect(terms).toContainText("member account in the gradientERP AWS organization");
  await expect(terms).toContainText("monthly invoice for your AWS account use");
  await expect(terms).toContainText("20% operator markup");
  await expect(terms).toContainText("cancel anytime");
  await expect(terms).toContainText("exported to you at closure");
  await expect(terms).toContainText("15 days");
});

test("Create is gated on acknowledging it", { tag: ["@smoke"] }, async ({ page }) => {
  await openCreate(page);
  const create = page.locator('[data-view="createGerpBtn"]');
  const ack = page.locator('[data-view="purchaseAck"]');

  await expect(create).toBeDisabled();
  await ack.check();
  await expect(create).toBeEnabled();

  // and unticking re-gates it — the state is the checkbox, not a one-way latch
  await ack.uncheck();
  await expect(create).toBeDisabled();
});

test("the acknowledgement does not survive leaving the screen", { tag: ["@smoke"] }, async ({ page }) => {
  // A buyer who backs out and returns is shown the disclosure again rather than an already-ticked
  // box they never read this time.
  await openCreate(page);
  await page.locator('[data-view="purchaseAck"]').check();
  await expect(page.locator('[data-view="createGerpBtn"]')).toBeEnabled();

  await page.goto("/");
  await page.locator('[data-view="homeScreen"] .card', { hasText: "Create a ∇ERP" }).click();
  await expect(page.locator('[data-view="purchaseAck"]')).not.toBeChecked();
  await expect(page.locator('[data-view="createGerpBtn"]')).toBeDisabled();
});

test("a gerp waiting on a card does not look like one that is building", { tag: ["@local"] }, async ({ page }) => {
  // Both states have no gateway_url, so both are "not ready" — and they mean opposite things. One
  // is a machine working; the other is a purchase nobody finished, which never becomes ready on
  // its own. Rendered the same, an abandoned checkout spins forever and the buyer waits for it.
  test.skip(!isLocal(), "writes a gerp row — local only");

  const { sub, email } = await getOwnerCreds();
  await seedGerpRow(sub, { gerp_id: "e2e-unpaid", label: "Unpaid Co", status: "awaiting_payment" });
  await login(page, { email, sub });

  const card = page.locator('[data-view="gerpCard"]', { hasText: "Unpaid Co" });
  await expect(card).toHaveClass(/\bpay\b/);
  await expect(card).toContainText("waiting on payment");
  await expect(card.locator(".spin")).toHaveCount(0);   // no spinner: nothing is happening

  // and it is the way back into checkout, since the row and its gerp_id already exist
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/api/billing/setup-link") && r.method() === "POST"),
    card.click(),
  ]);
  expect(JSON.parse(req.postData()).gerp_id).toBe("e2e-unpaid");
});

// ── paying with a card the account holds ─────────────────────────────────────────────────────
// Local only: both paths run against the Stripe stand-in, and the second opens a window.
const sellerContact = async (id) => {
  const c = new DynamoDBClient({ region: "us-east-1", ...resolveEnv().aws });
  const r = await c.send(new GetItemCommand({ TableName: "gerp-contacts-gradienterp", Key: { contact_id: { S: id } } }));
  return Object.fromEntries(Object.entries(r.Item || {}).map(([k, v]) => [k, v.S]));
};

// the legal business profile is required and prefills itself from the account record when the
// screen opens — the fixture account's record is complete, so the form is full before anyone types
async function pasteLegal(page) {
  await expect(page.locator("#biz-city")).not.toHaveValue("");
  await expect(page.locator("#biz-name")).toHaveCount(0);   // the business name is the name; no second one
}

test("a gerp is created against the account's card without leaving the page", { tag: ["@local", "@mutating"] }, async ({ page }) => {
  test.skip(!isLocal(), "the Stripe stand-in");
  const acct = await fixtureAccount({ tag: "paywith" });
  try {
    await localCard(acct.sub);        // the stack's own /dev verb, not a walk through the UI
    await login(page, acct);
    await page.locator('[data-view="homeScreen"] .card', { hasText: "Create a ∇ERP" }).click();
    const row = page.locator('[data-view="payWithRow"]');
    await expect(row).toHaveCount(1);
    await expect(row.locator("input")).toBeChecked();          // the default starts selected
    await page.locator("#bizname").fill("Pay With Co");
    await pasteLegal(page);
    // the public profile is the legal fields whose box is ticked: untick phone, keep the rest
    await page.locator('[data-view="pubField"][data-field="phone"] input').uncheck();
    await page.locator('[data-view="purchaseAck"]').check();
    await page.locator('[data-view="createGerpBtn"]').click();
    await expect(page.locator('[data-view="homeScreen"]')).toBeVisible({ timeout: 20_000 });
    await expect(page.locator("#status")).toContainText("Pay With Co is being created");
    const gerp = (await page.evaluate(async () => (await (await fetch("/api/gerps", { headers: {
      Authorization: "Bearer " + sessionStorage.getItem("id_token") } })).json()).gerps)).find(g => g.label === "Pay With Co");
    expect(gerp.status).toBe("active");
    // what the row holds: legal is the record with the business name; public is the name, the
    // ticked legal fields, no phone
    const rowItem = await getGerpRow(gerp.gerp_id);
    expect(rowItem.legal.name).toBe("Pay With Co");
    expect(rowItem.public.name).toBe("Pay With Co");
    expect(rowItem.public.city).toBe(rowItem.legal.city);
    expect(rowItem.public.email).toBe(rowItem.legal.email);
    expect(rowItem.public.phone).toBeUndefined();
    const contact = await sellerContact(gerp.gerp_id);
    expect(contact.name).toBe("Pay With Co");                 // billed to the business, not a gerp id
    expect(contact.stripe_payment_method_id).toMatch(/^pm_/);
  } finally { await acct.cleanup(); }
});

test("adding a card at create opens a window and the create screen stays put", { tag: ["@local", "@mutating"] }, async ({ page, context }) => {
  test.skip(!isLocal(), "the Stripe stand-in");
  const acct = await fixtureAccount({ tag: "popup" });
  try {
    await login(page, acct);
    await page.locator('[data-view="homeScreen"] .card', { hasText: "Create a ∇ERP" }).click();
    await page.locator('[data-view="payWithAddCard"]').waitFor();
    await expect(page.locator('[data-view="payWithRow"]')).toHaveCount(0);   // no cards: add is the only row
    await page.locator("#bizname").fill("Popup Co");
    await pasteLegal(page);
    await page.locator('[data-view="purchaseAck"]').check();
    await page.locator('[data-view="payWithAddCard"] input').check();
    const [popup] = await Promise.all([context.waitForEvent("page"), page.locator('[data-view="createGerpBtn"]').click()]);
    await popup.waitForURL(/127\.0\.0\.1:4242\/c\//, { timeout: 15_000 });
    await expect(page.locator('[data-view="createGerpScreen"]')).toBeVisible();   // the opener did not navigate
    await expect(page.locator("#status")).toContainText("window that opened");
    await popup.locator("input[name=number]").fill("5555 5555 5555 4444");
    await popup.locator("button:has-text('Save card')").click();
    await expect(page.locator('[data-view="homeScreen"]')).toBeVisible({ timeout: 20_000 });
    await expect(page.locator("#status")).toContainText("Card saved");
    await expect.poll(() => popup.isClosed(), { timeout: 5_000 }).toBe(true);
    const contact = await sellerContact((await page.evaluate(async () => (await (await fetch("/api/gerps", { headers: {
      Authorization: "Bearer " + sessionStorage.getItem("id_token") } })).json()).gerps)).find(g => g.label === "Popup Co").gerp_id);
    expect(contact.name).toBe("Popup Co");
  } finally { await acct.cleanup(); }
});
