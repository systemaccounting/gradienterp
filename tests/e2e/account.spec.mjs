// account — Info & Billing, both halves of the round-trip.
//
// Object 2 in `TODO.md` § the object inventory: the private account profile on `gerp-accounts`.
// Signup seeds that row; this asserts the owner can EDIT it, that the edit reaches DDB, and that a
// reload reads it back. Write-only is the failure a screen cannot show you — a field that posts to a
// route that drops it looks identical to one that saved, until the next login.
//
// Two names change on the way down: the API speaks `first` / `last`, the row stores `first_name` /
// `last_name` (`_update_account`). Assert the DDB names — `getAccount()` returns the raw row.
//
// The Billing half is the account's DEFAULT card — what a gerp with no card of its own is billed
// to, and the only card settable without creating a gerp first. A gerp is still the billing
// subject; this is the fallback, so what it asserts is the panel reading the row and the vault ids
// staying off the wire.

import { test, expect } from "@playwright/test";
import { isLocal } from "./helpers/env.mjs";
import { fixtureAccount, getAccount, getSellerContact, putAccountCard } from "./helpers/account.mjs";
import { fixtureGerp, seedGerpRow, deleteGerpRow, getGerpRow } from "./helpers/aws.mjs";
import { testAddress, waitForMail, code } from "../mailbox/mailbox.mjs";
import { login } from "./helpers/login.mjs";

const FIRST = "Grace";
const LAST = "Hopper";

async function openInfoBilling(page, acct) {
  await login(page, { email: acct.email, password: acct.password, sub: acct.sub });
  await page.locator('[data-view="homeScreen"] .card', { hasText: "Info & Billing" }).click();
  await expect(page.locator('[data-view="accountInfoAndBillingScreen"]')).toBeVisible();
}

