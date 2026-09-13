import { test, expect } from "@playwright/test";
import { login, openGerp } from "./helpers/login.mjs";
import { getOwnerCreds, fixtureGerp, getGerpRow } from "./helpers/aws.mjs";
import { isLocal } from "./helpers/env.mjs";

// Business info on the gerp screen: one form. The legal profile is the record; the public profile
// is the business name plus whichever legal fields are ticked under Public fields, plus links. The
// boxes follow Openly operated — off clears them and grays the card, on ticks them all — and a
// save re-derives the public map from the boxes, so the row's two maps are read back here.
test("the public profile is the ticked legal fields, and the boxes follow the switch", { tag: ["@smoke", "@mutating"] }, async ({ page }) => {
  test.skip(!isLocal(), "writes a fixture row; local only");
  const creds = await getOwnerCreds();
  const g = await fixtureGerp(creds.sub, "Boxes Co");
  try {
    await login(page, creds);
    await openGerp(page, g.label);
    const toggle = page.locator('[data-view="ooToggle"]');
    await expect(toggle).toBeEnabled({ timeout: 20_000 });
    if (!(await toggle.isChecked())) { await toggle.click(); await expect(page.locator("#status")).toContainText("openly operated"); }

    // the legal record, typed once; no legal name and no published name anywhere on the form
    await expect(page.locator("#biz-name")).toHaveCount(0);
    await expect(page.locator("#pub-name")).toHaveCount(0);
    await page.locator("#gi-label").fill("Boxes Co");
    for (const [k, v] of Object.entries({ email: "hello@boxes.example", phone: "+1 555 0199", street: "9 Crate Row",
                                            city: "Leeds", state: "LS", zip: "LS1", country: "UK" })) {
      await page.locator("#biz-" + k).fill(v);
    }
    // all eight boxes start ticked; untick phone and zip
    const box = (k) => page.locator(`[data-view="pubField"][data-field="${k}"] input`);
    for (const k of ["email", "phone", "street", "unit", "city", "state", "zip", "country"]) await expect(box(k)).toBeChecked();
    await box("phone").uncheck();
    await box("zip").uncheck();
    await page.locator('[data-view="saveGerpInfoBtn"]').click();
    await expect(page.locator("#status")).toContainText("saved");

    // the row: legal is the record under the business name; public is the name and the ticked fields
    const row = await getGerpRow(g.gerp_id);
    expect(row.legal).toMatchObject({ name: "Boxes Co", email: "hello@boxes.example", phone: "+1 555 0199", city: "Leeds", zip: "LS1" });
    expect(row.public).toMatchObject({ name: "Boxes Co", email: "hello@boxes.example", street: "9 Crate Row", city: "Leeds", state: "LS", country: "UK" });
    expect(row.public.phone).toBeUndefined();
    expect(row.public.zip).toBeUndefined();

    // reopened, the boxes read off the saved public profile
    await page.locator('[data-view="crumbRoot"]').click();
    await openGerp(page, "Boxes Co");
    await expect(page.locator("#gi-label")).toHaveValue("Boxes Co", { timeout: 15_000 });
    await expect(box("phone")).not.toBeChecked();
    await expect(box("zip")).not.toBeChecked();
    await expect(box("city")).toBeChecked();

    // the switch off: the card grays, every box clears; a save publishes the name alone
    await expect(toggle).toBeEnabled();
    await toggle.click();
    await expect(page.locator("#status")).toContainText("private");
    await expect(page.locator('[data-view="publicFields"]')).toHaveClass(/\boff\b/);
    for (const k of ["email", "street", "city"]) await expect(box(k)).not.toBeChecked();
    await page.locator('[data-view="saveGerpInfoBtn"]').click();
    await expect(page.locator("#status")).toContainText("saved");
    const off = await getGerpRow(g.gerp_id);
    expect(off.public).toMatchObject({ name: "Boxes Co" });
    expect(off.public.city).toBeUndefined();
    expect(off.legal.city).toBe("Leeds");   // the record is untouched by the switch

    // back on: every box ticks again and the card wakes
    await toggle.click();
    await expect(page.locator("#status")).toContainText("openly operated");
    await expect(page.locator('[data-view="publicFields"]')).not.toHaveClass(/\boff\b/);
    for (const k of ["email", "phone", "zip"]) await expect(box(k)).toBeChecked();
  } finally { await g.cleanup(); }
});
