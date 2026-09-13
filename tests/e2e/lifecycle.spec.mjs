import { test, expect } from "@playwright/test";
import { login } from "./helpers/login.mjs";
import { getOwnerCreds, getGerpRow } from "./helpers/aws.mjs";
import { isLocal } from "./helpers/env.mjs";

// The gerp lifecycle on one account, driven from the owner app against prod. Step 1 here: the
// purchase. It logs in, completes the account record from SSM, fills Create a gerp from that
// record, and stops at Stripe's window for the card — the one field a script never types.
//
// Headed and interactive, in no smoke:* or full:* script:
//
//   cd tests/e2e && E2E_ENV=prod npx playwright test lifecycle.spec.mjs --headed --grep @lifecycle
//
// The owner is the e2e login (SSM /gradienterp/test/e2e/owner_*), or E2E_OWNER_EMAIL +
// E2E_OWNER_PASSWORD. Its account record is complete already — filled once on Info & Billing, the
// way a customer does — and the create screen pastes the legal profile from it. The gerp is named
// by E2E_GERP_LABEL (default westwood).
const LABEL = process.env.E2E_GERP_LABEL || "westwood";
test.skip(isLocal(), "the lifecycle drives the live owner app; the purchase screen's local case is purchase.spec");
test.setTimeout(30 * 60_000);

const token = page => page.evaluate(() => sessionStorage.getItem("id_token"));
const apiJson = (page, path, init) => page.evaluate(async ({ path, init }) => {
  const r = await fetch(path, { ...init, headers: { "content-type": "application/json", Authorization: "Bearer " + sessionStorage.getItem("id_token"), ...(init?.headers || {}) } });
  return { status: r.status, body: await r.json().catch(() => ({})) };
}, { path, init });

test("1. the purchase: the record, the screen, the card in Stripe's window", { tag: ["@lifecycle", "@mutating"] }, async ({ page, context }) => {
  const creds = process.env.E2E_OWNER_EMAIL
    ? { email: process.env.E2E_OWNER_EMAIL, password: process.env.E2E_OWNER_PASSWORD }
    : await getOwnerCreds();
  await login(page, creds);
  expect(await token(page)).toBeTruthy();

  // the account record is what the create screen's legal profile prefills from
  const acct = await apiJson(page, "/api/account");
  expect(acct.body.missing || [], "fill Info & Billing first; the legal profile prefills from it").toEqual([]);

  // Create a gerp
  await page.goto("/");
  await page.locator('[data-view="homeScreen"] .card', { hasText: "Create a ∇ERP" }).click();
  await expect(page.locator('[data-view="createGerpScreen"]')).toBeVisible();
  await page.locator("#bizname").fill(LABEL);
  await expect(page.locator("#biz-city")).not.toHaveValue("");   // prefilled from the account record
  await page.locator('[data-view="purchaseAck"]').check();
  await page.locator('[data-view="payWithAddCard"] input').check();
  await expect(page.locator('[data-view="createGerpBtn"]')).toBeEnabled();

  // Create: the row is written and Stripe's page opens in a new window. The card is yours.
  const [popup] = await Promise.all([context.waitForEvent("page"), page.locator('[data-view="createGerpBtn"]').click()]);
  await popup.waitForURL(/checkout\.stripe\.com|\/\?gerp=/, { timeout: 30_000 });
  const mine = async () => ((await apiJson(page, "/api/gerps")).body.gerps || []).find(g => g.label === LABEL) || null;
  await expect.poll(mine, { timeout: 20_000 }).not.toBeNull();
  const row = await mine();
  console.log(`\n  gerp ${row.gerp_id} is waiting on the card — enter it in the Stripe window, then Resume.\n`);

  // hands over to the person; the inspector's Resume continues
  await page.pause();

  // the card landed: the create screen heard it, and the row left awaiting_payment
  await expect(page.locator("#status")).toContainText(/being created|Card saved/, { timeout: 60_000 });
  await expect.poll(async () => (await getGerpRow(row.gerp_id)).status, { timeout: 60_000 }).not.toBe("awaiting_payment");
  const status = (await getGerpRow(row.gerp_id)).status;
  console.log(`\n  ${row.gerp_id}: ${status} — the vend is running (provision_queue set) or stubbed (unset).\n`);
});
