import { expect, test } from "@playwright/test";
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
  // every request the page starts, until it answers or fails — what a slow step was still waiting on
  const waiting = new Map();
  page.on("request", (r) => waiting.set(r, Date.now()));
  page.on("requestfinished", (r) => waiting.delete(r));
  page.on("requestfailed", (r) => waiting.delete(r));

  await page.goto("/");
  let t0 = Date.now();
  await page.getByRole("button", { name: /log in/i }).click();

  // Cognito Managed Login (v2, Cloudscape): single-rendered form. The inputs keep the
  // classic name= attrs (username/password); the submit is now a <button>Sign in</button>
  // (the classic input[name=signInSubmitButton] no longer exists).
  await within(page, waiting, page.locator('input[name="username"]'), LOGIN_PAGE_MS,
               "Cognito's login page did not render its form");
  const pageMs = Date.now() - t0;
  await page.locator('input[name="username"]').fill(email);
  await page.locator('input[name="password"]').fill(password);
  t0 = Date.now();
  await page.getByRole("button", { name: /^sign in$/i }).click();

  // back on the SPA, landed on the homeScreen (appbar shows the email)
  await within(page, waiting, page.locator('[data-view="homeScreen"]'), SIGN_IN_MS,
               "signing in did not land on the app's home screen");
  const signInMs = Date.now() - t0;
  test.info().annotations.push({ type: "login", description: `login page ${pageMs}ms, sign-in to home ${signInMs}ms` });
  console.log(`[e2e] login page ${pageMs}ms, sign-in to home ${signInMs}ms`);
  await expect(page.locator('[data-view="whoChip"]')).toContainText(email);
}

// What a person waits for at each step of signing in, before the step counts as failed.
export const LOGIN_PAGE_MS = 3_000;   // "Log in" clicked → Cognito's form on screen
export const SIGN_IN_MS = 5_000;      // "Sign in" clicked → the app's home screen, signed in

// Wait for `locator`, and when the budget runs out fail with what the page was still waiting on.
async function within(page, waiting, locator, ms, what) {
  const t0 = Date.now();
  try {
    await locator.waitFor({ state: "visible", timeout: ms });
  } catch {
    const now = Date.now();
    const open = [...waiting].map(([r, at]) => `  ${r.method()} ${r.url().split("?")[0]} — no response after ${now - at}ms`);
    throw new Error(`${what} within ${ms}ms (waited ${now - t0}ms, on ${page.url().split("?")[0]})` +
                    (open.length ? `\nrequests with no response:\n${open.join("\n")}` : "\nno request was waiting on a response"));
  }
}

// Enter a gerp from the gerps table by its label → lands on the gerpScreen.
export async function openGerp(page, label) {
  await page.locator('[data-view="gerpCard"]', { hasText: label }).first().click();
  await expect(page.locator('[data-view="gerpScreen"]')).toBeVisible({ timeout: 20_000 });
  // the screen says which gerp it is, and that it is a gerp's screen rather than the account's
  await expect(page.locator('[data-view="crumb"]')).toHaveText(`Your ∇ERPs›${label}`);
}
