import { test, expect } from "@playwright/test";
import { login, openGerp } from "./helpers/login.mjs";
import { getOwnerCreds, getOpenlyOperated } from "./helpers/aws.mjs";

const GERP = "gradienterp";
const LABEL = "gradientERP";

// The canonical "mix browser with backend" case: toggle the checkbox in the UI, then read the real
// settings config table (GERP#openly_operated) and assert the flag flipped. Restores the original.
// (Post settings→DDB migration: the flag lives in DDB, not the SSM tenant blob.)
test("openly-operated toggle flips the gerp's settings DDB flag", { tag: ["@smoke", "@mutating"] }, async ({ page }) => {
  await login(page, await getOwnerCreds());
  await openGerp(page, LABEL);

  // settings: wait out the loading spinner (checkbox enabled once state is fetched)
  const toggle = page.locator('[data-view="ooToggle"]');
  await expect(toggle).toBeEnabled({ timeout: 20_000 });

  const before = await getOpenlyOperated(GERP);
  const want = !before;

  // flip in the UI → status confirms → the real DDB row reflects it
  await toggle.click();
  await expect(page.locator("#status")).toContainText(want ? "openly operated" : "private");
  await expect.poll(async () => getOpenlyOperated(GERP), { timeout: 15_000 }).toBe(want);

  // restore to the original value (don't leave the dogfood's publishing state changed)
  await toggle.click();
  await expect.poll(async () => getOpenlyOperated(GERP), { timeout: 15_000 }).toBe(before);
});
