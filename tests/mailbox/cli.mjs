#!/usr/bin/env node
// CLI half of the mailbox tool — see scripts/mailbox.sh for the wrapper.
//
// Exists so the tool is usable without a spec: a test calls the library, a human calls this, an
// agent calls this. Same reader either way.

import { waitForMail, listSince, fetch as fetchMsg, code, link, testAddress, DOMAIN } from "./mailbox.mjs";

const argv = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[i + 1] : dflt;
};
const has = (name) => argv.includes(`--${name}`);

function usage(exit = 0) {
  console.log(`mailbox — read inbound mail from the ${DOMAIN} catch-all

  --wait <addr>        block until a message for <addr> arrives, then print it
  --list               list recent messages (recipient · subject · when)
  --get <key>          print one message by S3 key
  --address [tag]      create an unforwarded test+ address and print it

  --code [digits]      with --wait/--get: print just the confirmation code (default 6)
  --link [match]       with --wait/--get: print just the first link (optionally matching)
  --subject <text>     with --wait: only match messages whose subject contains <text>
  --timeout <seconds>  with --wait: default 120
  --since <ms-epoch>   with --wait/--list: default now (wait) / 24h ago (list)

  AWS profile comes from MAILBOX_PROFILE (default operator-org).`);
  process.exit(exit);
}

if (!argv.length || has("help")) usage(argv.length ? 0 : 1);

const emit = (msg) => {
  if (has("code")) return console.log(code(msg, { digits: Number(opt("code", 6)) }));
  if (has("link")) return console.log(link(msg, { match: opt("link", undefined) }));
  console.log(`key:     ${msg.key}`);
  console.log(`from:    ${msg.from}`);
  console.log(`to:      ${msg.to}`);
  console.log(`subject: ${msg.subject}`);
  console.log("---");
  console.log((msg.text || msg.html || "").trim());
};

try {
  if (has("address")) {
    console.log(testAddress(opt("address", "e2e")));
  } else if (has("list")) {
    const since = Number(opt("since", Date.now() - 24 * 3600_000));
    const objs = await listSince(since);
    if (!objs.length) console.log("(no messages)");
    for (const o of objs.slice(0, Number(opt("limit", 20)))) {
      const m = await fetchMsg(o.Key);
      console.log(`${o.LastModified.toISOString()}  ${m.to.padEnd(34).slice(0, 34)}  ${m.subject.slice(0, 60)}`);
      console.log(`  ${o.Key}`);
    }
  } else if (has("get")) {
    emit(await fetchMsg(opt("get")));
  } else if (has("wait")) {
    emit(await waitForMail(opt("wait"), {
      since: Number(opt("since", Date.now())),
      timeoutMs: Number(opt("timeout", 120)) * 1000,
      subject: opt("subject", undefined),
    }));
  } else {
    usage(1);
  }
} catch (e) {
  console.error(String(e.message || e));
  process.exit(1);
}