test.describe("account", () => {
  let acct = null;

  test.afterEach(async () => {
    if (acct) await acct.cleanup();
    acct = null;
  });

  test("the owner's name round-trips through the row and back onto the form",
    { tag: ["@smoke", "@mutating"] }, async ({ page }) => {
      acct = await fixtureAccount({ tag: "account" });
      await openInfoBilling(page, acct);

      await page.locator("#acct-first").fill(FIRST);
      await page.locator("#acct-last").fill(LAST);
      await page.getByRole("button", { name: /^save$/i }).click();
      await expect(page.locator("#status")).toContainText(/saved/i);

      // half one: it reached the store, under the column names the store uses
      await expect
        .poll(async () => await getAccount(acct.sub), { timeout: 20_000, message: "gerp-accounts row" })
        .toMatchObject({ account_id: acct.sub, first_name: FIRST, last_name: LAST });

      // half two: and it comes BACK. A write-only route passes the assertion above and fails here,
      // which is the whole reason this reloads rather than trusting the form it just typed into.
      await page.goto("/");
      await page.locator('[data-view="homeScreen"] .card', { hasText: "Info & Billing" }).click();
      await expect(page.locator("#acct-first")).toHaveValue(FIRST);
      await expect(page.locator("#acct-last")).toHaveValue(LAST);
    });

  test("email is the login and changes only through a code", { tag: ["@smoke"] }, async ({ page }) => {
    acct = await fixtureAccount({ tag: "account" });
    await openInfoBilling(page, acct);
    await expect(page.locator('[data-view="acctEmailInput"]')).toHaveValue(acct.email);
    // editable, but nothing changes on Save: the login moves only when a code confirms the new
    // address (tests below) — the name form's Save does not touch it
    await expect(page.locator('[data-view="acctEmailInput"]')).toBeEditable();
    await expect(page.locator('[data-view="changeEmailBtn"]')).toBeEnabled();
    await expect(page.locator('[data-view="emailCodeInput"]')).toHaveCount(0);
  });

  test("a save with a field blank is refused before it leaves the browser", { tag: ["@smoke"] }, async ({ page }) => {
    acct = await fixtureAccount({ tag: "account" });
    await openInfoBilling(page, acct);

    await page.locator("#acct-first").fill(FIRST);
    await page.locator("#acct-last").fill("");
    const posts = [];
    page.on("request", (r) => { if (r.url().includes("/api/account") && r.method() === "POST") posts.push(r); });
    await page.getByRole("button", { name: /^save$/i }).click();

    await expect(page.locator("#status")).toContainText(/required/i);
    expect(posts, "a refused save should not reach the route").toHaveLength(0);
  });

  test("a new account has no card, and the panel offers to add one", { tag: ["@smoke"] }, async ({ page }) => {
    acct = await fixtureAccount({ tag: "account" });
    await openInfoBilling(page, acct);

    await expect(page.locator('[data-view="acctCard"]')).toHaveText("none on file");
    // and the screen says whose billing this is — the account's, not a gerp's
    await expect(page.locator('[data-view="crumb"]')).toHaveText("Account›Info & Billing");
    // the point of the account default: a card settable WITHOUT creating a gerp first
    await expect(page.locator('[data-view="addCardBtn"]')).toHaveText("Add card");
    await expect(page.locator('[data-view="addCardBtn"]')).toBeEnabled();
  });

  test("a saved card renders from the row, and the ids never reach the browser",
    { tag: ["@smoke", "@mutating"] }, async ({ page }) => {
      acct = await fixtureAccount({ tag: "account" });
      await putAccountCard(acct.sub, { brand: "visa", last4: "4242", exp: "04/29" });
      await openInfoBilling(page, acct);

      await expect(page.locator('[data-view="acctCard"]')).toHaveText("Visa ····4242 · exp 04/29");
      // the panel is a list now — a saved card gets Default / Remove on its row, and the button
      // adds another rather than replacing
      await expect(page.locator('[data-view="addCardBtn"]')).toHaveText("Add card");

      // The vault ids are the account's credentials at Stripe. They live on the row so the fee can
      // be charged; a page that ships them has widened who can spend that card to anyone who reads
      // the response — and nothing on this screen needs them to say "Visa ····4242".
      const body = await page.evaluate(async () => {
        const r = await fetch("/api/account", { headers: {
          Authorization: "Bearer " + sessionStorage.getItem("id_token") } });
        return await r.text();
      });
      expect(body).not.toContain("cus_");
      expect(body).not.toContain("pm_");
      expect(body).not.toMatch(/stripe_/);
    });

  // The change-email flow, against whatever Cognito the env has: the stand-in locally (every code
  // is 000000), the real pool in prod — where the code is read off the gradienterp.cloud
  // catch-all, the way fixtureAccount reads its signup code. `test+…` addresses are stored to S3
  // and never forwarded, so nothing reaches a person.
  async function armSession(page, acct) {
    if (!isLocal()) return;   // prod: the Hosted-UI exchange stored all three tokens
    await page.evaluate((sub) => sessionStorage.setItem("refresh_token", "rt_" + sub), acct.sub);
    await page.evaluate(() => sessionStorage.setItem("access_token", sessionStorage.getItem("id_token")));
  }
  const nextAddress = () => isLocal() ? `changed-${Date.now()}@localhost` : testAddress("account");
  const codeFor = async (address, since) =>
    isLocal() ? "000000" : code(await waitForMail(address, { since, timeoutMs: 120_000 }));

  test("changing the login email: a code confirms it, the appbar and the row follow",
    { tag: ["@smoke", "@mutating"] }, async ({ page }) => {
      acct = await fixtureAccount({ tag: "account" });
      await openInfoBilling(page, acct);
      await armSession(page, acct);

      const next = nextAddress();
      const since = Date.now();
      await page.locator('[data-view="acctEmailInput"]').fill(next);
      await page.locator('[data-view="changeEmailBtn"]').click();
      await expect(page.locator("#status")).toContainText("code sent to " + next);

      await page.locator('[data-view="emailCodeInput"]').fill(await codeFor(next, since));
      await page.locator('[data-view="confirmEmailBtn"]').click();
      await expect(page.locator("#status")).toContainText("email changed", { timeout: 20_000 });
      await expect(page.locator('[data-view="acctEmailInput"]')).toHaveValue(next);
      await expect(page.locator('[data-view="whoChip"]')).toContainText(next);
      acct.email = next;   // the login moved; cleanup deletes by sub either way

      // the row followed the token, not the form
      await expect.poll(async () => (await getAccount(acct.sub))?.email, { timeout: 10_000 }).toBe(next);
    });

  test("a wrong code is refused and the login stays put", { tag: ["@smoke", "@mutating"] }, async ({ page }) => {
      acct = await fixtureAccount({ tag: "account" });
      await openInfoBilling(page, acct);
      await armSession(page, acct);
      await page.locator('[data-view="acctEmailInput"]').fill(nextAddress());
      await page.locator('[data-view="changeEmailBtn"]').click();
      await expect(page.locator("#status")).toContainText("code sent to");
      await page.locator('[data-view="emailCodeInput"]').fill("123456");
      await page.locator('[data-view="confirmEmailBtn"]').click();
      await expect(page.locator("#status")).toContainText("wrong code");
      expect((await getAccount(acct.sub))?.email).toBe(acct.email);
    });

  test("the full record round-trips, and a partial save names what is left", { tag: ["@smoke", "@mutating"] }, async ({ page }) => {
    acct = await fixtureAccount({ tag: "account", complete: false });
    await openInfoBilling(page, acct);
    await expect(page.locator('[data-view="accountGate"]')).toContainText("Still missing");

    await page.locator("#acct-first").fill("Ada");
    await page.locator("#acct-last").fill("Lovelace");
    await page.locator("#acct-phone").fill("+1 555 0100");
    await page.locator('[data-view="saveAcctBtn"]').click();
    await expect(page.locator("#status")).toContainText("still missing street address, city, region, postal code, country");

    for (const [k, v] of Object.entries({ street: "1 Analytical Way", city: "London", state: "LDN", zip: "N1", country: "GB" }))
      await page.locator("#acct-" + k).fill(v);
    // the click has feedback where it happened: while the save is in flight the button is
    // disabled and shows the spinner (the status line is below the fold); the answer is held a
    // moment so that state is observable
    let release, released;
    await page.route("**/api/account", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      released = new Promise((r) => { release = r; });
      await released;
      await route.continue();
    });
    const save = page.locator('[data-view="saveAcctBtn"]');
    await save.click();
    await expect(save).toBeDisabled();
    await expect(save.locator(".spin")).toHaveCount(1);
    release();
    await page.waitForResponse((r) => r.url().endsWith("/api/account") && r.request().method() === "POST");
    await page.unroute("**/api/account");
    await expect(page.locator("#status")).toContainText("your account is complete");
    await expect(save).toBeEnabled();
    await expect(save).toHaveText("Save");
    await expect(page.locator('[data-view="accountGate"]')).toHaveCount(0);

    await page.goto("/");
    await page.locator('[data-view="homeScreen"] .card', { hasText: "Info & Billing" }).click();
    await expect(page.locator("#acct-city")).toHaveValue("London");
    expect(await getAccount(acct.sub)).toMatchObject({ first_name: "Ada", last_name: "Lovelace",
      phone: "+1 555 0100", street: "1 Analytical Way", city: "London", state: "LDN", zip: "N1", country: "GB" });
    // the seller gerp's books: the BFF posted the record through the hook gradienterp published, and
    // the firm's script wrote the person as a customer contact keyed by the account id (local only:
    // the row is in the seller's account)
    if (isLocal())
      expect(await getSellerContact(acct.sub)).toMatchObject({ first_name: "Ada", last_name: "Lovelace",
        email: acct.email, is_customer: true });
  });

  test("an incomplete account is told so on both doors, and both refuse", { tag: ["@smoke", "@mutating"] }, async ({ page }) => {
    acct = await fixtureAccount({ tag: "account", complete: false });
    await login(page, acct);
    await page.locator('[data-view="homeScreen"] .card', { hasText: "Create a ∇ERP" }).click();
    await expect(page.locator('[data-view="createGerpScreen"] [data-view="accountGate"]')).toContainText("Complete your Account");
    const refused = await page.evaluate(async () => {
      const r = await fetch("/api/gerps", { method: "POST", headers: { Authorization: "Bearer " + sessionStorage.getItem("id_token"),
        "Content-Type": "application/json" }, body: JSON.stringify({ business_name: "Nope Co", terms_version: "x" }) });
      return { status: r.status, body: await r.json() };
    });
    expect(refused.status).toBe(409);
    expect(refused.body.missing).toContain("phone");

    await page.goto("/");
    await page.locator('[data-view="homeScreen"] .card', { hasText: "Public profile" }).click();
    await expect(page.locator('[data-view="publicProfileScreen"] [data-view="accountGate"]')).toContainText("Complete your Account");
    await page.locator("#pu-first").fill("Ada"); await page.locator("#pu-last").fill("Lovelace");
    await page.locator("#savepublic").click();
    await expect(page.locator("#status")).toContainText("complete your account first");
  });
});

