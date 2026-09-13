// Closing a gerp — the dialog, and the two ways out of it.
//
// This is the most destructive control the owner app has: it exports the firm's records and then
// destroys the instance. So what is asserted here is mostly the GATE — that the button does nothing
// until someone has typed the phrase, that Cancel leaves no trace, and that the request carrying
// the phrase is what the server actually gets.
//
// The teardown itself is NOT exercised, and cannot be: `close_build_project` is empty by default,
// so the route records `close_requested` on the row and starts no build. That is the same off-by-
// default shape as `provision_queue`. A spec that could really close a gerp would be a spec that
// destroys the dogfood the first time someone runs the suite by accident.

import { test, expect } from "@playwright/test";
import { fixtureAccount } from "./helpers/account.mjs";
import { fixtureGerp, getGerpRow } from "./helpers/aws.mjs";
import { login } from "./helpers/login.mjs";
import { isLocal } from "./helpers/env.mjs";

const PHRASE = "I understand";

async function openGerpScreen(page, acct, gerp) {
  await login(page, { email: acct.email, password: acct.password, sub: acct.sub });
  await page.locator('[data-view="gerpCard"]', { hasText: gerp.label }).click();
  await expect(page.locator('[data-view="gerpScreen"]')).toBeVisible({ timeout: 20_000 });
}

test.describe("closing a gerp", () => {
  let acct = null;
  let gerp = null;

  // A MOCKED gerp — two operator-side rows, the same two `_create_gerp` writes, minus the vend.
  // Never gradienterp: this spec drives the control whose whole job is destruction.
  test.skip(!isLocal(), "writes gerp rows and drives a destructive control — local only");

  test.beforeEach(async () => {
    acct = await fixtureAccount({ tag: "close" });
    gerp = await fixtureGerp(acct.sub, "Closing Co");
  });

  test.afterEach(async () => {
    if (gerp) await gerp.cleanup();
    if (acct) await acct.cleanup();
    gerp = null; acct = null;
  });

  test("the dialog will not fire until the phrase is typed", { tag: ["@smoke"] }, async ({ page }) => {
    await openGerpScreen(page, acct, gerp);
    await page.locator('[data-view="closeGerpBtn"]').click();

    const dialog = page.locator('[data-view="closeDialog"]');
    await expect(dialog).toBeVisible();
    // it says what actually happens, in both directions — the copy is the disclosure
    await expect(dialog).toContainText("exported to you first");
    await expect(dialog).toContainText("15 days");
    await expect(dialog).toContainText("cannot be brought back");

    const submit = page.locator('[data-view="closeSubmitBtn"]');
    await expect(submit).toBeDisabled();

    // a near miss is still a miss
    await page.locator('[data-view="closeConfirmInput"]').fill("i understand");
    await expect(submit).toBeDisabled();

    await page.locator('[data-view="closeConfirmInput"]').fill(PHRASE);
    await expect(submit).toBeEnabled();
  });

  test("Cancel dismisses it and changes nothing", { tag: ["@smoke"] }, async ({ page }) => {
    await openGerpScreen(page, acct, gerp);
    await page.locator('[data-view="closeGerpBtn"]').click();
    // type the phrase FIRST — cancelling from a ready dialog is the case worth pinning
    await page.locator('[data-view="closeConfirmInput"]').fill(PHRASE);
    await expect(page.locator('[data-view="closeSubmitBtn"]')).toBeEnabled();

    const posts = [];
    page.on("request", (r) => { if (r.url().includes("/api/gerps/close")) posts.push(r); });
    await page.locator('[data-view="closeCancelBtn"]').click();

    await expect(page.locator('[data-view="closeDialog"]')).toHaveCount(0);
    expect(posts, "Cancel must not reach the route").toHaveLength(0);
    // and the row is untouched — no half-closed state left behind
    const row = await getGerpRow(gerp.gerp_id);
    expect(row.status ?? "").not.toContain("clos");

    // reopening starts empty: a ready dialog must not survive being dismissed
    await page.locator('[data-view="closeGerpBtn"]').click();
    await expect(page.locator('[data-view="closeConfirmInput"]')).toHaveValue("");
    await expect(page.locator('[data-view="closeSubmitBtn"]')).toBeDisabled();
  });

  test("Submit sends the phrase and records the request", { tag: ["@smoke", "@mutating"] }, async ({ page }) => {
    await openGerpScreen(page, acct, gerp);
    await page.locator('[data-view="closeGerpBtn"]').click();
    await page.locator('[data-view="closeConfirmInput"]').fill(PHRASE);

    const [req] = await Promise.all([
      page.waitForRequest((r) => r.url().includes("/api/gerps/close") && r.method() === "POST"),
      page.locator('[data-view="closeSubmitBtn"]').click(),
    ]);
    const body = JSON.parse(req.postData());
    expect(body.gerp_id).toBe(gerp.gerp_id);
    expect(body.confirm).toBe(PHRASE);

    // the dialog closes and the owner is told what did and did not happen. `teardown: false` —
    // closure is switched off — so the message must not claim an instance was destroyed.
    await expect(page.locator('[data-view="closeDialog"]')).toHaveCount(0);
    await expect(page.locator("#status")).toContainText(/nothing has been torn down/i);

    // the request is on the row, which is the whole of what an unarmed closure does
    await expect
      .poll(async () => (await getGerpRow(gerp.gerp_id)).status, { timeout: 10_000 })
      .toBe("close_requested");
  });
});
