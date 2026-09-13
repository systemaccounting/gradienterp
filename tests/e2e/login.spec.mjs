import { test, expect } from "@playwright/test";
import { login } from "./helpers/login.mjs";
import { getOwnerCreds } from "./helpers/aws.mjs";

// Full Hosted-UI login round-trip on the custom domain: SPA → Cognito Hosted UI →
// redirect to /auth/callback → PKCE token exchange → authenticated home. Read-only
// (no state mutation). Run with E2E_BASE_URL=https://gradienterp.cloud.
test("hosted-UI login lands authenticated on the app", { tag: ["@smoke"] }, async ({ page }) => {
  await login(page, await getOwnerCreds()); // asserts homeScreen + whoChip shows the signed-in email
  await expect(page.locator('[data-view="gerpsTable"]')).toBeVisible({ timeout: 20_000 });
});
