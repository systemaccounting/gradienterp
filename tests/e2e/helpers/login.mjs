import { expect } from "@playwright/test";
import { isLocal } from "./env.mjs";

// An unsigned JWT. Nothing local verifies one: the SPA decodes the payload for display, and the bff
// image decodes the bearer's claims because production's APIGW authorizer — the thing that DOES
// verify — has no local equivalent. Reproducing a signature would only be reproducing AWS.
export function localIdToken({ sub, email }) {
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none", typ: "JWT" })}.${b64({ sub, email, token_use: "id" })}.local`;
}

// Drive the Cognito login. The SPA's "Log in" → Cognito Managed Login (email+password) →
// redirect back to /auth/callback?code=… → the SPA does the PKCE token exchange → home.
// The password is fetched from SSM by the caller and typed by Playwright — it's never in
// the repo and never handled by a human.
export async function login(page, { email, password, sub }) {
  if (isLocal()) {
    // The SPA holds its session in sessionStorage and the PKCE callback is what puts it there.
    // Seed it before first paint and the app boots logged in, exercising every screen after the
    // login gate — which is the half a local run is for.
    const token = localIdToken({ sub: sub || process.env.E2E_LOCAL_SUB || email, email });
    await page.addInitScript((t) => sessionStorage.setItem("id_token", t), token);
    await page.goto("/");
    await expect(page.locator('[data-view="homeScreen"]')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('[data-view="whoChip"]')).toContainText(email);
    return;
  }
  await page.goto("/");
  await page.getByRole("button", { name: /log in/i }).click();

  // Cognito Managed Login (v2, Cloudscape): single-rendered form. The inputs keep the
  // classic name= attrs (username/password); the submit is now a <button>Sign in</button>
  // (the classic input[name=signInSubmitButton] no longer exists).
  await page.locator('input[name="username"]').fill(email);
  await page.locator('input[name="password"]').fill(password);
  await page.getByRole("button", { name: /^sign in$/i }).click();

  // back on the SPA, landed on the homeScreen (appbar shows the email)
  await expect(page.locator('[data-view="homeScreen"]')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[data-view="whoChip"]')).toContainText(email);
}

// Enter a gerp from the gerps table by its label → lands on the gerpScreen.
export async function openGerp(page, label) {
  await page.locator('[data-view="gerpCard"]', { hasText: label }).first().click();
  await expect(page.locator('[data-view="gerpScreen"]')).toBeVisible({ timeout: 20_000 });
  // the screen says which gerp it is, and that it is a gerp's screen rather than the account's
  await expect(page.locator('[data-view="crumb"]')).toHaveText(`Your ∇ERPs›${label}`);
}
