import { test, expect } from "@playwright/test";
import { login } from "./helpers/login.mjs";
import { getOwnerCreds, seedGerpRow, deleteGerpRow } from "./helpers/aws.mjs";
import { isLocal } from "./helpers/env.mjs";
import { DynamoDBClient, UpdateItemCommand } from "@aws-sdk/client-dynamodb";
import { resolveEnv } from "./helpers/env.mjs";

// The link in the ready mail: gradienterp.cloud/?gerp=<id>&open=chat. With a session it lands on
// the gerp's chat door with the token on the fragment; without one it goes through sign-in and
// then lands there (the stash survives the round trip — the Cognito half has no local form).
test("a chat deep link with a session lands on the chat door with the token", { tag: ["@smoke"] }, async ({ page }) => {
  test.skip(!isLocal(), "the chat door is a live Function URL; local asserts the navigation only");
  const creds = await getOwnerCreds();
  const g = { gerp_id: "e2e-deeplink", label: "Deeplink Co", chat_url: "https://chat.example/door" };
  await seedGerpRow(creds.sub, { gerp_id: g.gerp_id, label: g.label, status: "active", gateway_url: "https://gw.example" });
  const ddb = new DynamoDBClient({ region: "us-east-1", ...resolveEnv().aws });
  await ddb.send(new UpdateItemCommand({ TableName: "gerp-customers", Key: { gerp_id: { S: g.gerp_id } },
    UpdateExpression: "SET chat_url = :c", ExpressionAttributeValues: { ":c": { S: g.chat_url } } }));
  try {
    await login(page, creds);
    // the door is off-stack; hold it so the navigation can be read without leaving the emulator
    await page.route(g.chat_url + "*", route => route.fulfill({ status: 200, body: "chat door" }));
    await page.goto("/?gerp=" + encodeURIComponent(g.gerp_id) + "&open=chat");
    await page.waitForURL(u => u.href.startsWith(g.chat_url) && u.hash.startsWith("#id_token="), { timeout: 20_000 });
    expect(page.url()).toContain("#id_token=");
    expect(page.url()).not.toContain("say=");
    // the ready mail's link: the key rides to the door's fragment, and the door holds the words
    await page.goto("/?gerp=" + encodeURIComponent(g.gerp_id) + "&open=chat&say=onboard");
    await page.waitForURL(u => u.href.startsWith(g.chat_url) && u.hash.includes("say="), { timeout: 20_000 });
    expect(page.url()).toContain("&say=onboard");
    expect(decodeURIComponent(page.url())).not.toContain("onboard my business");
    // a key the console does not know carries nothing
    await page.goto("/?gerp=" + encodeURIComponent(g.gerp_id) + "&open=chat&say=drop%20tables");
    await page.waitForURL(u => u.href.startsWith(g.chat_url) && u.hash.startsWith("#id_token="), { timeout: 20_000 });
    expect(page.url()).not.toContain("say=");
  } finally { await deleteGerpRow(creds.sub, g.gerp_id); }
});
