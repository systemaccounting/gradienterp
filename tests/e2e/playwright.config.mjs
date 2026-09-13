import { defineConfig, devices } from "@playwright/test";
import { resolveEnv } from "./helpers/env.mjs";

// baseURL comes from the env preset (helpers/env.mjs): E2E_ENV=local|prod (default prod),
// or E2E_BASE_URL to override. See AGENTS.md for the run matrix (smoke/full × local/prod).
const { name, baseURL } = resolveEnv();
console.error(`[e2e] env=${name}  baseURL=${baseURL}`); // surface the target before the run (prod safety)

export default defineConfig({
  testDir: ".",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  // mutating specs touch shared live state — one seeded gerp, one owner — so they serialize.
  // `fullyParallel: false` only orders tests WITHIN a file; files still fan out across workers,
  // and two files on the same gerp is a flake on every run. One worker is the whole run.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
