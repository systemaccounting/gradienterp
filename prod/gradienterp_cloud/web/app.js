// gradientERP owner web app — lit-html (no build, vendored). Gerp-first SPA:
//   login -> home (your gerps + account actions) -> gerp hub.
// The auth (Cognito Hosted-UI PKCE) and BFF (/api/*) logic is preserved as plain async functions;
// lit owns the view rendering, routing, and the dynamic lists (gerps, profile links, autocomplete).
// Forms are uncontrolled: lit renders inputs with stable ids, handlers read/fill via $() on
// submit/load — lit preserves those DOM nodes across re-renders, so focus survives.

import { html, render, nothing } from './vendor/lit-html.js';

const CFG = {
  DOMAIN:    "https://gerp-auth.auth.us-east-1.amazoncognito.com",
  REGION:    "us-east-1",
  CLIENT_ID: "7jbo53d9kq8gvj0kglfnjuh17k",
  REDIRECT:  location.origin + "/auth/callback",
  SCOPES:    "openid email profile aws.cognito.signin.user.admin",   // the last one: attribute calls with the access token
  // where cognito() posts. The local stack has no Cognito, so on localhost this is the stand-in
  // (tests/server/cognito) — the same base-url seam the payments lambdas have for Stripe.
  COGNITO_IDP: ["localhost", "127.0.0.1"].includes(location.hostname)
    ? "http://127.0.0.1:4243/" : "https://cognito-idp.us-east-1.amazonaws.com/",
};
const $ = (id) => document.getElementById(id);

// ---------- state + the single update() ----------
const S = {
  view: "landingScreen",
  status: { msg: "", cls: "" },
  busy: null,           // the key of the button whose handler is still running (see `btn`)
  current: null,        // entered gerp {gerp_id, label}
  gerps: [],
  createAck: false,     // the purchase disclosure — Create stays disabled until it is ticked
  gerpsLoaded: false,   // false until the first /api/gerps resolves — don't flash the empty-state while loading
  pending: {},          // optimistic provisioning placeholders (gerp_id → label)
  capacity: null,       // {accounts, quota, available, measured_at} — the org's account count against its quota; null until read
  gerpCfg: null,        // gerp hub config { loading, chat_url, agent_email, agent_email_verified, verify_recipient, openly_operated, ooBusy, copied, notification_email, notification_email_verified, neBusy }
  pu: { links: [], ac: [], acOpen: false, linkMenuOpen: false, lat: null, lng: null, verified: false },
  acct: { loading: true },   // Info & Billing: { payment_method: {brand,last4,exp} | null, methods: [] }
  createOO: true,
  pubFields: {},        // which legal fields the public profile carries, one box per field (all, until unticked)
  createName: "",       // held so a failed create returns the buyer to a filled form
  createCards: null,    // the account's cards, listed when the create screen opens; null = loading
  createRegions: [],    // where a gerp can be built (GET /api/regions): [{id, label, status}]
  createRegion: "",     // the picked one; the address's country's by default, once the country is typed
  createPick: "",       // the selected row: a pm_… id, or "new" for add-a-card
  biz: { ac: [], acOpen: false, lat: null, lng: null },  // the create screen's legal address form (prefix biz)
  pub: { links: [], ac: [], acOpen: false, linkMenuOpen: false, lat: null, lng: null },  // and its public profile form (prefix pub)
  loadMsg: "",          // caption on the interstitial; empty means the sign-in one
  pendingEmail: "",
  pendingName: null,    // {first, last} between the signup form and the confirm code
  lightbox: null,       // the open demo {mod, role, prompt} or null
};
let root;
const update = () => render(appView(), root);
const set = (p) => { Object.assign(S, p); update(); remember(); };

// Where the owner is, kept in the url hash so a reload lands back there instead of on the home
// screen: #create, #account, #public, #gerp/<id>. The auth and consent landings use the path
// and the query, never the hash, so nothing collides.
const HASH_OF = { createGerpScreen: "#create", accountInfoAndBillingScreen: "#account", publicProfileScreen: "#public" };
function remember() {
  const h = S.view === "gerpScreen" && S.current?.gerp_id ? "#gerp/" + encodeURIComponent(S.current.gerp_id)
          : HASH_OF[S.view] || (S.view === "homeScreen" ? "" : null);
  if (h === null || location.hash === h || (!h && !location.hash)) return;
  history.replaceState(history.state, "", location.pathname + location.search + h);
}
async function restore() {
  const h = location.hash;
  if (h === "#create") { await openCreate(); return true; }
  if (h === "#account") { await openInfo(); return true; }
  if (h === "#public") { await openPublic(); return true; }
  if (h.startsWith("#gerp/")) {
    const id = decodeURIComponent(h.slice(6));
    const g = (S.gerps || []).find(x => x.gerp_id === id);
    if (g) { enterGerp(g.gerp_id, g.label); return true; }
  }
  return false;
}
// The banner renders at the END of the view (see `appView`), so on a long screen — a gerps list, the
// create form — it lands below the fold and the action looks like it did nothing at all. Bring it
// into view after the render rather than moving it, since where it sits is the layout's business.
const status = (msg, cls = "") => {
  set({ status: { msg, cls } });
  if (msg) requestAnimationFrame(() =>
    document.getElementById("status")?.scrollIntoView({ block: "nearest", behavior: "smooth" }));
};
const goTo = (v) => () => set({ view: v, status: { msg: "", cls: "" } });

// login-page demos: caption + a looping gif thumb; click opens it full-size in a lightbox
const DEMOS = [
  { mod: "accounting", role: "CFO", prompt: "prepare my monthly statements" },
  { mod: "contacts", role: "CMO", prompt: "who are our top 5 customers and how much did they spend?" },
  { mod: "labor", role: "COO", prompt: "schedule FOH coverage for the next 2 weeks" },
  { mod: "purchasing", role: "CPO", gif: "reorder", prompt: "we're down to 5 bags of espresso beans" },
  { mod: "treasury", role: "BOARD", gif: "invest", prompt: "offer Tanners Coffee Co 500k for a 10% monthly profit triggered 550k dividend" },
];
const ASSETS = "https://assets.gradienterp.cloud";                     // S3→CloudFront, not the BFF bundle
const demoThumb = (d) => `${ASSETS}/demo-${d.gif || d.mod}-thumb.gif`; // the row thumb (~50-140KB, still animated)
const demoGif = (d) => `${ASSETS}/demo-${d.gif || d.mod}.gif`;         // the card cut (lightbox fallback)
const demoFull = (d) => `${ASSETS}/demo-${d.gif || d.mod}-full.gif`;   // the lightbox cut, gif fallback
const demoVid = (d) => `${ASSETS}/demo-${d.gif || d.mod}-full.mp4`;    // the lightbox cut as video (~10x lighter)
const openDemo = (d) => () => set({ lightbox: d, lightboxLoading: true, lightboxGif: false });
const closeDemo = () => set({ lightbox: null });
window.addEventListener("keydown", (e) => { if (e.key === "Escape" && S.lightbox) closeDemo(); });

