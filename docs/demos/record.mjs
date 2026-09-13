// Record a login-page demo: drive the LIVE agent chat with Playwright and capture the page to
// a .webm. Playwright types via CDP (real per-character key events with a delay) so the
// recording shows genuine typing. See AGENTS.md — do NOT try OS keystroke injection.
//
//   node docs/demos/record.mjs --step 1     # accounting · CFO
//   node docs/demos/record.mjs --step 2     # contacts · CMO
//   node docs/demos/record.mjs --step 3     # labor · COO
//   node docs/demos/record.mjs --step 4     # treasury · BOARD
//
// Fresh context per run (clean session, no sidebar history); one webm per page. The chat page's
// video is renamed demo-<module>.webm under docs/demos/out/; the dashboard page's is dropped.
// Login creds come from SSM via the e2e aws helper (operator-org profile). ffmpeg → gif.

import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { rename, unlink, mkdir } from "node:fs/promises";
import { execSync } from "node:child_process";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));   // docs/demos
const REPO = path.resolve(HERE, "..", "..");                 // repo root — no machine-specific path
const require = createRequire(path.join(REPO, "tests/e2e/package.json"));
const { chromium } = require("playwright-core");
const { getOwnerCreds } = await import(path.join(REPO, "tests/e2e/helpers/aws.mjs"));

const STEPS = {
  1: { mod: "accounting", prompt: "prepare my monthly statements" },
  2: { mod: "contacts",   prompt: "who are our top 5 customers and how much did they spend?" },
  3: { mod: "labor",      prompt: "schedule the FOH for the next 2 weeks" },
  4: { mod: "treasury",   prompt: "Westwood Investments is putting in 500k for 10% of monthly net income until they've drawn 550k. the funds just cleared, set it up" },
  // section 2 — the INVESTOR's side: Westwood bids to buy a rule off Tanners' margin. The recorder
  // puppets Tanners' acceptance on the rail between the two turns (see playTanners). Filmed AS
  // Westwood — the header swap is cosmetic, the gerp underneath is the tenant, so "us" in the
  // prompt is the caller and the seller is a puppet gerp.
  5: { mod: "invest", business: "Westwood Investments",
       turns: ["Tanners Coffee Co earned a 20% gross margin over the past quarter — bid 500k to buy a rule paying us 10% of their monthly net income until we've drawn 550k",
               "did they take it?"],
       puppetAfter: 0 },
  // section 1 · CFO — two prompts: the books keep themselves, then the analyst room. Prompt 2
  // consumes prompt 1's statements alongside the exports in the cabinet (analysis_fixtures.py).
  7: { mod: "analysis",
       turns: ["prepare my monthly statements",
               "now go through the files in our storage — where are we leaking money? top three, one line each"] },
  // section 2 — supply chain: Tanners orders across the firm boundary; the recorder puppets Blue
  // Ridge's acceptance on the rail between the two turns (see playBlueRidge).
  // the barista just REPORTS the count; the agent books the variance, reads the item's par off its
  // reorder rule, and offers the PO. Turn 2 names the vendor → the cross-firm create_po fires, and
  // the recorder puppets Blue Ridge's acceptance before turn 3 asks.
  6: { mod: "reorder",
       turns: ["we're down to 5 bags of espresso beans",
               "yeah, Blue Ridge Roasters",
               "did they take it? when's it getting here?"],
       puppetAfter: 1 },
};

const stepArg = process.argv[process.argv.indexOf("--step") + 1];
const step = STEPS[stepArg];
if (!step) { console.error("usage: node docs/demos/record.mjs --step <1-7> [--probe]"); process.exit(1); }
const PROBE = process.argv.includes("--probe");   // login → chat → inject a mock answer → screenshot; no agent turn

const OUT = path.join(HERE, "out");
const TYPE_DELAY = 85;   // ms/char — visible human typing in the screencast
const DEMO_BUSINESS = "Tanners Coffee Co";   // header name shown in the demo (personalization; recorder-only)
await mkdir(OUT, { recursive: true });

