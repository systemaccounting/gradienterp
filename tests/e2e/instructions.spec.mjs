import { test, expect } from "@playwright/test";
import { login, openGerp } from "./helpers/login.mjs";
import { getOwnerCreds, getInstructions } from "./helpers/aws.mjs";

const GERP = "gradienterp";
const LABEL = "gradientERP";

// Standing instructions: the owner types a line on the gerp screen and the agent reads it on every
// turn. The browser half asserts the list renders and the X retires a row; the backend half asserts
// against the real INSTRUCTION# rows — the same ones the container Queries to build its prompt
// block, so a green run means the console and the agent are looking at one list.
//
// Adds a uniquely-suffixed line so a run never collides with the firm's real instructions, and
// removes it through the UI at the end — a leftover would be billed into every prompt after.
test("an instruction typed on the gerp screen lands in the agent's list, and the X retires it",
  { tag: ["@smoke", "@mutating"] }, async ({ page }) => {
    const text = `e2e probe ${Date.now()} — ignore this instruction`;

    await login(page, await getOwnerCreds());
    await openGerp(page, LABEL);

    const input = page.locator('[data-view="instrInput"]');
    await expect(input).toBeEnabled({ timeout: 20_000 });

    const before = await getInstructions(GERP);

    // type + Enter → status confirms → the real DDB rows carry it, appended last
    await input.fill(text);
    await input.press("Enter");
    await expect(page.locator("#status")).toContainText("instruction saved");
    await expect.poll(async () => getInstructions(GERP), { timeout: 15_000 })
      .toEqual([...before, text]);

    // and it renders in the list the owner reads back
    const row = page.locator(".setting.instr", { hasText: text });
    await expect(row).toBeVisible();

    // the X retires it — UI and backend both back to where they started
    await row.locator("button.x").click();
    await expect(page.locator("#status")).toContainText("instruction removed");
    await expect(row).toHaveCount(0);
    await expect.poll(async () => getInstructions(GERP), { timeout: 15_000 }).toEqual(before);
  });
