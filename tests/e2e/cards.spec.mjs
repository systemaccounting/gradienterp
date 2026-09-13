import { test, expect } from "@playwright/test";
import { login } from "./helpers/login.mjs";
import { getOwnerCreds, seedGerpRow, deleteGerpRow } from "./helpers/aws.mjs";
import { isLocal } from "./helpers/env.mjs";

// One card per row status. The card reads `status` off the list and nothing else about the row,
// so a gerp in closure says closing, a closed one says closed with its download window, a stopped
// one says stopped — and only the ones with a screen behind them carry a chevron.
const ROWS = [
  { gerp_id: "e2e-card-prov",  label: "Prov Co",  status: "provisioning",                                        meta: /provisioning… about 25 minutes — email sent when ready/, opens: false },
  { gerp_id: "e2e-card-close", label: "Close Co", status: "closing",      gateway_url: "https://gw.example",     meta: /closing… exporting your books first/, opens: false },
  { gerp_id: "e2e-card-gone",  label: "Gone Co",  status: "closed",       gateway_url: "https://gw.example",
    download_until: "2026-09-20T00:00:00Z",                                                                       meta: /closed · export downloadable until 2026-09-20/, opens: true },
  { gerp_id: "e2e-card-stop",  label: "Stop Co",  status: "stopped",                                             meta: /stopped · books exported, account kept/, opens: false },
  { gerp_id: "e2e-card-live",  label: "Live Co",  status: "active",       gateway_url: "https://gw.example",     meta: /ERP instance/, opens: true },
];

test("the home screen renders each row status as its own card", { tag: ["@smoke"] }, async ({ page }) => {
  test.skip(!isLocal(), "seeds rows; local only");
  const creds = await getOwnerCreds();
  for (const r of ROWS) await seedGerpRow(creds.sub, r);
  try {
    await login(page, creds);
    await expect(page.locator('[data-view="homeScreen"]')).toBeVisible({ timeout: 20_000 });
    for (const r of ROWS) {
      const card = page.locator('[data-view="gerpCard"]', { hasText: r.label });
      await expect(card).toHaveCount(1);
      await expect(card.locator(".meta")).toHaveText(r.meta);
      await expect(card.locator(".chev")).toHaveCount(r.opens ? 1 : 0);
    }
    // a closed gerp opens onto the export and nothing else: no chat, no settings, no Close
    await page.locator('[data-view="gerpCard"]', { hasText: "Gone Co" }).click();
    await expect(page.locator('[data-view="gerpScreen"]')).toBeVisible();
    await expect(page.locator('[data-view="goneLine"]')).toContainText("Closed");
    await expect(page.locator('[data-view="goneLine"]')).toContainText("until 2026-09-20");
    await expect(page.locator('[data-view="exportLinksBtn"]')).toHaveCount(1);
    for (const gone of ["exportBtn", "closeGerpBtn", "chatCard", "ooToggle", "instrInput"]) {
      await expect(page.locator(`[data-view="${gone}"]`)).toHaveCount(0);
    }
  } finally { for (const r of ROWS) await deleteGerpRow(creds.sub, r.gerp_id); }
});