// ── face-A puppet: I play the vendor (Blue Ridge) on the rail ──
// The cafe's create_po stamps a (thread, terms_hash) agreement row and awaits the seller. Between
// the order turn and the "did they accept?" turn, read that fresh row and send `po.accepted` as
// Blue Ridge — the same cross-firm handshake the manual proof ran, driven from inside the take.
const CUST_PROFILE = "gerp-gradienterp";      // the cafe (gradienterp) sub-account
const OP_ACCT = "185369506315";                           // the operator account that owns the bus
// The SHARED negotiation store (modules/agreements) — every kind's rows live here; the
// AGREEMENT#<kind> config rows carry no stamps, so the fresh-row filter skips them by name.
const AGREEMENTS_TABLE = "gerp-agreements-gradienterp";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function agreementRows() {
  const out = execSync(
    `aws dynamodb scan --table-name ${AGREEMENTS_TABLE} --no-cli-pager --profile ${CUST_PROFILE} --region us-east-1 --output json`,
    { encoding: "utf8" });
  return JSON.parse(out).Items.map((it) => ({
    thread: it.thread?.S, th: it.terms_hash?.S,
    bstamp: Number(it.buyer_stamp?.N || 0), sstamp: it.seller_stamp?.N,
  }));
}

async function playBlueRidge(knownThreads) {
  // the PO the order turn just created — a pending row (no seller stamp) on a thread new this take
  let row;
  for (let i = 0; i < 12 && !row; i++) {
    const fresh = agreementRows().filter((r) => !r.sstamp && !knownThreads.has(r.thread) && !r.thread?.startsWith("AGREEMENT#"));
    if (fresh.length) row = fresh.sort((a, b) => b.bstamp - a.bstamp)[0];
    else await sleep(1000);
  }
  if (!row) throw new Error("puppet: no pending PO row appeared after the order turn");
  const send = (type, detail) => execSync(
    `bash scripts/puppet.sh --acctid ${OP_ACCT} --send --as blue-ridge-roasters --to gradienterp --type ${type} --detail '${JSON.stringify(detail)}'`,
    { cwd: REPO, encoding: "utf8", stdio: "inherit" });

  send("po.accepted", { thread: row.thread, terms_hash: row.th });
  console.log(`puppet: Blue Ridge accepted PO ${row.thread}`);

  // ...and dispatches it. The delivery schedule is THEIRS to state — it crosses the rail as
  // shipment.sent and opens our inbound custody row with their ETA.
  const eta = new Date(Date.now() + 2 * 86400_000).toISOString().slice(0, 10);
  await sleep(2500);                       // let the acceptance land before the dispatch does
  send("shipment.sent", { thread: row.thread, carrier: "ups", tracking: "1Z999AA10123456784",
                          expected: eta, description: "7 bags espresso beans" });
  console.log(`puppet: Blue Ridge shipped, ETA ${eta}`);
}

// ── face-B puppet: I play the target firm (Tanners) on the rail ──
// The investor's propose_offer stamps a (thread, terms_hash) row as BUYER and awaits the seller.
// Between the bid turn and the "did they take it?" turn, read that fresh row and send
// `offer.accepted` as Tanners — the counterparty's stamp is theirs to give, so the take never
// settles its own bid. Same handshake as the vendor above, different substrate.
//
// No `puppet-` prefix on the id: that prefix only matters for CAPTURING events addressed to a
// puppet, and nothing here captures. Like playBlueRidge, this reads the tenant's own agreements
// table to find the row the bid just created, then emits the acceptance inbound. So the
// counterparty id is simply whatever the agent composed from the name in the prompt.
// The SHARED negotiation store (modules/agreements) — every kind's rows live here; the
// AGREEMENT#<kind> config rows also carry no stamps, so the fresh-row filter skips them by name.
const TREASURY_AGREEMENTS = "gerp-agreements-gradienterp";

function treasuryRows() {
  const out = execSync(
    `aws dynamodb scan --table-name ${TREASURY_AGREEMENTS} --no-cli-pager --profile ${CUST_PROFILE} --region us-east-1 --output json`,
    { encoding: "utf8" });
  return JSON.parse(out).Items.map((it) => ({
    thread: it.thread?.S, th: it.terms_hash?.S,
    bstamp: Number(it.buyer_stamp?.N || 0), sstamp: it.seller_stamp?.N,
  }));
}