// ---------- auth (preserved) ----------
const idToken = () => sessionStorage.getItem("id_token");
function claims() { try { const p = idToken().split(".")[1]; return JSON.parse(atob(p.replace(/-/g, "+").replace(/_/g, "/"))); } catch { return {}; } }
async function cognito(action, body) {
  const r = await fetch(CFG.COGNITO_IDP, {
    method: "POST",
    headers: { "content-type": "application/x-amz-json-1.1", "x-amz-target": "AWSCognitoIdentityProviderService." + action },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(d.message || (d.__type || "").split("#").pop() || ("error " + r.status));
    e.code = (d.__type || "").split("#").pop();   // AliasExistsException, CodeMismatchException, …
    throw e;
  }
  return d;
}
const accessToken = () => sessionStorage.getItem("access_token");
// Three tokens, one store. The id token is what every API call carries; the access token is what
// Cognito's own attribute calls take; the refresh token is what replaces both without a sign-in.
function storeTokens(t) {
  if (t.id_token) sessionStorage.setItem("id_token", t.id_token);
  if (t.access_token) sessionStorage.setItem("access_token", t.access_token);
  if (t.refresh_token) sessionStorage.setItem("refresh_token", t.refresh_token);
}
// the chat page's opening hash: the id token, and the refresh token beside it so the chat
// renews the id token itself for as long as the refresh token holds
function chatHash() {
  const rt = sessionStorage.getItem("refresh_token");
  return "#id_token=" + idToken() + (rt ? "&refresh_token=" + rt : "");
}
async function refresh() {
  const rt = sessionStorage.getItem("refresh_token");
  if (!rt) throw new Error("no refresh token in this session");
  const d = await cognito("InitiateAuth", { AuthFlow: "REFRESH_TOKEN_AUTH", ClientId: CFG.CLIENT_ID,
                                            AuthParameters: { REFRESH_TOKEN: rt } });
  const a = d.AuthenticationResult || {};
  storeTokens({ id_token: a.IdToken, access_token: a.AccessToken });
}
const b64url = (b) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const sha256 = (s) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
// A link into the console — `?gerp=<id>&open=chat` from the ready mail — survives the sign-in:
// stashed before the redirect to Cognito, read back after the token exchange, then followed.
// `say` is a phrase KEY, never free text from a url: the console owns the words
const SAY = { onboard: "onboard my business" };
function stashDeepLink() {
  const q = new URLSearchParams(location.search);
  if (q.get("gerp") && q.get("open") && !q.get("session_id") && !q.get("setup_intent")) sessionStorage.setItem("after_login", JSON.stringify({ gerp: q.get("gerp"), open: q.get("open"), say: q.get("say") || "" }));
}
async function followDeepLink() {
  let link = null;
  try { link = JSON.parse(sessionStorage.getItem("after_login") || "null"); } catch (_) {}
  sessionStorage.removeItem("after_login");
  if (!link) return false;
  const g = (S.gerps || []).find(x => x.gerp_id === link.gerp);
  if (!g) return false;
  if (link.open === "chat" && g.chat_url) {
    const say = SAY[link.say] ? "&say=" + encodeURIComponent(link.say) : "";   // the key; the chat page holds the words
    location.assign(g.chat_url + chatHash() + say); return true;
  }
  enterGerp(g.gerp_id, g.label);
  return true;
}
async function login() {
  stashDeepLink();
  const v = b64url(crypto.getRandomValues(new Uint8Array(32))); sessionStorage.setItem("pkce", v);
  const u = new URL(CFG.DOMAIN + "/oauth2/authorize");
  u.search = new URLSearchParams({ response_type: "code", client_id: CFG.CLIENT_ID, redirect_uri: CFG.REDIRECT, scope: CFG.SCOPES, code_challenge_method: "S256", code_challenge: b64url(await sha256(v)) });
  location.assign(u.toString());
}
async function exchange(code) {
  const r = await fetch(CFG.DOMAIN + "/oauth2/token", { method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", client_id: CFG.CLIENT_ID, code, redirect_uri: CFG.REDIRECT, code_verifier: sessionStorage.getItem("pkce") }) });
  if (!r.ok) throw new Error("token exchange: " + r.status + " " + await r.text());
  return await r.json();   // {id_token, access_token, refresh_token, …}
}
function logout() {
  sessionStorage.clear();
  // also end the Cognito Hosted UI session — clearing only our tokens leaves Cognito's own session
  // cookie, which silently re-auths the same user on the next /authorize. logout_uri must match
  // cognito.tf logout_urls exactly: origin, no slash.
  const u = new URL(CFG.DOMAIN + "/logout");
  u.search = new URLSearchParams({ client_id: CFG.CLIENT_ID, logout_uri: location.origin });
  location.assign(u.toString());
}
// The id token lives an hour and the refresh token a month: a call made on a stale token is
// refreshed first, and one the BFF still refuses is refreshed once and retried. A refresh that
// fails is a session that is over, which is the sign-in page, not a screen full of 401s.
const tokenStale = () => { const e = claims().exp; return !!e && e * 1000 < Date.now() + 60_000; };
async function api(path, opts = {}) {
  const call = () => fetch(path, { ...opts, headers: { ...(opts.headers || {}), "Authorization": "Bearer " + idToken(), "Content-Type": "application/json" } });
  const renew = async () => { try { await refresh(); return true; } catch { await login(); return false; } };
  if (tokenStale() && !(await renew())) return new Response(null, { status: 401 });
  const r = await call();
  if (r.status !== 401) return r;
  return (await renew()) ? call() : r;
}

// ---------- gerps: list, polling, enter ----------
let pollTimer = null;
const startPoll = () => { if (!pollTimer) pollTimer = setInterval(() => loadGerps().catch(() => {}), 10000); };
const stopPoll = () => { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } };
async function loadGerps() {
  const r = await api("/api/gerps"); if (!r.ok) throw new Error("/api/gerps " + r.status + " " + await r.text());
  const { gerps } = await r.json();
  S.gerps = gerps;
  S.gerpsLoaded = true;
  // the placeholder goes as soon as the row can speak for itself — any status but the one
  // the placeholder stands in for
  for (const g of gerps) if (g.status !== "provisioning" && g.status !== "queued") delete S.pending[g.gerp_id];
  // poll for machines that are working, and for a place in line. An unpaid gerp changes when
  // someone saves a card, not on a timer, so polling one is a request every ten seconds for the
  // rest of the session.
  const working = gerps.some(g => g.status === "provisioning" || g.status === "queued");
  (working || Object.keys(S.pending).length) ? startPoll() : stopPoll();
  update();
}
function enterGerp(gerp_id, label) {
  S.current = { gerp_id, label };
  S.gerpCfg = { loading: true };
  set({ view: "gerpScreen", status: { msg: "", cls: "" },
        biz: { ac: [], acOpen: false, lat: null, lng: null },
        pub: { links: [], ac: [], acOpen: false, linkMenuOpen: false, lat: null, lng: null } });
  api("/api/gerp-config?gerp_id=" + encodeURIComponent(gerp_id))
    .then(r => r.ok ? r.json() : Promise.reject(new Error("load failed")))
    .then(c => { S.gerpCfg = { ...c, loading: false }; update(); loadGerpCards(gerp_id); })
    .catch(() => { S.gerpCfg = { loading: false, error: true }; update(); });
  loadGerpInfo(gerp_id);
}
// Business info: the label and the two profiles off the row, into the shared inputs
async function loadGerpInfo(gerp_id) {
  try {
    const r = await api("/api/gerp-info?gerp_id=" + encodeURIComponent(gerp_id));
    if (!r.ok) return;
    const info = await r.json();
    if (S.current?.gerp_id !== gerp_id) return;
    $("gi-label").value = info.label || "";
    fillBusinessProfile(info.legal, info.public);
  } catch (_) {}
}
async function doSaveGerpInfo() {
  const gerp_id = S.current?.gerp_id; if (!gerp_id) return;
  const label = ($("gi-label")?.value || "").trim(); if (!label) { status("business name required", "err"); return; }
  const { legal, missing, pub } = readBusinessProfile();
  if (missing.length) { status("business profile — missing " + missing.map(k => BIZ_FIELD_NAMES[k] || k).join(", "), "err"); return; }
  status("saving…", "muted");
  try {
    const r = await api("/api/gerp-info", { method: "POST", body: JSON.stringify({ gerp_id, label, legal, public: pub }) });
    const t = await r.text(); if (!r.ok) throw new Error(r.status + " " + t);
    S.current = { ...S.current, label };
    status("saved", "ok");
    loadGerps();
  } catch (e) { status(String(e.message || e), "err"); }
}
async function toggleOO(want) {
  if (!S.current) return;
  // the public boxes follow the switch: off clears them, on ticks them all again
  S.pubFields = want ? ALL_PUBLIC() : {};
  S.gerpCfg = { ...S.gerpCfg, ooBusy: true }; update();
  try {
    const r = await api("/api/gerp-settings", { method: "POST", body: JSON.stringify({ gerp_id: S.current.gerp_id, openly_operated: want }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || ("error " + r.status));
    const s = await r.json();
    S.gerpCfg = { ...S.gerpCfg, openly_operated: !!s.openly_operated, ooBusy: false };
    status(want ? "now openly operated — publishing to openlyoperated.biz" : "now private", "ok");
  } catch (err) { S.gerpCfg = { ...S.gerpCfg, ooBusy: false }; status(String(err.message || err), "err"); }
}
async function saveNotificationEmail(addr) {
  if (!S.current) return;
  addr = (addr || "").trim();
  if (addr === (S.gerpCfg?.notification_email || "")) return;   // unchanged — skip the write (avoids a needless re-verify)
  S.gerpCfg = { ...S.gerpCfg, neBusy: true }; update();
  try {
    const r = await api("/api/gerp-settings", { method: "POST", body: JSON.stringify({ gerp_id: S.current.gerp_id, notification_email: addr }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || ("error " + r.status));
    const s = await r.json();
    S.gerpCfg = { ...S.gerpCfg, notification_email: s.notification_email || "", notification_email_verified: !!s.notification_email_verified, neBusy: false };
    status(addr ? "notification email saved — check " + addr + " for the verify link" : "notification email cleared", "ok");
  } catch (err) { S.gerpCfg = { ...S.gerpCfg, neBusy: false }; status(String(err.message || err), "err"); }
}

async function saveTimezone(value) {
  if (!S.current) return;
  const tz = (value || "").trim();
  if (!tz || tz === (S.gerpCfg?.timezone || "")) return;
  S.gerpCfg = { ...S.gerpCfg, tzBusy: true }; update();
  try {
    const r = await api("/api/gerp-settings", { method: "POST", body: JSON.stringify({ gerp_id: S.current.gerp_id, timezone: tz }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || ("error " + r.status));
    const s = await r.json();
    S.gerpCfg = { ...S.gerpCfg, timezone: s.timezone || "UTC", tzBusy: false };
    status("timezone saved — periods and schedules now use " + (s.timezone || "UTC"), "ok");
  } catch (err) { S.gerpCfg = { ...S.gerpCfg, tzBusy: false }; status(String(err.message || err), "err"); }
}

// Past this many the list collapses behind "show more" — a long list is the agent's problem to
// read, not the owner's to scroll past on the way to the rest of the screen.
const INSTR_INLINE = 10;

// Standing instructions: one line each, loaded into the agent's system prompt on every turn.
// Both writes POST the same /api/gerp-settings and take the returned view as truth, so the list
// reflects what the gerp actually stored (a duplicate line, for instance, comes back unchanged).
async function saveInstruction(input) {
  if (!S.current) return;
  const text = (input.value || "").trim();
  if (!text) return;
  S.gerpCfg = { ...S.gerpCfg, inBusy: true }; update();
  try {
    const r = await api("/api/gerp-settings", { method: "POST", body: JSON.stringify({ gerp_id: S.current.gerp_id, instruction: text }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || ("error " + r.status));
    const s = await r.json();
    input.value = "";
    S.gerpCfg = { ...S.gerpCfg, instructions: s.instructions || [], inBusy: false };
    status("instruction saved — your agent has it from its next message", "ok");
  } catch (err) { S.gerpCfg = { ...S.gerpCfg, inBusy: false }; status(String(err.message || err), "err"); }
}
async function removeInstruction(id) {
  if (!S.current) return;
  S.gerpCfg = { ...S.gerpCfg, inBusy: true }; update();
  try {
    const r = await api("/api/gerp-settings", { method: "POST", body: JSON.stringify({ gerp_id: S.current.gerp_id, remove_instruction: id }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || ("error " + r.status));
    const s = await r.json();
    const left = s.instructions || [];
    // collapse once the list fits again, so "show less" can't strand the owner on an empty tail
    S.gerpCfg = { ...S.gerpCfg, instructions: left, inBusy: false, expanded: S.gerpCfg.expanded && left.length > INSTR_INLINE };
    status("instruction removed", "ok");
  } catch (err) { S.gerpCfg = { ...S.gerpCfg, inBusy: false }; status(String(err.message || err), "err"); }
}
const instrRow = (i, busy) => html`
  <div class="setting instr"><span class="s-body"><span class="s-name">${i.text}</span></span><button class="x" title="Remove" ?disabled=${busy} @click=${() => removeInstruction(i.id)}>×</button></div>`;

// ---------- public-user: address autocomplete + links ----------
let acTimer = null;
const acClose = (form = "pu") => { if (S[form]) set({ [form]: { ...S[form], ac: [], acOpen: false } }); };
// Two forms carry an address — the public profile (`pu`) and the account's private record
// (`acct`) — so the autocomplete takes the form's prefix: inputs are `<prefix>-street` etc. and
// the suggestion state lives on S[prefix].
async function acPick(placeId, form = "pu") {
  set({ [form]: { ...S[form], ac: [], acOpen: false } });
  try {
    const r = await api("/api/places/place?id=" + encodeURIComponent(placeId));
    if (!r.ok) return;
    const p = await r.json();
    // a suggestion for the street alone carries no house number: the one typed stays
    const typed = ($(form + "-street").value || "").trim();
    const number = (typed.match(/^\d+[A-Za-z]?\b/) || [])[0];
    if (p.street) $(form + "-street").value = (number && !/^\d/.test(p.street)) ? number + " " + p.street : p.street;
    ["city", "state", "zip", "country"].forEach(k => { if (p[k]) $(form + "-" + k).value = p[k]; });
    S[form].lat = p.lat; S[form].lng = p.lng;   // geocode → stored on save
    if (form === "biz" && p.country && S.view === "createGerpScreen") loadCreateRegions(p.country);   // the address's country picks the region
    onAcctInput();
  } catch (_) {}
}
function acInput(form = "pu") {
  S[form].lat = S[form].lng = null;             // manual edit invalidates the prior geocode
  const q = $(form + "-street").value.trim();
  clearTimeout(acTimer);
  if (q.length < 3) { acClose(form); return; }
  acTimer = setTimeout(async () => {
    try { const r = await api("/api/places/autocomplete?q=" + encodeURIComponent(q));
      set({ [form]: { ...S[form], ac: r.ok ? (await r.json()).suggestions : [], acOpen: true } });
    } catch (_) { acClose(form); }
  }, 300);
}
// What the first-level division and the postal code are called where the address is. Amazon
// gives the values (Region, SubRegion, PostalCode), not the words; a country not listed reads
// Region / Postal code. Keyed by the country as Places names it and by ISO2.
const REGION_WORD = { "United States": "State", US: "State", Australia: "State", AU: "State", India: "State", IN: "State",
  Brazil: "State", BR: "State", Mexico: "State", MX: "State", Germany: "State", DE: "State", Austria: "State", AT: "State",
  Canada: "Province", CA: "Province", Ireland: "County", IE: "County", "United Kingdom": "County", GB: "County",
  Japan: "Prefecture", JP: "Prefecture", France: "Region", FR: "Region", Spain: "Province", ES: "Province", Italy: "Province", IT: "Province",
  Netherlands: "Province", NL: "Province", Belgium: "Province", BE: "Province", Switzerland: "Canton", CH: "Canton",
  "New Zealand": "Region", NZ: "Region", Singapore: "District", SG: "District", "South Korea": "Province", KR: "Province" };
const POSTAL_WORD = { "United States": "Zip", US: "Zip", Ireland: "Eircode", IE: "Eircode", "United Kingdom": "Postcode", GB: "Postcode",
  Australia: "Postcode", AU: "Postcode", "New Zealand": "Postcode", NZ: "Postcode", Canada: "Postal code", CA: "Postal code",
  India: "PIN code", IN: "PIN code", Netherlands: "Postcode", NL: "Postcode" };
const regionWord = (country) => REGION_WORD[(country || "").trim()] || "Region";
const postalWord = (country) => POSTAL_WORD[(country || "").trim()] || "Postal code";

// `optionalHint: false` drops the "— optional" beside Unit, for a form that is optional as a whole
const addressFieldsTpl = (form, { optionalHint = true } = {}) => html`
  <label for="${form}-street">Street address</label>
  <div class="ac-wrap"><input id="${form}-street" autocomplete="off" placeholder="start typing — we'll autocomplete" @input=${() => acInput(form)} @blur=${(e) => { setTimeout(() => acClose(form), 150); onAcctInput(e); }} />
    <div class="ac-menu ${S[form]?.acOpen && S[form]?.ac?.length ? "open" : ""}">${(S[form]?.ac || []).map(s => html`<div class="ac-item" @mousedown=${e => { e.preventDefault(); acPick(s.place_id, form); }}>${s.label}</div>`)}</div></div>
  <div class="row2"><div><label for="${form}-unit">Unit${optionalHint ? html` <span class="muted" style="font-weight:400">— optional</span>` : nothing}</label><input id="${form}-unit" autocomplete="address-line2" /></div><div><label for="${form}-city">City</label><input id="${form}-city" @blur=${onAcctInput} autocomplete="address-level2" /></div></div>
  <div class="row2"><div><label for="${form}-state" data-view="stateLabel">${regionWord($(form + "-country")?.value)}</label><input id="${form}-state" @blur=${onAcctInput} autocomplete="address-level1" /></div><div><label for="${form}-zip">${postalWord($(form + "-country")?.value)}</label><input id="${form}-zip" @blur=${onAcctInput} autocomplete="postal-code" /></div></div>
  <label for="${form}-country">Country</label><input id="${form}-country" @blur=${(e) => { onAcctInput(e); update(); }} autocomplete="country-name" />`;
const LINK_TYPES = [
  { id: "website", label: "Website", ph: "https://example.com" },
  { id: "x", label: "X", ph: "https://x.com/handle" },
  { id: "linkedin", label: "LinkedIn", ph: "https://linkedin.com/in/…" },
  { id: "github", label: "GitHub", ph: "https://github.com/…" },
  { id: "instagram", label: "Instagram", ph: "https://instagram.com/…" },
  { id: "youtube", label: "YouTube", ph: "https://youtube.com/@…" },
  { id: "facebook", label: "Facebook", ph: "https://facebook.com/…" },
  { id: "bluesky", label: "Bluesky", ph: "https://bsky.app/profile/…" },
  { id: "other", label: "Other", ph: "https://…" },
];
const LINK_LABEL = Object.fromEntries(LINK_TYPES.map(t => [t.id, t.label]));
const LINK_PH = Object.fromEntries(LINK_TYPES.map(t => [t.id, t.ph]));
// Two forms carry links — the person's public profile (`pu`) and the business's on the create
// screen (`pub`) — so these take the form's prefix like the address helpers do.
const addLink = (t, form = "pu") => { S[form].links.push({ type: t, url: "" }); set({ [form]: { ...S[form], linkMenuOpen: false } });
  const ins = $(form + "-links")?.querySelectorAll("input"); ins && ins[ins.length - 1]?.focus(); };
const removeLink = (i, form = "pu") => { S[form].links.splice(i, 1); update(); };

// ---------- handlers (bodies preserved; read via $()) ----------
async function doSignup() {
  const first = $("su-first").value.trim(), last = $("su-last").value.trim();
  const email = $("su-email").value.trim(), pass = $("su-pass").value;
  if (!first || !last || !email || !pass) { status("all fields required", "err"); return; }
  status("creating your account…", "muted");
  try {
    // The pool holds identity and credentials; the name is the gerp-accounts row's. It rides to
    // the post-confirmation trigger as ClientMetadata on the CONFIRM call (the trigger receives the
    // metadata of the call that fires it), so it is held here across the code screen.
    await cognito("SignUp", { ClientId: CFG.CLIENT_ID, Username: email, Password: pass,
      UserAttributes: [{ Name: "email", Value: email }] });
    S.pendingEmail = email;
    S.pendingName = { first, last };
    set({ view: "confirmScreen", status: { msg: "", cls: "" } });
    $("cf-code").focus();
  } catch (e) { status(String(e.message || e), "err"); }
}
async function doConfirm() {
  const code = $("cf-code").value.trim();
  if (!code) { status("enter the code from your email", "err"); return; }
  status("confirming…", "muted");
  try {
    await cognito("ConfirmSignUp", { ClientId: CFG.CLIENT_ID, Username: S.pendingEmail, ConfirmationCode: code,
                                     ClientMetadata: { first: S.pendingName?.first || "", last: S.pendingName?.last || "" } });
    status("account created — sign in to continue", "ok");
    await login();
  } catch (e) { status(String(e.message || e), "err"); }
}
// The business profile off the inputs: the legal record (and which required fields it lacks) and
// the public one with its links and geocode.
// The business name is the name, legal and public alike: `bizname` on the create screen,
// `gi-label` on the gerp screen. Neither profile carries its own.
const businessName = () => ($("bizname")?.value || $("gi-label")?.value || "").trim();
function readBusinessProfile() {
  const legal = {}; BIZ_FIELDS.forEach(k => { const v = ($("biz-" + k)?.value || "").trim(); if (v) legal[k] = v; });
  legal.name = businessName() || legal.name;
  const missing = BIZ_REQUIRED.filter(k => !legal[k]);
  // the GSTIN is an Indian business's, and one that is not a GSTIN is named here, before the card step
  if (legal.tax_id && !isIndia(legal.country)) delete legal.tax_id;
  if (legal.tax_id) { legal.tax_id = legal.tax_id.toUpperCase(); if (!/^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$/.test(legal.tax_id)) missing.push("tax_id"); }
  // the public profile: the business name, the legal fields whose box is ticked, the links
  const pub = { name: legal.name };
  for (const [k] of PUB_CHOICES) if (S.pubFields[k] && legal[k]) pub[k] = legal[k];
  if (S.pubFields.street && S.biz.lat != null && S.biz.lng != null) { pub.lat = S.biz.lat; pub.lng = S.biz.lng; }
  pub.links = S.pub.links.map(l => ({ type: l.type, url: (l.url || "").trim() })).filter(l => l.url);
  return { legal, missing, pub };
}
function fillBusinessProfile(legal, pub) {
  BIZ_FIELDS.forEach(k => { const b = $("biz-" + k); if (b) b.value = (legal || {})[k] || ""; });
  S.biz.lat = (legal || {}).lat ?? (pub || {}).lat ?? null; S.biz.lng = (legal || {}).lng ?? (pub || {}).lng ?? null;
  // a box is ticked when the saved public profile carries that field; no public yet ticks them all
  const has = Object.keys(pub || {}).length > 0;
  const pubFields = has ? Object.fromEntries(PUB_CHOICES.map(([k]) => [k, !!(pub || {})[k]])) : ALL_PUBLIC();
  set({ pubFields, pub: { ...S.pub, links: ((pub || {}).links || []).map(l => ({ type: l.type || "other", url: l.url || "" })) } });
}
async function doCreate() {
  // Three steps, because the card never touches this page: record the gerp, get a hosted Stripe
  // page, hand the browser to it. Provisioning fires when we come back and the card is stored —
  // a gerp is a real AWS sub-account, so it is not vended to someone who typed a name and left.
  const name = $("bizname").value.trim(); if (!name) { status("business name required", "err"); return; }
  // the legal business profile is required as a whole; the public one is sent as typed
  const { legal, missing: missingLegal, pub } = readBusinessProfile();
  if (missingLegal.length) { status("business profile — missing " + missingLegal.map(k => BIZ_FIELD_NAMES[k] || k).join(", "), "err"); return; }
  if (!S.createAck) { status("please read and acknowledge what you are buying", "err"); return; }
  const oo = S.createOO, pick = S.createPick;
  if (!pick) { status("choose how to pay", "err"); return; }
  // Add a card opens Stripe's page in a NEW window, and the window has to be opened here, inside
  // the click — opened after the fetch below it is a popup the browser blocks. It starts blank and
  // is pointed at Stripe once the session exists; the create screen never navigates.
  const win = pick === "new" ? window.open("about:blank", "gerp-card") : null;
  set({ createName: name, status: { msg: "creating…", cls: "muted" } });
  try {
    const r = await api("/api/gerps", { method: "POST", body: JSON.stringify({ business_name: name, openly_operated: oo, terms_version: PURCHASE_TERMS_VERSION(), legal, public: pub, region: S.createRegion || undefined }) });
    if (!r.ok) {
      const text = await r.text();
      let j = {}; try { j = JSON.parse(text); } catch (_) {}
      if (r.status === 409 && j.balance_owed != null) {
        // an earlier account under this email or phone closed unpaid — said plainly, not as a
        // payment-page failure, with the invoice it can settle from here
        if (win) win.close();
        set({ createOwed: { balance: j.balance_owed, invoices: j.invoices || [] } });
        status(`An earlier account closed owing $${Number(j.balance_owed).toFixed(2)}. Settle it before creating a gerp.`, "err");
        return;
      }
      throw new Error(r.status + " " + text);
    }
    const t = await r.json();
    if (pick === "new") {
      const s = await api("/api/billing/setup-link", { method: "POST", body: JSON.stringify({
        gerp_id: t.gerp_id, return_url: location.origin + "/?gerp=" + encodeURIComponent(t.gerp_id) + "&popup=1" }) });
      if (!s.ok) throw new Error("payment setup " + s.status + " " + await s.text());
      const { url } = await s.json();
      if (win) win.location = url; else window.open(url, "gerp-card");
      S.pending[t.gerp_id] = t.label;
      status("Finish adding the card in the window that opened — this page updates when it is saved.", "muted");
      return;
    }
    // an existing card: the gerp takes it, and taking it is the payment step — it provisions
    const sel = await api("/api/billing/methods", { method: "POST", body: JSON.stringify({
      action: "select", gerp_id: t.gerp_id, payment_method_id: pick }) });
    if (!sel.ok) throw new Error("select card " + sel.status + " " + await sel.text());
    S.pending[t.gerp_id] = t.label;
    set({ createName: "", createAck: false, view: "homeScreen", status: { msg: "", cls: "" } });
    status(`${t.label} is being created.`, "ok");
    loadGerps();
  } catch (e) {
    if (win) win.close();
    set({ view: "createGerpScreen" }); loadAccountGate();
    status(payerMsg(e, "create ∇ERP"), "err");
  }
}

// The card window, when it lands back on our origin with popup=1: save the card, tell the
// opener, close. Nothing else in the app runs in that window.
async function finishInPopup(sessionId, gerpId) {
  let msg = { gerp_id: gerpId, ok: false };
  try { await finishCardSetup(sessionId); msg.ok = true; }
  catch (e) { console.error("[card window]", e); msg.error = String(e.message || e); }
  try { new BroadcastChannel("gerp-cards").postMessage(msg); } catch (_) {}
  // the error carries text from the server, so it is set as text, never parsed as markup
  const note = document.createElement("p");
  note.style.cssText = "font:15px system-ui;padding:2rem";
  note.textContent = msg.ok ? "Card saved — you can close this window." : "The card was not saved: " + msg.error;
  document.body.replaceChildren(note);
  if (msg.ok) setTimeout(() => window.close(), 400);
}
// The create screen listens: the card landed, the gerp is being vended, go show it.
try {
  new BroadcastChannel("gerp-cards").onmessage = (ev) => {
    const m = ev.data || {};
    if (S.view !== "createGerpScreen") return;
    if (m.ok) { set({ createName: "", createAck: false, view: "homeScreen" }); status("Card saved — the ∇ERP is being created.", "ok"); loadGerps(); }
    else status("The card was not saved: " + (m.error || "unknown"), "err");
  };
} catch (_) {}
// What a buyer reads when a payment step fails, and what they do NOT read: the response body. A 502
// off the seller carries our secret names and route internals, and a person mid-purchase needs to
// know whether they were charged — not which SSM parameter is missing. The raw error still goes to
// the console, where whoever is debugging can reach it.
function payerMsg(e, where) {
  console.error(`[${where}]`, e);
  return "Couldn't reach the payment page. Nothing has been charged — try again, and if it keeps "
       + "happening, the ∇ERP is saved and you can finish paying for it from your ∇ERPs list.";
}

// `location.assign` neither throws nor resolves — the page either unloads or it does not, and if
// it does not the interstitial spins with nothing left to update it. Both halves are bounded: the
// request can be abandoned, and a navigation that has not happened hands the screen back.
const LEAVE_MS = 8000, CALL_MS = 25000;

async function askStripe(body, back) {
  const ac = new AbortController();
  const bell = setTimeout(() => ac.abort(), CALL_MS);
  try {
    const r = await api("/api/billing/setup-link", { method: "POST", signal: ac.signal,
                                                     body: JSON.stringify(body) });
    if (!r.ok) throw new Error("payment setup " + r.status + " " + await r.text());
    const { url } = await r.json();
    if (!url) throw new Error("payment setup returned no url");
    location.assign(url);
    setTimeout(() => {
      if (S.view !== "loadingScreen") return;      // it left, as it should have
      console.error("[checkout] the browser did not navigate", url);
      set({ loadMsg: "", view: back });
      status("The payment page didn't open. Nothing has been charged — try again.", "err");
    }, LEAVE_MS);
  } finally { clearTimeout(bell); }
}

async function goToCheckout(gerpId, label) {
  // The row and its gerp_id already exist by the time this runs, whether it was created a second
  // ago or abandoned last week — so resuming an unpaid gerp is this same call, not a second path.
  //
  // The interstitial is the ONLY thing said here. Every route to Stripe waits the same few seconds
  // on the same round trip, so they get the same screen; a status line as well just says it twice.
  const back = S.view;
  set({ view: "loadingScreen", loadMsg: "Loading payment screen…", status: { msg: "", cls: "" } });
  try {
    S.pending[gerpId] = label;
    await askStripe({ gerp_id: gerpId,
                      return_url: location.origin + "/?gerp=" + encodeURIComponent(gerpId) }, back);
  } catch (e) {
    set({ loadMsg: "", view: back });      // back where they pressed it, so the error has a home
    throw e;
  }
}

async function resumeCheckout(g) {
  try { await goToCheckout(g.gerp_id, g.label); }
  catch (e) { status(payerMsg(e, "resume checkout"), "err"); }
}

async function finishVendorConsent(sessionId) {
  // Which gerp and which vendor is the server's to say: every gerp the account owns is asked
  // whether it holds this session, and the one that does completes it.
  try {
    const r = await api("/api/mcp/complete", { method: "POST", body: JSON.stringify({ session_id: sessionId }) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) return { cls: "err", msg: out.error || ("The connection could not be finished (" + r.status + ")") };
    const vendor = out.provider || "the vendor";
    const where = out.gerp_id ? " on " + out.gerp_id : "";
    const msg = out.kind === "target"
      ? vendor + " is connecting" + where + " — the agent's " + vendor + " tools appear in a moment."
      : vendor + " connected" + where + ".";
    return { cls: "ok", msg, gerp_id: out.gerp_id || "" };
  } catch (e) {
    console.error("[mcp consent]", e);
    return { cls: "err", msg: "We lost track of that connection — ask the agent for the link again." };
  }
}

async function finishCardSetup(sessionId) {
  // Back from Stripe. The session id is all we carry — which card, and whose, is read server-side
  // off the session itself, because everything in this URL is the payer's to edit.
  const r = await api("/api/billing/save-card", { method: "POST", body: JSON.stringify(
    sessionId.startsWith("seti_") ? { setup_intent_id: sessionId } : { session_id: sessionId }) });
  // A transport failure is NOT a refusal. The server may have finished after we stopped waiting —
  // a 20s BFF timeout did exactly that, saving the card while the buyer was told it had not been.
  // `definite` marks the one case we can speak for: a server that answered and said no.
  if (!r.ok) throw new Error("saving card " + r.status + " " + await r.text());
  const out = await r.json();
  if (!out.stored) {
    const e = new Error("card was not saved — nothing was charged, you can try again");
    e.definite = true;
    throw e;
  }
  return out;
}
async function openPublic() {
  S.pu = { links: [], ac: [], acOpen: false, linkMenuOpen: false, lat: null, lng: null, verified: false };
  set({ view: "publicProfileScreen", status: { msg: "", cls: "" } }); loadAccountGate();
  ["first", "middle", "last", "street", "unit", "city", "state", "zip", "country", "phone"].forEach(k => { if ($("pu-" + k)) $("pu-" + k).value = ""; });
  $("pu-email").value = claims().email || "";
  try {
    const r = await api("/api/public-user");
    if (r.ok) {
      const p = await r.json();
      ["first", "middle", "last", "street", "unit", "city", "state", "zip", "country", "email", "phone"].forEach(k => { if (p[k] && $("pu-" + k)) $("pu-" + k).value = p[k]; });
      S.pu.lat = p.lat ?? null; S.pu.lng = p.lng ?? null;
      S.pu.soc = p.soc || []; S.pu.soc_window = p.soc_window || "";
      S.pu.links = Array.isArray(p.links) ? p.links.map(l => ({ type: l.type || "other", url: l.url || "" })) : [];
      S.pu.verified = !!p.verified;
      update();
    }
  } catch (_) {}
}
async function doSavePublic() {
  const PU = ["first", "middle", "last", "street", "unit", "city", "state", "zip", "country", "email", "phone"];
  const f = {}; PU.forEach(k => f[k] = $("pu-" + k).value.trim());
  if (!f.first || !f.last) { status("first and last name required", "err"); return; }
  if (S.pu.lat != null && S.pu.lng != null) { f.lat = S.pu.lat; f.lng = S.pu.lng; }
  f.links = S.pu.links.map(l => ({ type: l.type, url: (l.url || "").trim() })).filter(l => l.url);
  status("saving…", "muted");
  try {
    const r = await api("/api/public-user", { method: "POST", body: JSON.stringify(f) });
    const t = await r.text(); if (!r.ok) throw new Error(r.status + " " + t);
    status("saved", "ok"); set({ view: "homeScreen" });
  } catch (e) { status(String(e), "err"); }
}
async function openInfo() {
  set({ view: "accountInfoAndBillingScreen", status: { msg: "", cls: "" }, acct: { loading: true } });
  $("acct-email").value = claims().email || "";
  ACCT_FIELDS.forEach(k => { if ($("acct-" + k)) $("acct-" + k).value = ""; });
  try {
    const r = await api("/api/account");
    if (r.ok) {
      const a = await r.json();
      // the record inputs are uncontrolled — filled here, preserved by lit across the re-render below
      ACCT_FIELDS.forEach(k => { if ($("acct-" + k)) $("acct-" + k).value = a[k] || ""; });
      if (a.email) $("acct-email").value = a.email;
      set({ acct: { payment_method: a.payment_method || null, methods: [], missing: a.missing || [],
                    ac: [], acOpen: false, lat: null, lng: null } });
      loadCards();
    } else { set({ acct: {} }); }
  } catch (_) { set({ acct: {} }); }
}

// Taking your data out, without asking the agent. This is the door that outlives the instance:
// after closure the agent, the gateway and the runtime are gone, and the account screen is the only
// place left that can hand you a fresh link (modules/export/TODO.md § closure).
// Closing a gerp destroys it. The dialog is a gate, not a warning: Close stays disabled until the
// owner has typed the phrase, and the SERVER requires the same string on the request — a phrase
// typed into a browser records nothing, which is why `terms_version` works the same way on create.
const CLOSE_PHRASE = "I understand";

const openClose = () => { S.gerpCfg = { ...S.gerpCfg, closing: { typed: "", busy: false } }; update(); };
const dismissClose = () => { S.gerpCfg = { ...S.gerpCfg, closing: null }; update(); };

// Deleting the account erases the person from the platform. Same gate as a close: the phrase is
// typed here and required on the request. Refused by the server while a gerp is still running.
const DELETE_PHRASE = "delete my account";
const openDelete = () => set({ deleting: { typed: "", busy: false, live: [] } });
const dismissDelete = () => set({ deleting: null });

async function confirmDelete() {
  set({ deleting: { ...S.deleting, busy: true } });
  try {
    const r = await api("/api/account", { method: "DELETE", body: JSON.stringify({ confirm: DELETE_PHRASE }) });
    const out = await r.json().catch(() => ({}));
    if (r.status === 409 && out.gerps) {
      // the server names what is still running; each is a gerp with its own close
      set({ deleting: { ...S.deleting, busy: false, live: out.gerps } });
      return;
    }
    if (!r.ok) throw new Error(r.status + " " + JSON.stringify(out));
    // there is no account to be signed in to. Cognito's logout clears the hosted-UI cookie too.
    logout();
  } catch (e) {
    console.error("[delete account]", e);
    set({ deleting: { ...S.deleting, busy: false } });
    status("Couldn't delete the account. Nothing has changed — try again in a moment.", "err");
  }
}

async function confirmClose() {
  const gerp_id = S.current?.gerp_id;
  S.gerpCfg = { ...S.gerpCfg, closing: { ...S.gerpCfg.closing, busy: true } };
  update();
  try {
    const r = await api("/api/gerps/close", { method: "POST", body: JSON.stringify(
      { gerp_id, confirm: CLOSE_PHRASE }) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok && r.status !== 202) throw new Error(r.status + " " + JSON.stringify(out));
    S.gerpCfg = { ...S.gerpCfg, closing: null };
    // `teardown: false` is the server saying closure is switched off — say so rather than claiming
    // an instance was destroyed when nothing was
    status(out.teardown
      ? "Closing. Your export is being written now; you can download it from this screen for 15 days."
      : "Closure requested. Nothing has been torn down — the teardown path is not enabled yet.",
      "ok");
    await loadGerps().catch(() => {});
  } catch (e) {
    console.error("[close ∇ERP]", e);
    S.gerpCfg = { ...S.gerpCfg, closing: { ...S.gerpCfg.closing, busy: false } };
    status("Couldn't close this gerp. Nothing has changed — try again in a moment.", "err");
  }
}

async function requestExport(remintOnly) {
  const gerp_id = S.current?.gerp_id;
  if (!gerp_id) return;
  S.gerpCfg = { ...S.gerpCfg, exBusy: true, exLinks: null };
  status(remintOnly ? "getting your download links…" : "starting your export…", "muted");
  try {
    const r = await api("/api/export", { method: "POST", body: JSON.stringify(
      remintOnly ? { gerp_id, credentials_only: true } : { gerp_id }) });
    const out = await r.json().catch(() => ({}));
    if (r.status === 202) {
      // the export is running; it can take minutes, so nothing to hand over yet
      status("Export started. It can take a few minutes for a large business — come back and "
           + "press Get download links.", "ok");
    } else if (r.status === 404) {
      status("No export yet — press Export my data first.", "err");
    } else if (r.ok && out.download) {
      S.gerpCfg = { ...S.gerpCfg, exLinks: out.download, exExpires: out.expires_at };
      status("", "");
    } else {
      throw new Error(r.status + " " + JSON.stringify(out));
    }
  } catch (e) {
    console.error("[export]", e);
    status("Couldn't reach your export. Nothing was changed — try again in a moment.", "err");
  } finally {
    S.gerpCfg = { ...S.gerpCfg, exBusy: false };
    update();
  }
}

// The SET of cards is Stripe's and is read every time it is shown — brand, last4 and expiry are
// Stripe's to change, so a copy here goes stale. What we store is which one is selected, and the
// list says that separately from the row flags: a gerp can select a card on the ACCOUNT's customer,
// which no row of the gerp's own list carries.
async function cards(body) {
  const r = await api("/api/billing/methods", { method: "POST", body: JSON.stringify(body) });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(out.error || r.status);
  return out;
}

async function loadCards() {
  try {
    const out = await cards({ action: "list" });
    const rows = (out.methods || []).map(m => ({ ...m, mine: true }));
    set({ acct: { ...S.acct, methods: rows, selected: out.selected || "" } });
  } catch (_) { /* the panel still renders the account default; a missing list is not an error */ }
}

// A gerp picks from its OWN cards and the account's, so this reads both. They are separate Stripe
// customers, so no method appears twice; the scope rides each row because it decides what may be
// removed here — an account card is removed from Info & Billing, where the guard sees every gerp.
async function loadGerpCards(gerp_id) {
  try {
    // ONE call. `list` returns every processor customer this payer can be charged against — its own
    // and whichever the selection points at — so the merge that used to happen here happens where
    // the contact is, and a card is never missed because the client asked the wrong question.
    const out = await cards({ action: "list", gerp_id });
    const rows = (out.methods || []).map(m => ({ ...m, mine: m.customer === out.own_customer }));
    const sel = out.selected || "";
    S.gerpCfg = { ...S.gerpCfg, cards: rows, cardSel: sel,
                  // a stored selection matching nothing left at Stripe: the issuer or the payer
                  // removed the card outside this app, and the next charge is going to fail
                  cardGone: !!sel && !rows.some(r => r.id === sel) };
    update();
  } catch (_) { S.gerpCfg = { ...S.gerpCfg, cards: [], cardSel: "", cardGone: false }; update(); }
}

async function addGerpCard() {
  // Same call the purchase flow makes, so a card added to a provisioned gerp and a card that pays
  // for a new one are one path. Wrapped because goToCheckout throws and a dead click says nothing.
  try { await goToCheckout(S.current.gerp_id, S.current.label); }
  catch (e) { status(payerMsg(e, "add ∇ERP card"), "err"); }
}

async function selectCard(id, gerp_id) {
  status("selecting…", "muted");
  try {
    await cards({ action: "select", payment_method_id: id, ...(gerp_id ? { gerp_id } : {}) });
    status("card selected", "ok");
    gerp_id ? await loadGerpCards(gerp_id) : await loadCards();
  } catch (e) { status(String(e.message || e), "err"); }
}

// Pay now: the seller charges the card this gerp selects, for the oldest unpaid hosting invoice.
// A decline says why and leaves the invoice owed; a success settles it and the row clears on the
// next daily read, so the panel stays until then and says so.
async function payNow() {
  const gerp_id = S.current?.gerp_id;
  S.gerpCfg = { ...S.gerpCfg, payBusy: true }; update();
  try {
    const r = await api("/api/billing/pay", { method: "POST", body: JSON.stringify({ gerp_id }) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.error || (r.status + " " + JSON.stringify(out)));
    status("Paid. The charge went through — this clears on the next daily check, and the chase stops now.", "ok");
    S.gerpCfg = { ...S.gerpCfg, payBusy: false, unpaid: null }; update();
  } catch (e) {
    console.error("[pay now]", e);
    S.gerpCfg = { ...S.gerpCfg, payBusy: false }; update();
    status("Couldn't charge that card: " + String(e.message || e) + ". Pick another, or use the link in the email.", "err");
  }
}

// A hosted link for an invoice the account still owes — the refusal on create names it.
async function openPayLink(invoice_id) {
  const win = window.open("about:blank", "gerp-pay");
  try {
    const r = await api("/api/billing/pay-link", { method: "POST", body: JSON.stringify({ invoice_id }) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok || !out.url) throw new Error(out.error || (r.status + " " + JSON.stringify(out)));
    if (win) win.location = out.url; else window.open(out.url, "gerp-pay");
  } catch (e) {
    if (win) win.close();
    console.error("[pay link]", e);
    status("Couldn't get a payment link: " + String(e.message || e), "err");
  }
}

async function deleteCard(id, gerp_id) {
  status("removing…", "muted");
  try {
    // a 409 names the gerp that is billed to it, which is the useful half of the refusal
    const out = await cards({ action: "delete", payment_method_id: id,
                              ...(gerp_id ? { gerp_id } : {}) });
    status(out.cleared_selection ? "card removed — nothing is selected now" : "card removed", "ok");
    gerp_id ? await loadGerpCards(gerp_id) : await loadCards();
  } catch (e) { status(String(e.message || e), "err"); }
}

// One row: a radio to bill this subject to it, the card, and Remove where removal belongs here.
// Two buttons, not a radio and a button. A radio that disables itself once chosen is the same
// state as a greyed button, said in a second vocabulary — and it sat oddly beside Remove.
//
// Both grey rather than disappear: a payer who cannot see an action wonders where it went. Neither
// disabled state is the guard — the server refuses a removal another gerp is billed to, and
// re-selecting the current card is a no-op — they just say so before the click.
const cardRowTpl = (m, sel, onSelect, onRemove, last) => {
  const isDefault = m.id === sel;
  return html`
  <div class="field" data-view="cardRow" data-pm=${m.id}>
    <span class="k">${isDefault ? "Billed to" : m.mine ? "Also saved" : "On your account"}</span>
    <span class="v">${m.brand ? cardBrand(m.brand) + " ····" + m.last4 : m.label}${m.exp ? " · exp " + m.exp : ""}
      <button class="ghost" data-view="defCardBtn" style="margin-left:.6rem"
              ?disabled=${isDefault} title=${isDefault ? "already the card this is billed to" : ""}
              @click=${() => onSelect(m.id)}>Default</button>
      ${onRemove ? html`<button class="ghost" data-view="delCardBtn" style="margin-left:.4rem"
                ?disabled=${last} title=${last ? "the only card on file — add another first" : ""}
                @click=${() => onRemove(m.id)}>Remove</button>` : nothing}</span>
  </div>`;
};

async function addDefaultCard() {
  // No gerp_id: the ACCOUNT is the subject, which is what makes this the one path to a card that
  // does not require creating a gerp first.
  const back = S.view;
  set({ view: "loadingScreen", loadMsg: "Loading payment screen…", status: { msg: "", cls: "" } });
  try {
    await askStripe({ return_url: location.origin + "/?billing=account" }, back);
  } catch (e) {
    set({ loadMsg: "", view: back });
    console.error("[add default card]", e);
    status("Couldn't reach the payment page. Nothing has been charged and no card was saved — "
         + "try again in a moment.", "err");
  }
}
// A button whose handler is still running shows the spinner in place of its label, disabled, so
// the click has feedback where it happened; the status line at the bottom still says what came
// of it. `key` names the button in S.busy; one at a time is the whole model.
const working = (key, fn) => async (ev) => { set({ busy: key }); try { await fn(ev); } finally { set({ busy: null }); } };
const btn = (key, label, fn, view) => html`<button data-view=${view} ?disabled=${S.busy === key} @click=${working(key, fn)}>${S.busy === key ? html`<span class="spin"></span>` : label}</button>`;

const ACCT_FIELDS = ["first", "middle", "last", "phone", "street", "unit", "city", "state", "zip", "country"];
async function doSaveAcct() {
  const f = {}; ACCT_FIELDS.forEach(k => f[k] = ($("acct-" + k)?.value || "").trim());
  if (!f.first || !f.last) { status("first and last required", "err"); return; }
  status("saving…", "muted");
  try {
    const r = await api("/api/account", { method: "POST", body: JSON.stringify(f) });
    const t = await r.text(); if (!r.ok) throw new Error(r.status + " " + t);
    const a = JSON.parse(t);
    set({ acct: { ...S.acct, missing: a.missing || [] } });
    status(a.missing?.length ? "saved — still missing " + missingNames(a.missing) : "saved — your account is complete", a.missing?.length ? "muted" : "ok");
  } catch (e) { status(String(e), "err"); }
}

// The create screen. The business profile inputs are uncontrolled: cleared after the render,
// filled by the paste button, preserved by lit across the re-renders that follow.
const BIZ_FIELDS = ["name", "email", "phone", "street", "unit", "city", "state", "zip", "country", "tax_id"];
// India, as the form and Places write it; an Indian business saves its card on the card page
const isIndia = (c) => ["IN", "IND", "INDIA"].includes(String(c || "").trim().toUpperCase());
// the public profile is a choice of legal fields, one box per field
const PUB_CHOICES = [["email", "Email"], ["phone", "Phone"], ["street", "Street"], ["unit", "Unit"],
                     ["city", "City"], ["state", "State"], ["zip", "Zip"], ["country", "Country"]];
const ALL_PUBLIC = () => Object.fromEntries(PUB_CHOICES.map(([k]) => [k, true]));
const BIZ_REQUIRED = ["name", "email", "phone", "street", "city", "state", "zip", "country"];
const BIZ_FIELD_NAMES = { name: "business name", email: "email", phone: "phone", street: "street address",
                          city: "city", state: "region", zip: "postal code", country: "country",
                          tax_id: "GSTIN (15 characters, or leave it empty)" };
async function openCreate() {
  set({ view: "createGerpScreen", createOO: true, createAck: false, createRegionPicked: false, status: { msg: "", cls: "" },
        biz: { ac: [], acOpen: false, lat: null, lng: null },
        pub: { links: [], ac: [], acOpen: false, linkMenuOpen: false, lat: null, lng: null } });
  loadCreateCards();
  loadCreateRegions();
  loadCapacity();
  BIZ_FIELDS.forEach(k => { const el = $("biz-" + k); if (el) el.value = ""; });
  $("biz-email").value = claims().email || "";
  set({ pubFields: ALL_PUBLIC() });
  loadAccountGate();
}

// What the account still lacks before it can create a gerp or publish a profile. Read off
// GET /api/account (`missing`); the create and publish routes refuse with the same list.
const ACCOUNT_FIELD_NAMES = { first: "first name", last: "last name", phone: "phone", street: "street address",
                              city: "city", state: "region", zip: "postal code", country: "country" };
const missingNames = (m) => (m || []).map(k => ACCOUNT_FIELD_NAMES[k] || k).join(", ");
// A card, no field list — the inputs are the list. On a door it says where to go; on Info &
// Billing itself it says why the fields matter.
const accountGateTpl = () => (S.acct?.missing?.length && !S.acct.loading) ? html`
  <div class="panel gate" data-view="accountGate">${
    S.view === "accountInfoAndBillingScreen"
      ? html`Still missing: ${missingNames(stillMissing())}.`
      : html`Complete your <a @click=${openInfo}>Account › Info &amp; Billing</a> to continue.`}</div>` : nothing;
// the saved record's gaps less what is typed in the fields right now: the line shrinks as the
// owner fills the form, before Save says so
const stillMissing = () => (S.acct?.missing || []).filter(k => !($("acct-" + k)?.value || "").trim());
// on leaving a field that now holds something: the line drops it, without a re-render per key
const onAcctInput = (e) => { if (S.view === "accountInfoAndBillingScreen" && (!e || (e.target?.value || "").trim())) update(); };
async function loadAccountGate() {
  // `loading` is Info & Billing's spinner state and starts true; this read is not that screen's
  // load, so it clears the flag along with setting what it came for. Returns the record, so the
  // create screen fills its legal profile off the same read.
  try { const r = await api("/api/account"); if (r.ok) { const a = await r.json(); set({ acct: { ...(S.acct || {}), missing: a.missing || [], loading: false } }); return a; } }
  catch (_) {}
  return null;
}

// What Cognito refuses, in the words a person can act on. Anything else surfaces as it came.
const EMAIL_REFUSALS = {
  AliasExistsException:     "that address is already another account's login",
  InvalidParameterException: "enter a different, valid email address",
  CodeMismatchException:    "wrong code — press Change again for a new one",
  ExpiredCodeException:     "that code expired — press Change again for a new one",
  LimitExceededException:   "too many attempts — try again later",
};
const emailRefusal = (e) => EMAIL_REFUSALS[e.code] || String(e.message || e);

// Step one of changing the login: ask Cognito to send a code to the NEW address. The pool keeps
// the old address as the login until the code confirms, so nothing here is irreversible.
async function changeEmail() {
  const next = $("acct-email").value.trim().toLowerCase();
  if (!next || next === (claims().email || "").toLowerCase()) { status("enter a different email address", "err"); return; }
  if (!accessToken()) { status("sign in again to change your email", "err"); return; }
  status("sending a code…", "muted");
  try {
    await cognito("UpdateUserAttributes", { AccessToken: accessToken(),
                                            UserAttributes: [{ Name: "email", Value: next }] });
    set({ acct: { ...S.acct, emailStep: "code", pendingEmail: next } });
    status("code sent to " + next, "ok");
  } catch (e) { status(emailRefusal(e), "err"); }
}

// Step two: the code confirms, the login switches, the tokens refresh so the id token carries the
// new address, and one GET /api/account writes the row from the claim — the client never names
// an email to the BFF.
async function confirmEmail() {
  const code = $("acct-email-code").value.trim();
  if (!code) { status("enter the code", "err"); return; }
  status("confirming…", "muted");
  try {
    await cognito("VerifyUserAttribute", { AccessToken: accessToken(), AttributeName: "email", Code: code });
    await refresh();
    const r = await api("/api/account");
    const a = r.ok ? await r.json() : {};
    set({ acct: { ...S.acct, emailStep: null, pendingEmail: "" } });
    $("acct-email").value = a.email || claims().email || "";
    status("email changed", "ok");
  } catch (e) { status(emailRefusal(e), "err"); }
}

// ---------- view templates ----------

// Glyph path data, kept OUT of the markup it decorates. Each of these is one opaque 400–1500 char
// string; inline, they made the four anchors that carry them the widest lines in the file and any
// diff on an href or a title unreadable. Here they are obviously data, and the anchors are markup.
const ICON = {
  discord: "M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z",
  github: "M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12",
  docs: "M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z",
  contact: "M8 1a5 5 0 0 0-5 5v1h1a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V6a6 6 0 1 1 12 0v6a2.5 2.5 0 0 1-2.5 2.5H9.366a1 1 0 0 1-.866.5h-1a1 1 0 1 1 0-2h1a1 1 0 0 1 .866.5H11.5A1.5 1.5 0 0 0 13 12h-1a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h1V6a5 5 0 0 0-5-5",
};
const glyph = (name, viewBox = "0 0 24 24") =>
  html`<svg class="ico" viewBox=${viewBox} aria-hidden="true"><path d=${ICON[name]}/></svg>`;

const wordmark = html`<span class="nabla">∇</span><span class="brand">gradient<span class="g">ERP</span></span>`;
const bigLogo = html`<span class="wm-big" role="img" aria-label="gradientERP"></span>`;
const homeLogo = html`<span class="logo to-home" role="button" tabindex="0" title="Home" @click=${goTo("homeScreen")}>${wordmark}</span>`;
const appbar = (chipId) => html`<div class="appbar">${homeLogo}<span class="sp"></span><span class="chip" data-view="whoChip">${claims().email || ""}</span><a data-view="logoutLink" @click=${logout}>log out</a></div>`;
// Which scope a screen belongs to. Every screen past home is either the ACCOUNT's or one GERP's,
// and nothing else on the page says which — the heading is "Info & Billing" either way. The first
// segment is the home section the screen came from, and clicking it goes back there.
const crumb = (root, leaf) => html`<nav class="crumb" data-view="crumb" aria-label="Where you are"><a data-view="crumbRoot" @click=${goTo("homeScreen")}>${root}</a><span class="sep">›</span><span data-view="crumbLeaf">${leaf}</span></nav>`;

const loginTpl = () => html`
  <div class="login"><p class="agents-note">agents: this console is a client-rendered app behind login — read https://gradienterp.cloud/llms.txt for the site guide, the API surface, and still-frame transcripts of the demos.</p><div class="login-hero">
    ${bigLogo}
    <p class="tag">ai first, cloud native ERP</p>
    <p class="thesis">supplying the <span class="g">gradient</span> r&amp;d and capital need through <a href="https://openlyoperated.biz" target="_blank" rel="noopener" style="color:var(--acc);text-decoration:underline;text-underline-offset:2px;">business transparency</a></p>
    <button @click=${login}>Log in</button>
    <p class="alt"><a @click=${goTo("signupScreen")}>Create an account</a></p>
    <div class="demos">
      ${DEMOS.map((d) => html`
        <div class="demo-row" @click=${openDemo(d)}>
          <div class="meta">
            <div class="kicker">${d.mod} · <span class="role">${d.role}</span></div>
            <div class="prompt">${d.prompt}</div>
          </div>
          <div class="thumb"><img src=${demoThumb(d)} alt="${d.mod} demo" @error=${(e) => e.target.remove()} />gif</div>
        </div>`)}
    </div>
    <div class="login-icons">
      <a class="discord" href="https://discord.gg/87FHhUhQmK" target="_blank" rel="noopener" title="Community Discord" aria-label="Community Discord">${glyph("discord")}</a>
      <a class="github" href="https://github.com/systemaccounting/gradienterp" target="_blank" rel="noopener" title="Source on GitHub" aria-label="Source on GitHub">${glyph("github")}</a>
      <a class="docs" href="https://github.com/systemaccounting/gradienterp/blob/main/docs/AGENTS.md" target="_blank" rel="noopener" title="docs for agents" aria-label="docs for agents">${glyph("docs")}</a>
      <a class="contact" href="/support" target="_blank" rel="noopener" title="Private support" aria-label="Private support">${glyph("contact", "0 0 16 16")}</a>
    </div>
  </div>
  <footer class="login-foot"><a data-view="termsLink" href=${TERMS_URL} target="_blank" rel="noopener">Terms</a><a data-view="privacyLink" href=${PRIVACY_URL} target="_blank" rel="noopener">Privacy</a><a data-view="dpaLink" href=${DPA_URL} target="_blank" rel="noopener">DPA</a></footer></div>`;

const signupTpl = () => html`<div class="auth">${bigLogo}
  <h2 class="title">Create your account</h2>
  <div class="row2"><div><label for="su-first">First name</label><input id="su-first" autocomplete="given-name" /></div><div><label for="su-last">Last name</label><input id="su-last" autocomplete="family-name" /></div></div>
  <label for="su-email">Email</label><input id="su-email" type="email" autocomplete="email" placeholder="you@company.com" />
  <label for="su-pass">Password</label><input id="su-pass" type="password" autocomplete="new-password" placeholder="8+ chars, upper, lower, number" />
  <button @click=${doSignup}>Create account</button>
  <p class="alt"><a @click=${goTo("landingScreen")}>← back to log in</a></p></div>`;

const confirmTpl = () => html`<div class="auth">${bigLogo}
  <h2 class="title">Check your email</h2>
  <p class="sub">We sent a verification code${S.pendingEmail ? " to " + S.pendingEmail : ""}.</p>
  <p class="spamhint">Don't see it? Check your spam folder.</p>
  <label for="cf-code">Verification code</label><input id="cf-code" inputmode="numeric" autocomplete="one-time-code" placeholder="123456" />
  <button @click=${doConfirm}>Confirm &amp; continue</button>
  <p class="alt"><a @click=${goTo("landingScreen")}>← back to log in</a></p></div>`;

// One card per row status. awaiting_payment, queued and provisioning are all "not up"; the last
// two are a machine working (a queued gerp says how many are in line before it) while the first
// is a purchase nobody finished: a spinner on it is a lie that never resolves. closing and stopped are stacks going or gone with no screen behind
// them, so no chevron; a closed gerp opens because its export links are still minted for
// fifteen days.
const money = (n) => "$" + Number(n || 0).toFixed(2);
const gerpCardTpl = (g) => g.unpaid
  ? html`<div class="card pay" data-view="gerpCard" @click=${() => enterGerp(g.gerp_id, g.label)}><div class="ic">⚠️</div><div class="body"><div class="name">${g.label}</div><div class="meta">payment failed · ${money(g.unpaid.total)} past due${g.unpaid.closes_on ? " · closes " + g.unpaid.closes_on : ""}</div></div><div class="chev">→</div></div>`
  : g.status === "awaiting_payment"
  ? html`<div class="card pay" data-view="gerpCard" @click=${() => resumeCheckout(g)}><div class="ic">💳</div><div class="body"><div class="name">${g.label}</div><div class="meta">${
      g.prior ? `held — that card's earlier account closed owing $${g.prior.balance_owed.toFixed(2)}; settle it or add another card`
      : g.held === "limit" ? "held — this account holds as many ∇ERPs as it can; close one, or ask support for more"
      : g.held === "capacity" ? "held — no room for a new ∇ERP right now; select the card again later"
              : "waiting on payment method — add a card to finish"}</div></div><div class="chev">→</div></div>`
  : g.status === "queued"
  ? html`<div class="card prov" data-view="gerpCard"><div class="ic"><span class="spin"></span></div><div class="body"><div class="name">${g.label}</div><div class="meta">${g.ahead ? `in line · ${g.ahead} ahead of you` : "in line · up next"}</div></div></div>`
  : g.status === "provisioning"
  ? html`<div class="card prov" data-view="gerpCard"><div class="ic"><span class="spin"></span></div><div class="body"><div class="name">${g.label}</div><div class="meta">provisioning… about 25 minutes — email sent when ready</div></div></div>`
  : g.status === "close_requested" || g.status === "closing"
  ? html`<div class="card prov" data-view="gerpCard"><div class="ic"><span class="spin"></span></div><div class="body"><div class="name">${g.label}</div><div class="meta">closing… exporting your books first</div></div></div>`
  : g.status === "closed"
  ? html`<div class="card" data-view="gerpCard" @click=${() => enterGerp(g.gerp_id, g.label)}><div class="ic">📦</div><div class="body"><div class="name">${g.label}</div><div class="meta">closed${g.download_until ? " · export downloadable until " + g.download_until.slice(0, 10) : ""}</div></div><div class="chev">→</div></div>`
  : g.status === "stopped"
  ? html`<div class="card prov" data-view="gerpCard"><div class="ic">⏸</div><div class="body"><div class="name">${g.label}</div><div class="meta">stopped · books exported, account kept</div></div></div>`
  : html`<div class="card" data-view="gerpCard" @click=${() => enterGerp(g.gerp_id, g.label)}><div class="ic init">${(g.label || "·").trim()[0].toUpperCase()}</div><div class="body"><div class="name">${g.label}</div><div class="meta">ERP instance</div></div><div class="chev">→</div></div>`;
const homeTpl = () => {
  const shown = new Set(S.gerps.map(g => g.gerp_id));
  const pendingOnly = Object.entries(S.pending).filter(([id]) => !shown.has(id));
  const empty = !S.gerps.length && !pendingOnly.length;
  return html`
    ${appbar()}
    <div class="sec">Your ∇ERPs</div>
    <div class="cards" data-view="gerpsTable">
      ${!S.gerpsLoaded ? html`<div class="muted" style="padding:.4rem 0 .2rem">Loading your ∇ERPs…</div>` : empty ? html`<div class="muted" style="padding:.4rem 0 .2rem">No ∇ERPs yet — create one below.</div>` : nothing}
      ${S.gerps.map(gerpCardTpl)}
      ${pendingOnly.map(([, label]) => html`<div class="card prov"><div class="ic"><span class="spin"></span></div><div class="body"><div class="name">${label}</div><div class="meta">provisioning… about 25 minutes — email sent when ready</div></div></div>`)}
    </div>
    <div class="sec">Account</div>
    <div class="cards">
      <div class="card"
           @click=${openCreate}>
        <div class="ic">＋</div>
        <div class="body">
          <div class="name">Create a ∇ERP</div>
          <div class="meta">spin up a new ERP instance — its own books, agent, gateway</div>
        </div>
        <div class="chev">→</div>
      </div>
      <div class="card" @click=${openPublic}><div class="ic">◎</div><div class="body"><div class="name">Public profile</div><div class="meta">your platform-wide identity, referenced across businesses</div></div><div class="chev">→</div></div>
      <div class="card" @click=${openInfo}><div class="ic">𝍌</div><div class="body"><div class="name">Info &amp; Billing</div><div class="meta">your name, email, and payment details</div></div><div class="chev">→</div></div>
    </div>`;
};

const linksTpl = (form = "pu") => html`${S[form].links.map((lk, i) => html`<div class="link-row">
  <span class="link-tag">${LINK_LABEL[lk.type] || "Link"}</span>
  <input type="url" .value=${lk.url || ""} placeholder=${LINK_PH[lk.type] || "https://…"} @input=${e => { S[form].links[i].url = e.target.value; }} />
  <button type="button" class="link-x" title="Remove" @click=${() => removeLink(i, form)}>×</button></div>`)}`;
// the list plus the add menu, as one block
const linksBlockTpl = (form = "pu") => html`
  <div id="${form}-links">${linksTpl(form)}</div>
  <div class="linkwrap"><button type="button" class="link-add" @click=${() => set({ [form]: { ...S[form], linkMenuOpen: !S[form].linkMenuOpen } })}>+ Add link ▾</button>
    <div class="link-menu ${S[form].linkMenuOpen ? "open" : ""}">${LINK_TYPES.map(t => html`<div class="link-opt" @click=${() => addLink(t.id, form)}>${t.label}</div>`)}</div></div>`;
const publicuserTpl = () => html`
  ${appbar()}
  ${crumb("Account", "Public profile")}
  <h2 class="title">Public profile</h2>
  <p class="sub">Your platform-wide profile, referenced across the businesses you work with.
    Info saved here is published at <a data-view="profileUrl" href=${profileUrl()} target="_blank" rel="noopener">${profileUrl().replace(/^https?:\/\//, "")}</a>.</p>
  ${accountGateTpl()}
  <div class="verify-row"><span class="vbadge ${S.pu.verified ? "ok" : ""}">${S.pu.verified ? "Verified" : "Unverified"}</span><button class="ghost" disabled title="coming soon">Verify me</button></div>
  <div class="sec">Occupation — history</div>
  ${measuredTpl(S.pu.soc, "hours", S.pu.soc_window, "occupation")}
  <div class="sec">Name</div>
  <div class="row3">
    <div><label for="pu-first">First</label><input id="pu-first" autocomplete="given-name" /></div>
    <div><label for="pu-middle">Middle</label><input id="pu-middle" autocomplete="additional-name" /></div>
    <div><label for="pu-last">Last</label><input id="pu-last" autocomplete="family-name" /></div>
  </div>
  <div class="sec">Address</div>
  ${addressFieldsTpl("pu")}
  <div class="sec">Contact</div>
  <label for="pu-email">Email address</label><input id="pu-email" type="email" autocomplete="email" />
  <label for="pu-phone">Phone number</label><input id="pu-phone" type="tel" autocomplete="tel" />
  <div class="sec">Links</div>
  ${linksBlockTpl("pu")}
  <button id="savepublic" @click=${doSavePublic}>Save profile</button>`;

// A measured distribution — what a person does, or what a business sells — read off the profile
// row. Written by the platform's count, never by an owner; the screen only shows it.
const measuredTpl = (rows, weight, window, what) => (rows || []).length
  ? html`<div class="panel" data-view="measured-${what}">
      ${rows.map(r => html`<div class="field"><span class="k">${r.code}</span>
        <span class="v">${Math.round(r.share * 100)}% · ${weight === "hours" ? `${Math.round(r.hours)} h` : money(r.revenue)}</span></div>`)}
    </div>
    <p class="sub" style="margin-top:.5rem; font-size:.84rem;">Counted from the books over ${window || "the recorded work"}. Nothing here is typed.</p>`
  : html`<div class="panel" data-view="measured-${what}"><div class="field"><span class="k">${what === "occupation" ? "Occupation" : "Industry"}</span><span class="v muted">nothing yet</span></div></div>
    <p class="sub" style="margin-top:.5rem; font-size:.84rem;">Counted from ${what === "occupation" ? "the tasks and hours logged against you" : "the invoices this business issues"} once there are some. Nothing here is typed.</p>`;

const infobillingTpl = () => html`
  ${appbar()}
  ${crumb("Account", "Info & Billing")}
  <h2 class="title">Info &amp; Billing</h2>
  <p class="sub">The details you signed up with, and how you pay for ERP instances.</p>
  <div class="sec">Your info</div>
  <p class="sub" style="font-size:.84rem;">Personal information serving compliance and stays private. This information is required to create a ∇ERP and a separate public profile.</p>
  ${accountGateTpl()}
  <div class="row2"><div><label for="acct-first">First name</label><input id="acct-first" @blur=${onAcctInput} autocomplete="given-name" /></div><div><label for="acct-middle">Middle <span class="muted" style="font-weight:400">— optional</span></label><input id="acct-middle" autocomplete="additional-name" /></div></div>
  <label for="acct-last">Last name</label><input id="acct-last" @blur=${onAcctInput} autocomplete="family-name" />
  <label for="acct-phone">Phone</label><input id="acct-phone" @blur=${onAcctInput} type="tel" autocomplete="tel" />
  ${addressFieldsTpl("acct")}
  ${btn("saveAcct", "Save", doSaveAcct, "saveAcctBtn")}
  <div class="sec">Login email</div>
  <label for="acct-email">Email <span class="muted" style="font-weight:400">— a change is confirmed by a code sent to the new address</span></label>
  <div class="row2">
    <input id="acct-email" data-view="acctEmailInput" type="email" autocomplete="email" ?disabled=${S.acct.emailStep === "code"} />
    <button class="ghost" data-view="changeEmailBtn" ?disabled=${S.acct.emailStep === "code"} @click=${changeEmail}>Change</button>
  </div>
  ${S.acct.emailStep === "code" ? html`
    <label for="acct-email-code">Code <span class="muted" style="font-weight:400">— sent to ${S.acct.pendingEmail}</span></label>
    <div class="row2">
      <input id="acct-email-code" data-view="emailCodeInput" inputmode="numeric" autocomplete="one-time-code" placeholder="123456" />
      <button data-view="confirmEmailBtn" @click=${confirmEmail}>Confirm</button>
      <button class="ghost" @click=${() => set({ acct: { ...S.acct, emailStep: null, pendingEmail: "" } })}>Cancel</button>
    </div>` : nothing}
  <div class="sec">Billing</div>
  ${billingPanelTpl()}
  <div class="sec">Deleting</div>
  <p class="sub">Erase your account from the platform. Every ∇ERP of yours has to be closed first.</p>
  <button class="ghost danger-text" data-view="deleteAccountBtn" @click=${openDelete}>Delete my account</button>
  ${helpLink}`;

// Stripe's brand is a lowercase slug (`visa`, `amex`, `mastercard`). The ones that are acronyms
// stay uppercase; the rest read as words.
const CARD_BRANDS = { amex: "AMEX", jcb: "JCB", unionpay: "UnionPay", diners: "Diners Club" };
const cardBrand = (b) => CARD_BRANDS[b] || (b ? b[0].toUpperCase() + b.slice(1) : "Card");

// A gerp is what gets billed — its own AWS account, its own invoice — so its card hangs off it.
// This is the ACCOUNT's optional default: what a gerp with no card of its own falls back to, and
// the only card settable without creating a gerp.
const billingPanelTpl = () => {
  const pm = S.acct.payment_method;
  return html`
    <div class="panel">
      ${(S.acct.methods || []).length ? (S.acct.methods || []).map(m =>
        cardRowTpl(m, S.acct.selected, id => selectCard(id), id => deleteCard(id),
                   (S.acct.methods || []).length === 1)) : html`
        <div class="field">
          <span class="k">Payment method</span>
          <span class="v ${pm ? "" : "muted"}" data-view="acctCard">${
            S.acct.loading ? "…"
              : pm ? `${cardBrand(pm.brand)} ····${pm.last4}${pm.exp ? " · exp " + pm.exp : ""}`
                   : "none on file"}</span>
        </div>`}
    </div>
    <button class="ghost" data-view="addCardBtn" ?disabled=${S.acct.loading} @click=${addDefaultCard}>${
      "Add card"}</button>
    <p class="sub" style="margin-top:.85rem; font-size:.84rem;">A new ∇ERP starts with your default
      card selected. Each ∇ERP is billed on its own monthly invoice.</p>`;
};

// The disclosure text and its version come from the SHELL, where the BFF inlined
// web/purchase-terms.txt at serve time — one source of truth, plain enough to read on GitHub or at
// /purchase-terms.txt. The version is that file's content hash, and it is what the create request
// records as terms_version.
const _termsEl = () => document.getElementById("purchase-terms");
const PURCHASE_TERMS = () => (_termsEl()?.textContent || "").trim();
// the documents are pages of the public repo, not routes of this app: the terms, and beside them
// the privacy notice and the DPA when they land under docs/
// where a person's public profile is served: the public surface, by the profile's id (the sub)
const profileUrl = () => "https://openlyoperated.biz/p/" + (claims().sub || "");
const bizPageUrl = (gerp_id) => "https://openlyoperated.biz/#b/" + encodeURIComponent(gerp_id || "");
const TERMS_URL = "https://github.com/systemaccounting/gradienterp/blob/main/docs/TERMS.md";
const PRIVACY_URL = "https://github.com/systemaccounting/gradienterp/blob/main/docs/PRIVACY.md";
const DPA_URL = "https://github.com/systemaccounting/gradienterp/blob/main/docs/DPA.md";
const PURCHASE_TERMS_VERSION = () => _termsEl()?.dataset.version || "";

// The business profile — legal (required) and public (optional) — as the create screen asks it and
// the gerp screen edits it. One set of inputs (`biz-*`, `pub-*`): one screen is on at a time.
// the public fields matter only while the gerp is openly operated: the create screen's box, or
// the gerp screen's toggle
const openlyOperated = () => S.view === "createGerpScreen" ? !!S.createOO : !!S.gerpCfg?.openly_operated;
const businessProfileTpl = () => html`
  <label>Business profile</label>
  <div class="disc legal">
    <div class="row2"><div><label for="biz-email">Email</label><input id="biz-email" type="email" autocomplete="email" /></div><div><label for="biz-phone">Phone</label><input id="biz-phone" type="tel" autocomplete="tel" /></div></div>
    ${addressFieldsTpl("biz")}
    <div ?hidden=${!isIndia($("biz-country")?.value)} data-view="gstinField"><label for="biz-tax_id">GSTIN <span class="s-desc">(optional)</span></label><input id="biz-tax_id" autocomplete="off" maxlength="15" placeholder="22AAAAA0000A1Z5" /></div>
  </div>
  <div class="disc public ${openlyOperated() ? "" : "off"}" data-view="publicFields">
    <div class="sec">Public fields</div>
    <p class="disc-note">Published like yellow pages, not required. The rest stays private.</p>
    <div class="pub-grid">${PUB_CHOICES.map(([key, label]) => html`
      <label class="pub-field" data-view="pubField" data-field=${key}>
        <input type="checkbox" .checked=${!!S.pubFields[key]} @change=${e => set({ pubFields: { ...S.pubFields, [key]: e.target.checked } })} />
        <span>${key === "state" ? regionWord($("biz-country")?.value) : key === "zip" ? postalWord($("biz-country")?.value) : label}</span>
      </label>`)}</div>
    <div class="sec">Links</div>
    ${linksBlockTpl("pub")}
  </div>`;

const creategerpTpl = () => html`
  ${appbar()}
  ${crumb("Account", "Create a ∇ERP")}
  <h2 class="title">Create a ∇ERP</h2>
  <p class="sub">An ERP instance with its own books, agent, and gateway. You save a card next; it spins up after that — about 25 minutes, email sent when ready.</p>
  ${accountGateTpl()}
  <label for="bizname">Business name</label><input id="bizname" placeholder="e.g. Blue Bottle" autocomplete="off" .value=${S.createName || ""} />
  <label class="setting" style="margin-top:.8rem;">
    <input type="checkbox" .checked=${S.createOO}
           @change=${e => set({ createOO: e.target.checked, pubFields: e.target.checked ? ALL_PUBLIC() : {} })} />
    <span class="s-body">
      <span class="s-name">Openly operated</span>
      <span class="s-desc">publish to openlyoperated.biz to invite help (<em>currently ${S.createOO ? "public" : "private"}</em>)</span>
    </span>
  </label>
  ${businessProfileTpl()}
  ${regionTpl()}
  <div class="sec">Pay with</div>
  ${payWithTpl()}
  <div class="sec">What you are buying</div>
  <p class="terms" data-view="purchaseTerms">${PURCHASE_TERMS()}</p>
  <p class="sub" style="font-size:.84rem;">The <a data-view="purchaseTermsLink" href=${TERMS_URL} target="_blank" rel="noopener">terms</a> cover your data, what you may send other businesses, and non-payment. The <a data-view="purchasePrivacyLink" href=${PRIVACY_URL} target="_blank" rel="noopener">privacy notice</a> covers your account data, and the <a data-view="purchaseDpaLink" href=${DPA_URL} target="_blank" rel="noopener">DPA</a> the data your business keeps in its ∇ERP.</p>
  <label class="setting"><input type="checkbox" data-view="purchaseAck" .checked=${S.createAck} @change=${e => set({ createAck: e.target.checked })} /><span class="s-body"><span class="s-name">I read &amp; understand</span></span></label>
  <div class="sec">Agent email</div>
  <p class="sub">Once it spins up, email your agent at an address tied to <strong>${claims().email || ""}</strong>. Verify the confirmation email once so it can reply.</p>
  ${S.createOwed?.invoices?.length ? html`<p class="sub" data-view="owedPanel">Owed from an earlier account: ${money(S.createOwed.balance)}.
    ${S.createOwed.invoices.map(inv => html`<button class="ghost" data-view="owedPayBtn" @click=${() => openPayLink(inv)}>Pay ${inv}</button> `)}</p>` : nothing}
  ${capacityLine()}
  <button data-view="createGerpBtn" style="margin-top:.9rem;" ?disabled=${!S.createAck || !S.createPick} @click=${doCreate}>Create</button>
  ${helpLink}`;

// the community Discord, at the foot of a screen where someone may get stuck
const helpLink = html`<p class="help-link"><a href="https://discord.gg/87FHhUhQmK" target="_blank" rel="noopener" data-view="discordHelp">${glyph("discord")} Request help in Discord</a></p>`;

// The account's cards, the default first and selected; *add a card* last. A gerp pays with the row
// it picks. "Default" means which row starts selected and nothing else.
const payWithTpl = () => {
  const cards = S.createCards;
  if (cards === null) return html`<div class="setting" data-view="payWithLoading"><span class="spin"></span><span class="s-body"><span class="s-name muted">your saved cards…</span></span></div>`;
  const row = (id, label, view) => html`
    <label class="setting" data-view=${view} data-pm=${id}>
      <input type="radio" name="paywith" .checked=${S.createPick === id} @change=${() => set({ createPick: id })} />
      <span class="s-body"><span class="s-name">${label}</span></span>
    </label>`;
  return html`
    ${cards.map(m => row(m.id, `${cardBrand(m.brand)} ····${m.last4}${m.exp ? " · exp " + m.exp : ""}`, "payWithRow"))}
    ${row("new", cards.length ? "Add a card" : "No saved cards. Create a ∇ERP to add a card.", "payWithAddCard")}`;
};
// Where the gerp is built. The dropdown is the platform's list; the address's country picks the
// default (the server maps it), and an owner may choose another. A region marked "not yet" is
// shown grayed with the note: AWS has not reached it.
// the org's account count against its quota, on the create screen above the button: what a
// create takes one of. Not on the landing screen — an account there is a gradientERP login
async function loadCapacity() {
  try {
    const r = await api("/api/capacity");
    set({ capacity: r.ok ? await r.json() : null });
  } catch (_) { set({ capacity: null }); }
}
const capacityLine = () => S.capacity
  ? html`<p class="capacity" data-view="capacity">${S.capacity.available} ${S.capacity.available === 1 ? "account" : "accounts"} currently available in the AWS org</p>`
  : nothing;

async function loadCreateRegions(country = "") {
  try {
    const r = await api("/api/regions" + (country ? "?country=" + encodeURIComponent(country) : ""));
    const out = r.ok ? await r.json() : {};
    const regions = out.regions || [];
    // the owner's own pick stays; otherwise the address's country decides
    const keep = S.createRegionPicked && regions.some(x => x.id === S.createRegion && x.status === "offered");
    set({ createRegions: regions, createRegion: keep ? S.createRegion : (out.default || regions[0]?.id || "") });
  } catch (_) { set({ createRegions: [], createRegion: "" }); }
}

const regionTpl = () => S.createRegions.length ? html`
  <div class="sec">Region</div>
  <p class="sub">Your books, documents and agent live here. The region nearest your business address is picked for you.</p>
  <select id="region" data-view="regionSelect" .value=${S.createRegion} @change=${e => set({ createRegion: e.target.value, createRegionPicked: true })}>
    ${S.createRegions.map(r => html`<option value=${r.id} ?disabled=${r.status !== "offered"} ?selected=${r.id === S.createRegion}>${r.label}${r.status !== "offered" ? " — not yet" : ""}</option>`)}
  </select>` : nothing;

async function loadCreateCards() {
  set({ createCards: null, createPick: "" });
  try {
    const r = await api("/api/billing/methods", { method: "POST", body: JSON.stringify({ action: "list" }) });
    const out = r.ok ? await r.json() : {};
    const cards = out.methods || [];
    const def = cards.find(m => m.selected)?.id || cards[0]?.id || "new";
    set({ createCards: cards, createPick: def });
  } catch (_) { set({ createCards: [], createPick: "new" }); }
}

// The export section. It outlives the instance: a closed gerp's screen is this and nothing else,
// minting links for fifteen days off the stack that stays. `live` false hides "Export my data" —
// with the tables gone there is nothing to export, only the copy already made to download.
const yourDataTpl = (c, live = true) => html`
    <div class="sec">Your data</div>
    <p class="sub">${live
      ? "A copy of everything this business holds — books, invoices, contacts, inventory, and every document in the filing cabinet. Yours to take, any time."
      : "The copy of everything this business held, made before the instance was removed. Download it while the account stands."}</p>
    <div class="panel">
      <div class="field">
        <span class="k">Export</span>
        <span class="v muted" data-view="exportState">${
          c.exBusy ? "working…" : c.exLinks ? "ready" : live ? "run one to get a copy" : "press Get download links"}</span>
      </div>
    </div>
    ${c.exLinks ? html`
      <p class="sub" style="margin-top:.85rem; font-size:.84rem;">Download the script for your
        machine and run it. It carries its own credentials${c.exExpires ? " and expires in about an hour" : ""}
        — if it stops working, press Get download links again.</p>
      <div class="cards">
        ${Object.entries(c.exLinks).map(([name, url]) => html`
          <a class="card" data-view="exportLink" href=${url}>
            <div class="ic">⤓</div>
            <div class="body">
              <div class="name">${name}</div>
              <div class="meta">${name.endsWith(".ps1") ? "Windows PowerShell" : "macOS or Linux"}</div>
            </div>
            <div class="chev">→</div>
          </a>`)}
      </div>` : nothing}
    ${live ? html`<button class="ghost" data-view="exportBtn" ?disabled=${c.exBusy}
            @click=${() => requestExport(false)}>Export my data</button>` : nothing}
    <button class="ghost" data-view="exportLinksBtn" ?disabled=${c.exBusy}
            @click=${() => requestExport(true)}>Get download links</button>`;

// A gerp with no stack behind it: closed, closing, or stopped. Owner controls need the instance;
// what is left is the export, and for a closed gerp the date the account goes.
const GONE = { close_requested: "closing", closing: "closing", closed: "closed", stopped: "stopped" };
const goneGerpTpl = (c) => {
  const day = (c.download_until || "").slice(0, 10);
  const line = c.status === "closed"
    ? `Closed${c.closed_at ? " " + c.closed_at.slice(0, 10) : ""}. Your books and documents were exported first${day ? `; they are downloadable here until ${day}, when the AWS account closes` : ""}.`
    : c.status === "stopped"
    ? "Stopped by the operator. The instance is down; your books and documents were exported first, and the account and this screen stay."
    : "Closing. Your books and documents are being exported; the instance goes after that.";
  return html`
    ${appbar()}
    ${crumb("Your ∇ERPs", S.current?.label || "")}
    <h2 class="title">${S.current?.label || ""}</h2>
    <p class="sub" data-view="goneLine">${line}</p>
    ${c.status === "closing" || c.status === "close_requested" ? nothing : yourDataTpl(c, false)}`;
};

const gerpTpl = () => {
  const c = S.gerpCfg || {};
  if (!c.loading && GONE[c.status]) return goneGerpTpl(c);
  const chat = c.loading ? html`<div class="card soon"><div class="ic">💬</div><div class="body"><div class="name">Chat with your agent</div><div class="meta">loading…</div></div><div class="chev">→</div></div>`
    : c.chat_url ? html`
      <div class="card" data-view="chatCard"
           @click=${() => window.open(c.chat_url + chatHash(), "_blank")}>
        <div class="ic">💬</div>
        <div class="body">
          <div class="name">Chat with your agent</div>
          <div class="meta">ask about your books, run actions</div>
        </div>
        <div class="chev">→</div>
      </div>`
    : html`<div class="card soon"><div class="ic">💬</div><div class="body"><div class="name">Chat with your agent</div><div class="meta">${c.error ? "couldn't load — reopen the ∇ERP" : "available once provisioning completes"}</div></div><div class="chev">→</div></div>`;
  const copyEmail = async () => { try { await navigator.clipboard.writeText(c.agent_email); S.gerpCfg = { ...S.gerpCfg, copied: true }; update(); setTimeout(() => { if (S.gerpCfg) { S.gerpCfg.copied = false; update(); } }, 1500); } catch (_) {} };
  return html`
    ${appbar()}
    ${crumb("Your ∇ERPs", S.current?.label || "")}
    <h2 class="title">${S.current?.label || ""}</h2>
    <p class="sub">Owner controls for this instance.</p>
    <div class="cards">
      ${chat}
      ${c.agent_email ? html`
        <div class="card" title="Click to copy" @click=${copyEmail}>
          <div class="ic">✉️</div>
          <div class="body">
            <div class="name">Email your agent</div>
            <div class="meta"><span>${c.agent_email}</span> <em>(check your spam for replies)</em></div>
          </div>
          <div class="chev">${c.copied ? "✓" : "⧉"}</div>
        </div>` : nothing}
    </div>
    <div class="sec">Industry — history</div>
    ${measuredTpl(c.naics, "revenue", c.naics_window, "industry")}
    <div class="sec">Settings</div>
    <label class="setting ${c.loading || c.ooBusy ? "loading" : ""}">
      <input type="checkbox" data-view="ooToggle" .checked=${!!c.openly_operated}
             ?disabled=${c.loading || c.ooBusy || c.error}
             @change=${e => toggleOO(e.target.checked)} />
      <span class="s-spin"></span>
      <span class="s-body">
        <span class="s-name">Openly operated</span>
        <span class="s-desc">${c.openly_operated
          ? html`published at <a data-view="bizPageLink" href=${bizPageUrl(S.current?.gerp_id)} target="_blank" rel="noopener" @click=${e => e.stopPropagation()}>${bizPageUrl(S.current?.gerp_id).replace(/^https?:\/\//, "")}</a>`
          : "open up your business to invite help"}</span>
      </span>
    </label>
    ${c.agent_email ? html`<div class="setting"><span class="s-body"><span class="s-name">Agent email</span><span class="s-desc">${c.agent_email_verified ? "replies enabled" : "verify the link we emailed " + (c.verify_recipient || "you") + " to enable replies"}</span></span></div>` : nothing}
    <div class="setting">
      <span class="s-body">
        <span class="s-name">Notification email</span>
        <span class="s-desc">where your agent emails you when you ask it to${c.notification_email ? (c.notification_email_verified ? " — verified" : " — check " + c.notification_email + " for the verify link") : " — not set yet"}</span>
        <input type="email" placeholder="you@example.com" .value=${c.notification_email || ""}
               ?disabled=${c.loading || c.neBusy || c.error}
               @change=${e => saveNotificationEmail(e.target.value)} />
      </span>
    </div>
    <div class="setting">
      <span class="s-body">
        <span class="s-name">Timezone</span>
        <span class="s-desc">the clock your business runs on — decides which month a sale lands in and when a scheduled job fires</span>
        <input type="text" data-view="tzInput" placeholder="America/Los_Angeles" .value=${c.timezone || ""}
               ?disabled=${c.loading || c.tzBusy || c.error}
               @change=${e => saveTimezone(e.target.value)} />
      </span>
    </div>
    <div class="sec">Business info</div>
    <label for="gi-label">Business name</label><input id="gi-label" autocomplete="organization" />
    ${businessProfileTpl()}
    ${btn("saveGerpInfo", "Save", doSaveGerpInfo, "saveGerpInfoBtn")}
    <div class="sec">Billing</div>
    ${c.unpaid ? html`
      <div class="panel" data-view="unpaidPanel">
        <div class="field"><span class="k">Payment failed</span>
          <span class="v">${money(c.unpaid.total)} past due${c.unpaid.invoices?.length > 1 ? ` (${c.unpaid.invoices.length} invoices)` : ""}${c.unpaid.closes_on ? html` · this ∇ERP closes <b>${c.unpaid.closes_on}</b>` : nothing}</span></div>
      </div>
      <p class="sub" style="margin:.8rem 0 0">Pick the card below that should pay it, then pay. The emailed link works too.</p>
      <button data-view="payNowBtn" style="margin:.8rem 0 1.8rem" ?disabled=${c.payBusy} @click=${payNow}>${c.payBusy ? "Paying…" : "Pay now"}</button>` : nothing}
    <p class="sub">This instance is billed on its own monthly invoice. Pick the card that pays it —
      one saved on this ∇ERP, or one on your account.</p>
    ${c.cardGone ? html`<p class="sub" data-view="cardGone" style="margin-top:.6rem">The card this
      ∇ERP was billed to is no longer at your bank — it was replaced or removed there, not here.
      Pick another, or the next invoice will not be paid.</p>` : nothing}
    <div class="panel">
      ${(c.cards || []).length ? (c.cards || []).map(m => cardRowTpl(
          m, c.cardSel, id => selectCard(id, S.current.gerp_id),
          // an account card is removed from Info & Billing, where the guard can see every gerp
          // billed to it. Offering it here would be a delete with half the picture.
          // a card on the ACCOUNT's customer is removed from Info & Billing, where the guard sees
          // every gerp billed to it. Offering it here would be a delete with half the picture.
          m.mine ? id => deleteCard(id, S.current.gerp_id) : null,
          (c.cards || []).filter(x => x.mine).length === 1))
        : html`<div class="field">
            <span class="k">Payment method</span>
            <span class="v muted" data-view="gerpCard">${c.loading ? "…" : "none on file"}</span>
          </div>`}
    </div>
    <button class="ghost" data-view="addGerpCardBtn" ?disabled=${c.loading}
            @click=${() => addGerpCard()}>${
      "Add card"}</button>

    ${yourDataTpl(c)}

    <div class="sec">Closing</div>
    <p class="sub">Shut this business down. You get your records first.</p>
    <button class="ghost danger-text" data-view="closeGerpBtn" @click=${openClose}>Close this ∇ERP</button>

    <div class="sec">Instructions</div>
    <p class="sub">How your agent works at this business. One line each — it reads all of them before every reply.</p>
    ${(c.expanded ? c.instructions : (c.instructions || []).slice(0, INSTR_INLINE)).map(i => instrRow(i, c.inBusy))}
    ${(c.instructions || []).length > INSTR_INLINE ? html`
      <button class="more" data-view="instrMore" @click=${() => { S.gerpCfg = { ...S.gerpCfg, expanded: !c.expanded }; update(); }}>
        ${c.expanded ? "show less" : `show ${c.instructions.length - INSTR_INLINE} more`}</button>` : nothing}
    <div class="setting"><span class="s-body"><input type="text" data-view="instrInput" placeholder="add an instruction — e.g. flag any invoice over $500 before paying it" maxlength="300" ?disabled=${c.loading || c.inBusy || c.error}
      @keydown=${e => { if (e.key === "Enter") { e.preventDefault(); saveInstruction(e.target); } }} @blur=${e => saveInstruction(e.target)} /></span></div>`;
};

// shown while the OAuth code exchange + first gerps load run — a dedicated
// interstitial so the login page never flashes back between Cognito and home.
const loadingTpl = () => html`
  <div class="loading">${bigLogo}<div class="loadspin"></div>
    <p class="loadmsg" data-view="loadMsg">${S.loadMsg || "Signing you in…"}</p></div>`;

const VIEWS = { landingScreen: loginTpl, loadingScreen: loadingTpl, signupScreen: signupTpl, confirmScreen: confirmTpl, homeScreen: homeTpl, publicProfileScreen: publicuserTpl, accountInfoAndBillingScreen: infobillingTpl, createGerpScreen: creategerpTpl, gerpScreen: gerpTpl };
const closeDialogTpl = () => {
  const c = S.gerpCfg?.closing;
  if (!c) return nothing;
  const canSubmit = c.typed.trim() === CLOSE_PHRASE && !c.busy;
  return html`
    <div class="modal" data-view="closeDialog" @click=${dismissClose}>
      <div class="modal-inner" @click=${(e) => e.stopPropagation()}>
        <h3>Close ${S.current?.label || "this ∇ERP"}?</h3>
        <p>Your books and documents are exported to you first, and stay downloadable from this
          screen for 15 days. The instance itself — its agent, its books, everything running — is
          destroyed now and cannot be brought back. After the 15 days the export is deleted too.</p>
        <p class="sub">Keeping that export, and keeping it somewhere that satisfies whatever rule
          applies to you, is yours to do.</p>
        <label for="close-confirm">Type <b>${CLOSE_PHRASE}</b> to confirm</label>
        <input id="close-confirm" data-view="closeConfirmInput" .value=${c.typed}
               ?disabled=${c.busy} autocomplete="off"
               @input=${(e) => { S.gerpCfg = { ...S.gerpCfg, closing: { ...c, typed: e.target.value } }; update(); }} />
        <div class="modal-actions">
          <button class="ghost" data-view="closeCancelBtn" ?disabled=${c.busy}
                  @click=${dismissClose}>Cancel</button>
          <button class="danger" data-view="closeSubmitBtn" ?disabled=${!canSubmit}
                  @click=${confirmClose}>${c.busy ? "Closing…" : "Close this ∇ERP"}</button>
        </div>
      </div>
    </div>`;
};

const deleteDialogTpl = () => {
  const d = S.deleting;
  if (!d) return nothing;
  const canSubmit = d.typed.trim() === DELETE_PHRASE && !d.busy && !d.live.length;
  return html`
    <div class="modal" data-view="deleteDialog" @click=${dismissDelete}>
      <div class="modal-inner" @click=${(e) => e.stopPropagation()}>
        <h3>Delete your account?</h3>
        <p>Your login, your private record, your public profile and your saved card are deleted,
          and the seller's record of you is reduced to a shell. Invoices already issued to your
          ∇ERPs stay on the seller's books, as tax law requires.</p>
        <p class="sub">What the platform keeps: that an account with this email, phone or card
          closed, when, and whether it left a balance unpaid. Nothing that names you.</p>
        ${d.live.length ? html`
          <p class="err" data-view="deleteBlocked">Still running — close each one first:
            ${d.live.map(g => html`<b>${g.label}</b> `)}</p>` : nothing}
        <label for="delete-confirm">Type <b>${DELETE_PHRASE}</b> to confirm</label>
        <input id="delete-confirm" data-view="deleteConfirmInput" .value=${d.typed}
               ?disabled=${d.busy} autocomplete="off"
               @input=${(e) => set({ deleting: { ...d, typed: e.target.value } })} />
        <div class="modal-actions">
          <button class="ghost" data-view="deleteCancelBtn" ?disabled=${d.busy}
                  @click=${dismissDelete}>Cancel</button>
          <button class="danger" data-view="deleteSubmitBtn" ?disabled=${!canSubmit}
                  @click=${confirmDelete}>${d.busy ? "Deleting…" : "Delete my account"}</button>
        </div>
      </div>
    </div>`;
};

const lightboxTpl = () => S.lightbox ? html`
  <div class="lightbox" @click=${closeDemo}>
    <div class="lightbox-inner" @click=${(e) => e.stopPropagation()}>
      <button class="lightbox-close" @click=${closeDemo} aria-label="Close">×</button>
      <div class="lightbox-cap">${S.lightbox.mod} · <span class="role">${S.lightbox.role}</span> — <span class="p">${S.lightbox.prompt}</span></div>
      ${S.lightboxLoading ? html`<div class="lightbox-wait"><div class="lightbox-spin"></div></div>` : ""}
      ${S.lightboxGif
        ? html`<img src=${demoFull(S.lightbox)} alt="${S.lightbox.mod} demo"
                     style=${S.lightboxLoading ? "visibility:hidden;position:absolute" : ""}
                     @load=${() => set({ lightboxLoading: false })}
                     @error=${(e) => { if (!e.target.dataset.fb) { e.target.dataset.fb = 1; e.target.src = demoGif(S.lightbox); } }} />`
        : html`<video src=${demoVid(S.lightbox)} autoplay loop muted playsinline style=${S.lightboxLoading ? "visibility:hidden;position:absolute" : ""} @canplay=${() => set({ lightboxLoading: false })} @error=${() => set({ lightboxGif: true, lightboxLoading: true })}></video>`}
    </div>
  </div>` : nothing;
const appView = () => html`<div class="wrap" data-view=${S.view}>${(VIEWS[S.view] || loginTpl)()}${S.status.msg ? html`<div id="status" class=${S.status.cls}>${S.status.msg}</div>` : nothing}</div>${lightboxTpl()}${closeDialogTpl()}${deleteDialogTpl()}`;

// close the autocomplete / link menus on outside click
document.addEventListener("click", (e) => {
  for (const form of ["pu", "acct", "biz", "pub"]) {
    if (S[form]?.acOpen && !e.target.closest(".ac-wrap")) acClose(form);
    if (S[form]?.linkMenuOpen && !e.target.closest(".linkwrap")) set({ [form]: { ...S[form], linkMenuOpen: false } });
  }
});

// ---------- boot ----------
(async function () {
  root = document.getElementById("root");
  root.replaceChildren(); // index.html pre-renders the landing for no-JS readers; lit-html appends rather than replaces, so clear it
  const code = new URLSearchParams(location.search).get("code");
  if (code) {
    set({ view: "loadingScreen", status: { msg: "", cls: "" } }); // dedicated interstitial — not the login page
    try { storeTokens(await exchange(code)); }
    catch (e) { status("Sign-in failed — please try again. (" + String(e) + ")", "err"); }
    history.replaceState({}, "", "/");
  }
  // Back from a vendor's consent (modules/mcp): Identity returns the owner to /mcp/callback with
  // the session it opened. Kept across the sign-in when there is none yet, then completed against
  // the gerp that was waiting on it. Read here so the Stripe branch below never sees it.
  const consent = location.pathname === "/mcp/callback"
    ? new URLSearchParams(location.search).get("session_id")
    : sessionStorage.getItem("mcp_consent");
  if (consent && !idToken()) { sessionStorage.setItem("mcp_consent", consent); await login(); return; }
  if (consent) {
    sessionStorage.removeItem("mcp_consent");
    history.replaceState({}, "", "/");
    set({ view: "loadingScreen", status: { msg: "", cls: "" }, loadMsg: "Finishing the connection…" });
    const arrival = await finishVendorConsent(consent);
    try { await loadGerps(); } catch (e) { console.error("[load ∇ERPs]", e); }
    // land on the gerp that was waiting on it, named: an account with several gerps reads
    // "linear connected on gradienterp", not a line that could be any of them
    const waited = arrival.gerp_id && (S.gerps || []).find(g => g.gerp_id === arrival.gerp_id);
    if (waited) { enterGerp(waited.gerp_id, waited.label || waited.gerp_id); set({ status: { msg: arrival.msg, cls: arrival.cls } }); return; }
    set({ view: "homeScreen", status: { msg: arrival.msg, cls: arrival.cls } });
    return;
  }
  if (idToken()) {
    stashDeepLink();
    // returning from Stripe: ?session_id=… is appended to the return url we created. Read BEFORE
    // the interstitial goes up, because it decides what the interstitial says — a buyer coming
    // back from a card page was not signing in, and the wait here is the card being stored.
    const q = new URLSearchParams(location.search);
    // the card page returns `setup_intent` instead (an Indian business: the e-mandate's SetupIntent)
    const sessionId = q.get("session_id") || q.get("setup_intent");
    if (sessionId && q.get("popup") === "1") { await finishInPopup(sessionId, q.get("gerp") || ""); return; }
    set({ view: "loadingScreen", status: { msg: "", cls: "" },   // no flash of empty home
          loadMsg: sessionId ? "Saving your card…" : "" });
    // Held rather than shown, because the `set` below lands on the home screen and would wipe a
    // status set here — which is how a failed card save used to arrive as a silent no-op: the buyer
    // came back from Stripe, the save failed, and the only sign was a gerp still waiting on payment.
    let arrival = { msg: "", cls: "" };
    if (sessionId) {
      const gerp = q.get("gerp");
      if (gerp) S.pending[gerp] = gerp;    // greyed card while the sub-account is vended
      history.replaceState({}, "", "/");   // a refresh must not re-submit the session
      try {
        const out = await finishCardSetup(sessionId);
        // Two subjects come back through one return url. The account default has no gerp to vend,
        // so its only outcome is the card itself — say so, or a successful save looks like nothing
        // happened at all.
        // Both subjects get told. A gerp being VENDED shows its own greyed card, but a card added
        // to a gerp that already exists changes nothing visible from here.
        arrival = { cls: "ok", msg: "Card saved." };
      } catch (e) {
        console.error("[save card]", e);
        // No charge happens here — this is a setup intent, so the honest sentence says so. Only a
        // refusal the server actually returned (`definite`) is reported as one; anything else is
        // not knowing, and telling a buyer their card failed when it saved is the worse error.
        const definite = !!(e && e.definite);
        arrival = q.get("billing") === "account"
          ? definite
            ? { cls: "err", msg: "We couldn't save your card. Nothing has been charged — try again "
                + "from Info & Billing." }
            : { cls: "err", msg: "We lost track of that — nothing has been charged. Check Info & "
                + "Billing: if your card is listed it saved, and if not, try again." }
          : definite
            ? { cls: "err", msg: "We couldn't save your card. Nothing has been charged — your ∇ERP "
                + "is saved below and you can finish paying for it from there." }
            : { cls: "err", msg: "We lost track of that — nothing has been charged. Open the ∇ERP "
                + "below and check its Billing section: if your card is listed it saved." };
      }
    }
    try { await loadGerps(); }
    catch (e) {
      console.error("[load ∇ERPs]", e);
      if (!arrival.msg) arrival = { cls: "err", msg: "Couldn't load your ∇ERPs — reload to try again." };
    }
    if (!sessionId && await followDeepLink()) {
      history.replaceState({}, "", "/");
    } else if (!sessionId && await restore()) {
      // the screen the reload happened on
    } else if (sessionId && q.get("billing") === "account") {
      // a card added from Info & Billing returns to Info & Billing, with the outcome on it
      await openInfo();
      status(arrival.msg, arrival.cls);
    } else {
      set({ view: "homeScreen", status: arrival });
    }
  } else if (new URLSearchParams(location.search).get("gerp") && new URLSearchParams(location.search).get("open")) {
    // a link into the console with no session: straight to sign-in, the link kept for after
    await login();
  } else {
    set({ view: "landingScreen" });
  }
})();
