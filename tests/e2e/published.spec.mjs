import { test, expect } from "@playwright/test";
import { postJournalEntry } from "./helpers/aws.mjs";
import { isLocal } from "./helpers/env.mjs";

// The surface the owner client cannot see: openlyoperated.biz. A known fact is posted on the
// gerp's own ledger, and the page must show THAT number — sourced from api.openlyoperated.biz and
// nowhere else. A published field reading 0 looks like a quiet business, so the assertion is
// against the ledger truth, never "the endpoint answered".
const SITE = "https://openlyoperated.biz";
const API = "https://api.openlyoperated.biz/v1";
const GERP = "gradienterp";
const money = n => "$" + Math.round(n).toLocaleString("en-US");

test.skip(isLocal(), "the site and the api have no local image; this spec drives the live ones");

test("a posted entry reaches the economy card, the business page and its live channel through the api", { tag: ["@smoke", "@mutating"] }, async ({ page }) => {
  // every data request the page makes is the api or the stream (fonts come from Google; the page from the site)
  const foreign = [];
  page.on("request", r => { const u = new URL(r.url()); if (["fetch", "xhr", "websocket", "eventsource"].includes(r.resourceType()) && !["api.openlyoperated.biz", "events.openlyoperated.biz"].includes(u.hostname)) foreign.push(r.url()); });

  const before = (await (await fetch(`${API}/economy/counters?signal=revenue`)).json()).headline;
  const gerpBefore = (await (await fetch(`${API}/gerps/${GERP}/sources/metrics`)).json()).metrics.find(m => m.key === "revenue").headline;

  // the business page, subscribed to the gerp's channel
  await page.goto(`${SITE}/#b/${GERP}`);
  await expect(page.locator("#oo-live")).toContainText("subscribed", { timeout: 20_000 });

  const amount = 1 + Math.round(Math.random() * 400) / 100;   // a cent-distinct fact
  const entry = await postJournalEntry(GERP, { amount, memo: "published.spec" });
  expect(entry.entryId).toBeTruthy();

  // the channel delivers the entry to the page as it happens
  await expect(page.locator("#oo-live")).toContainText("cr sales revenue " + money(amount), { timeout: 30_000 });

  // the api's numbers move by the fact, on both sides of the line
  await expect.poll(async () => (await (await fetch(`${API}/economy/counters?signal=revenue`)).json()).headline.value, { timeout: 30_000 })
    .toBeCloseTo((before.value || 0) + amount, 2);
  await expect.poll(async () => (await (await fetch(`${API}/gerps/${GERP}/sources/metrics`)).json()).metrics.find(m => m.key === "revenue").headline.value, { timeout: 30_000 })
    .toBeCloseTo((gerpBefore.value || 0) + amount, 2);

  // the page renders what the api returns: the gerp's revenue card, then the economy's
  const gerpNow = (await (await fetch(`${API}/gerps/${GERP}/sources/metrics`)).json()).metrics.find(m => m.key === "revenue");
  await page.reload();
  await expect(page.locator('#oo-biz [data-metric="revenue"]')).toContainText(money(gerpNow.headline.value), { timeout: 20_000 });

  const economyNow = (await (await fetch(`${API}/economy/counters?signal=revenue`)).json());
  await page.goto(`${SITE}/`);
  await expect(page.locator('.oo-kpi [data-metric="revenue"]')).toContainText(money(economyNow.headline.value), { timeout: 20_000 });
  await expect(page.locator('.oo-kpi [data-metric="revenue"] code')).toHaveText(economyNow.source.curl);

  expect(foreign, "a data request to somewhere other than the api or the stream").toEqual([]);
});
