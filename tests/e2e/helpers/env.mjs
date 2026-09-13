// Named env presets → what the suite drives and what it asserts against.
//   local = the images from `scripts/local-dev.sh` (bff :3000, per_customer :8080) over moto :5000
//   prod  = the live custom domain and the live AWS accounts
// E2E_ENV picks a preset (default `prod`); E2E_BASE_URL overrides the UI url for one-offs.
//
// `local` is a REAL end-to-end: the browser drives the real SPA, the BFF runs the real handler, and
// the assertions read the same table names production uses — they just live on the emulator. Two
// things genuinely cannot be local and each spec says so: Cognito's hosted login (there is no local
// authorizer, so `login()` injects a token the way the SPA's own callback would) and SES mail.
const PRESETS = {
  local: {
    baseURL: "http://localhost:3000",
    aws: { endpoint: process.env.LOCAL_AWS_ENDPOINT || "http://localhost:5000",
           credentials: { accessKeyId: "local", secretAccessKey: "local" } },
  },
  prod: {
    baseURL: "https://gradienterp.cloud",
    aws: null,   // named profiles from ~/.aws/config — see helpers/aws.mjs
  },
};

export function resolveEnv() {
  const name = process.env.E2E_ENV || "prod";
  const preset = PRESETS[name];
  if (!preset) {
    throw new Error(`unknown E2E_ENV="${name}" (expected ${Object.keys(PRESETS).join(" | ")})`);
  }
  return { name, baseURL: process.env.E2E_BASE_URL || preset.baseURL, aws: preset.aws };
}

export const isLocal = () => (process.env.E2E_ENV || "prod") === "local";
