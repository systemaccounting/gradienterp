import { test, expect } from "@playwright/test";
import { isLocal } from "./helpers/env.mjs";
import { testAddress, waitForMail, code } from "../mailbox/mailbox.mjs";
import { FIXTURE_PASSWORD, getAccount, lookupSub, deleteAccount, poolAttributes } from "./helpers/account.mjs";

// Signup through the SPA's own form, against the real pool: the name never goes to Cognito — it
// rides the confirm call as ClientMetadata and the post-confirmation trigger seeds the row from
// it. Prod only: the trigger is a Cognito-side hook the local stand-in does not run.
test("signing up seeds the row with the name from the confirm call, and the pool holds no name",
  { tag: ["@mutating"] }, async ({ page }) => {
    test.skip(isLocal(), "the post-confirmation trigger only runs in the real pool");
    const email = testAddress("signup");
    let sub;
    try {
      await page.goto("/");
      await page.getByText("Create an account").click();
      await expect(page.locator('[data-view="signupScreen"]')).toBeVisible();
      await page.locator("#su-first").fill("Ken");
      await page.locator("#su-last").fill("Barista");
      await page.locator("#su-email").fill(email);
      await page.locator("#su-pass").fill(FIXTURE_PASSWORD);
      const since = Date.now();
      await page.getByRole("button", { name: /create account|sign up/i }).click();
      await expect(page.locator('[data-view="confirmScreen"]')).toBeVisible({ timeout: 30_000 });

      await page.locator("#cf-code").fill(code(await waitForMail(email, { since, timeoutMs: 120_000 })));
      await page.getByRole("button", { name: /confirm & continue/i }).click();
      // a confirmed signup goes straight to the Hosted UI to sign in; reaching it IS the success
      await page.waitForURL(/amazoncognito\.com/, { timeout: 30_000 });

      sub = await lookupSub(email);
      expect(sub).toBeTruthy();
      await expect.poll(async () => await getAccount(sub), { timeout: 20_000 })
        .toMatchObject({ account_id: sub, email, first_name: "Ken", last_name: "Barista" });
      const pool = await poolAttributes(email);
      expect(pool.email).toBe(email);
      expect(pool.given_name).toBeUndefined();
      expect(pool.family_name).toBeUndefined();
    } finally {
      await deleteAccount({ email, sub });
    }
  });