async function playTanners(knownThreads) {
  let row;
  for (let i = 0; i < 12 && !row; i++) {
    const fresh = treasuryRows().filter((r) => !r.sstamp && !knownThreads.has(r.thread) && !r.thread?.startsWith("AGREEMENT#"));
    if (fresh.length) row = fresh.sort((a, b) => b.bstamp - a.bstamp)[0];
    else await sleep(1000);
  }
  if (!row) throw new Error("puppet: no pending bid row appeared after the bid turn");
  execSync(
    `bash scripts/puppet.sh --acctid ${OP_ACCT} --send --as tanners_coffee_co --to gradienterp --type offer.accepted --detail '${JSON.stringify({ thread: row.thread, terms_hash: row.th })}'`,
    { cwd: REPO, encoding: "utf8", stdio: "inherit" });
  console.log(`puppet: Tanners accepted the bid on ${row.thread}`);
}

const { email, password } = await getOwnerCreds();

const browser = await chromium.launch({ headless: false });
const context = await browser.newContext({
  baseURL: "https://gradienterp.cloud",
  // Frame in-camera (see AGENTS.md § recording config): ~880 wide ≈ the 760px bubble + margin, so
  // the answer fills the frame and no gif crop is needed; 720 tall packs the composer under the
  // answer. recordVideo = viewport × deviceScaleFactor (same aspect) → crisp, full-frame, no crop.
  viewport: { width: 880, height: 720 },
  deviceScaleFactor: 2,
  recordVideo: { dir: OUT, size: { width: 1760, height: 1440 } },
});

const page = await context.newPage();
const shot = (name) => page.screenshot({ path: path.join(OUT, `dbg-${name}.png`) }).catch(() => {});

