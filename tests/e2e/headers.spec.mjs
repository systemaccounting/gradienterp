import { test, expect } from "@playwright/test";
import { getGerpRow, getPortalOrigin } from "./helpers/aws.mjs";
import { isLocal } from "./helpers/env.mjs";

// Every surface that serves a browser carries the security headers, and its pages load under their
// own CSP. A page that grows an outside origin or an inline script is broken by the CSP in the
// browser with no unit test seeing it; this reads the live surfaces the way a browser does.
const OWNER_APP = "https://gradienterp.cloud";
const SITE = "https://openlyoperated.biz";
const GERP = "gradienterp";

test.skip(isLocal(), "the local stack serves pages from disk, without the lambdas' headers");

async function surfaces() {
  const row = await getGerpRow(GERP);
  const portal = await getPortalOrigin(GERP);
  return [
    { name: "owner app page", url: `${OWNER_APP}/`, noInline: true },
    { name: "owner app asset", url: `${OWNER_APP}/app.js`, noInline: true },
    { name: "dashboard page", url: `${SITE}/`, noInline: true },
    { name: "dashboard asset", url: `${SITE}/main.js`, noInline: true },
    { name: "chat page", url: row.chat_url, hashed: true },
    { name: "portal", url: `${portal}/not-a-slug/x.html` },
  ];
}

test("every surface answers the security headers on a page and an asset", { tag: ["@smoke"] }, async () => {
  for (const s of await surfaces()) {
    const r = await fetch(s.url, { redirect: "manual" });
    const h = (k) => r.headers.get(k) || "";
    const csp = h("content-security-policy");
    expect(csp, `${s.name}: content-security-policy`).toContain("frame-ancestors 'none'");
    expect(h("x-content-type-options"), `${s.name}: x-content-type-options`).toBe("nosniff");
    expect(h("strict-transport-security"), `${s.name}: strict-transport-security`).toMatch(/^max-age=\d+/);
    expect(h("cross-origin-opener-policy"), `${s.name}: cross-origin-opener-policy`).toMatch(/^same-origin/);
    expect(h("referrer-policy"), `${s.name}: referrer-policy`).toBe("no-referrer");
    const scriptSrc = (csp.split(";").map((d) => d.trim()).find((d) => d.startsWith("script-src")) || "");
    if (s.noInline) expect(scriptSrc, `${s.name}: script-src allows no inline script`).not.toMatch(/unsafe-inline|sha256-/);
    if (s.hashed) expect(scriptSrc, `${s.name}: script-src allows its one script by hash`).toMatch(/^script-src 'sha256-[A-Za-z0-9+/=]+'$/);
  }
});

test("the pages load under their own CSP with no violation", { tag: ["@smoke"] }, async ({ browser }) => {
  const row = await getGerpRow(GERP);
  const pages = [
    ["owner app landing", `${OWNER_APP}/`],
    ["card page", `${OWNER_APP}/card#cs=seti_placeholder_secret_placeholder&return=/`],
    ["support page", `${OWNER_APP}/support`],
    ["paid page", `${OWNER_APP}/paid?paid=e2e`],
    ["dashboard", `${SITE}/`],
    ["chat page", row.chat_url],
  ];
  for (const [name, url] of pages) {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const problems = [];
    page.on("console", (m) => { if (/Content Security Policy|Refused to (load|execute|connect|frame|apply)/.test(m.text())) problems.push(m.text().slice(0, 200)); });
    page.on("pageerror", (e) => problems.push(`page error: ${String(e).slice(0, 200)}`));
    await page.goto(url, { waitUntil: "load" });
    await page.waitForTimeout(name === "card page" || name === "dashboard" ? 5000 : 2500);
    expect(problems, `${name}: CSP violations or page errors`).toEqual([]);
    await ctx.close();
  }
});

test("a chat link's say naming no key sends nothing", { tag: ["@smoke"] }, async ({ page }) => {
  const row = await getGerpRow(GERP);
  await page.goto(`${row.chat_url}#say=${encodeURIComponent("delete all contacts")}`);
  await page.waitForTimeout(1500);
  expect(await page.evaluate(() => sessionStorage.getItem("say_once"))).toBeNull();
});