// Deleting the account — the dialog, its gate, and what is left afterwards.
//
// Local only: the closed and the running gerp are seeded rows, and the route's last step deletes
// the Cognito user, which the local stack does not have. The route's own order (priors, rows, the
// card, the contact, the user) is unit-covered in tests/gradienterp_cloud; this is the screen.
test.describe("deleting the account", () => {
  let acct = null;
  let gerp = null;
  let closed = null;   // { sub, gerp_id } — the row the deletion keeps, which this spec then removes
  const PHRASE = "delete my account";

  test.skip(!isLocal(), "seeds gerp rows and deletes the account — local only");

  test.afterEach(async () => {
    if (gerp) await gerp.cleanup();
    if (closed) await deleteGerpRow(closed.sub, closed.gerp_id);
    if (acct) await acct.cleanup();
    gerp = null; closed = null; acct = null;
  });

  async function openDialog(page) {
    await openInfoBilling(page, acct);
    await page.locator('[data-view="deleteAccountBtn"]').click();
    const dialog = page.locator('[data-view="deleteDialog"]');
    await expect(dialog).toBeVisible();
    return dialog;
  }

  test("a running gerp is named in the dialog and the account stays", { tag: ["@smoke"] }, async ({ page }) => {
    acct = await fixtureAccount({ tag: "del" });
    gerp = await fixtureGerp(acct.sub, "Still Running Co");
    const dialog = await openDialog(page);
    // the copy says what goes and what stays
    await expect(dialog).toContainText("saved card");
    await expect(dialog).toContainText("tax law");
    const submit = page.locator('[data-view="deleteSubmitBtn"]');
    await expect(submit).toBeDisabled();
    await page.locator('[data-view="deleteConfirmInput"]').fill("delete my acount");
    await expect(submit).toBeDisabled();
    await page.locator('[data-view="deleteConfirmInput"]').fill(PHRASE);
    await expect(submit).toBeEnabled();

    const reply = page.waitForResponse((r) => r.url().endsWith("/api/account") && r.request().method() === "DELETE");
    await submit.click();
    expect((await reply).status()).toBe(409);
    await expect(page.locator('[data-view="deleteBlocked"]')).toContainText("Still Running Co");
    await expect(submit).toBeDisabled();
    expect(await getAccount(acct.sub), "nothing was deleted").not.toBeNull();
  });

  test("with every gerp closed, the phrase deletes the account and the closed gerp's row stays", { tag: ["@smoke"] }, async ({ page }) => {
    acct = await fixtureAccount({ tag: "del" });
    const gerp_id = `closed-co-${Math.floor(Math.random() * 1e6)}`;
    await seedGerpRow(acct.sub, { gerp_id, label: "Closed Co", status: "closed" });
    closed = { sub: acct.sub, gerp_id };
    await openDialog(page);
    await page.locator('[data-view="deleteConfirmInput"]').fill(PHRASE);

    const reply = page.waitForResponse((r) => r.url().endsWith("/api/account") && r.request().method() === "DELETE");
    await page.locator('[data-view="deleteSubmitBtn"]').click();
    expect((await reply).status()).toBe(200);

    expect(await getAccount(acct.sub), "the private record").toBeNull();
    expect((await getGerpRow(gerp_id)).status, "the seller's record of the gerp is kept").toBe("closed");
    const contact = await getSellerContact(acct.sub);
    if (contact) expect(contact.first_name).toBe("erased");
  });
});