try {
  // ── login (Cognito Managed Login) ──
  console.log("login…");
  await page.goto("/");
  await page.getByRole("button", { name: /log in/i }).click();
  // Cognito Managed Login renders the form twice (responsive) and hides one per breakpoint — target
  // the VISIBLE copy or a fill can land in the hidden one (same as tests/e2e/helpers/login.mjs).
  await page.locator('input[name="username"]:visible').fill(email);
  await page.locator('input[name="password"]:visible').fill(password);
  await page.getByRole("button", { name: /^sign in$/i }).click();
  await page.waitForURL(/gradienterp\.cloud\/?($|\?)/, { timeout: 30_000 });
  await page.waitForTimeout(2500);

  // ── enter the gerp, open chat (opens a new page/tab) ──
  console.log("open gerp…");
  await page.locator('[data-view="gerpCard"]').first().click();

  console.log("open chat…");
  // [data-view="chatCard"] is only present on the clickable variant (once chat_url loads), so
  // waiting for it fixes the race where an early click on the "loading…" card is a no-op.
  const chatCard = page.locator('[data-view="chatCard"]');
  await chatCard.waitFor({ timeout: 30_000 });
  const [chat] = await Promise.all([
    context.waitForEvent("page", { timeout: 30_000 }),
    chatCard.click(),
  ]);
  await chat.waitForLoadState("domcontentloaded");
  await chat.locator("#in").waitFor({ timeout: 20_000 });

  // collapse the saved-chats sidebar so the frame is just the conversation, and personalize the
  // header: the live chat shows the caller's login email (the dogfood address); for the demo we
  // show a business name instead. Recorder-only — no backend change.
  await chat.evaluate((biz) => {
    const b = [...document.querySelectorAll("button")].find((el) => el.textContent.trim() === "☰");
    if (b && !document.querySelector(".sidebar.collapsed")) b.click();
    const role = document.querySelector("header .role");
    if (role) role.textContent = `· ${biz}`;
  }, step.business || DEMO_BUSINESS);
  await chat.waitForTimeout(600);

  // ── probe: inject a mock exchange and screenshot the framing (no agent turn) ──
  if (PROBE) {
    await chat.evaluate(() => {
      const log = document.querySelector("#log");
      const add = (cls, html) => { const d = document.createElement("div"); d.className = cls; d.innerHTML = html; log.append(d); };
      const link = (n) => `<a href="#">statements/2026-07-21/${n}-2026-07-21.csv</a>`;
      log.innerHTML = "";
      add("msg me", "prepare my monthly statements");
      ["pulling your balances", "reading your orders"].forEach((t) => add("tool", t));
      add("msg agent",
        "done, july statements are in s3\n\n" +
        "income statement · net income <b>$6,850</b> · " + link("income-statement") + "\n\n" +
        "balance sheet · total assets <b>$50,994</b> · " + link("balance-sheet") + "\n\n" +
        "cash flow · net cash change <b>$22,594</b> · " + link("cash-flow") + "\n\n" +
        "owners equity · total equity <b>$38,294</b> · " + link("owners-equity") + "\n\n" +
        "trial balance · total debits <b>$136,294</b> (balanced) · " + link("trial-balance") + "\n\n" +
        "want me to email these?");
      log.scrollTop = log.scrollHeight;
    });
    await chat.waitForTimeout(400);
    await chat.screenshot({ path: path.join(OUT, "probe.png") });
    await context.close();
    await browser.close();
    console.log(`probe → ${path.join(OUT, "probe.png")}`);
    process.exit(0);
  }

  // ── the take: type each prompt as real keystrokes, send, wait for the answer ──
  const input = chat.locator("#in");
  const sendTurn = async (prompt, firstBeat) => {
    await input.click();
    await chat.waitForTimeout(firstBeat ? 1800 : 900);   // a beat before typing (longer on turn 1)
    await input.pressSequentially(prompt, { delay: TYPE_DELAY });
    await chat.waitForTimeout(500);
    await input.press("Enter");
    // the composer disables while the turn streams and re-enables when done
    await chat.locator("#in[disabled]").waitFor({ timeout: 10_000 }).catch(() => {});
    await chat.locator("#in:not([disabled])").waitFor({ timeout: 240_000 });
    await chat.waitForTimeout(1800);   // hold on the finished answer
  };

  if (step.turns) {
    // multi-turn with a puppet action wedged in — snapshot pre-existing threads so the puppet can
    // spot the one THIS take creates, then play the counterparty between the right turns. The two
    // faces sit on different substrates: a PO is a purchasing agreement, a bid is a treasury one.
    const snapshot = step.mod === "invest" ? treasuryRows : agreementRows;
    const known = new Set(snapshot().map((r) => r.thread));
    for (let i = 0; i < step.turns.length; i++) {
      console.log(`type: ${step.turns[i]}`);
      await sendTurn(step.turns[i], i === 0);
      if (step.puppetAfter === i && step.mod === "invest") {
        await chat.waitForTimeout(800);
        await playTanners(known);
        await chat.waitForTimeout(5000);   // let the acceptance land before the next turn asks
      } else if (step.puppetAfter === i) {
        await chat.waitForTimeout(800);
        await playBlueRidge(known);
        await chat.waitForTimeout(5000);   // let the acceptance land before the next turn
      }
    }
    await chat.waitForTimeout(2500);
  } else {
    console.log(`type: ${step.prompt}`);
    await sendTurn(step.prompt, true);
    await chat.waitForTimeout(2500);   // hold on the finished answer
  }

  const chatVideo = chat.video();
  const dashVideo = page.video();    // the dashboard page's webm — a throwaway
  await context.close();
  await browser.close();

  if (dashVideo) await unlink(await dashVideo.path()).catch(() => {});
  if (chatVideo) {
    const dst = path.join(OUT, `demo-${step.mod}.webm`);
    await rename(await chatVideo.path(), dst);
    console.log(`saved ${dst}`);
  }
} catch (e) {
  await shot("error");
  console.error("FAILED:", e.message, `(see ${OUT}/dbg-error.png)`);
  await context.close().catch(() => {});
  await browser.close().catch(() => {});
  process.exit(1);
}
