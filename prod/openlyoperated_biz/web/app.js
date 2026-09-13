// openlyoperated.biz — shared lit-html shell for the dashboard.
//
// Both entry pages import from here:
//   index.html      → empty/annotated regions (deployable scaffold)
//   index.mock.html → seeded regions (demo)
//
// app.js owns everything they share — helpers, app state, the control handlers, the shell chrome
// (masthead / persona bar / bottom tab bar / footer), and mount(). Each page passes its own region
// templates (ticker, kpi, opps, left, center, right, drawer) into mount(); adding a new view is a
// new template function, not a new copy of the shell. No build step — vendored lit-html, ESM only.

import { html, render, nothing } from './vendor/lit-html.js';
export { html, nothing };

// ---------- helpers ----------
export const pad = n => String(n).padStart(2, '0');
export const fmtClock = d => pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
export const hash = s => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; };
export const rngFor = seed => { let x = seed >>> 0; return () => { x = (Math.imul(x, 1664525) + 1013904223) >>> 0; return x / 4294967296; }; };
export const fmtMoney = n => { const a = Math.abs(n); if (a >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M'; if (a >= 1e4) return '$' + Math.round(n / 1e3) + 'k'; if (a >= 1e3) return '$' + (n / 1e3).toFixed(1) + 'k'; return '$' + Math.round(n); };
export const fmtPlain = n => '$' + Math.round(n).toLocaleString('en-US');
export const pick = a => a[Math.floor(Math.random() * a.length)];
export const C = { UP: '#3a7d52', DOWN: '#b04a3f', MUT: '#7d7b71', ORANGE: '#df722a', GOLD: '#f4b73c' };

// sparkline / area chart as lit svg templates (no string concat)
export function spark(series, color) {
  const w = 84, h = 24, p = 2, min = Math.min(...series), max = Math.max(...series), rng = (max - min) || 1;
  const pts = series.map((v, i) => [p + i * (w - 2 * p) / (series.length - 1), h - p - (v - min) / rng * (h - 2 * p)]);
  const d = 'M' + pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' L'), last = pts[pts.length - 1];
  const area = d + ` L${(w - p).toFixed(1)},${h - p} L${p},${h - p} Z`;
  return html`<svg width=${w} height=${h} viewBox="0 0 ${w} ${h}" style="display:block;flex:none;">
    <path d=${area} fill=${color} opacity="0.1"></path>
    <path d=${d} fill="none" stroke=${color} stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"></path>
    <circle cx=${last[0].toFixed(1)} cy=${last[1].toFixed(1)} r="1.9" fill=${color}></circle></svg>`;
}
let gidc = 0;
export function bigChart(series, color) {
  const w = 476, h = 116, p = 6, min = Math.min(...series), max = Math.max(...series), rng = (max - min) || 1;
  const pts = series.map((v, i) => [p + i * (w - 2 * p) / (series.length - 1), h - p - (v - min) / rng * (h - 2 * p)]);
  const d = 'M' + pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' L'), last = pts[pts.length - 1];
  const area = d + ` L${(w - p).toFixed(1)},${h - p} L${p},${h - p} Z`, gid = 'oog' + (gidc++);
  return html`<svg width="100%" height=${h} viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="display:block;">
    <defs><linearGradient id=${gid} x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color=${C.ORANGE} stop-opacity="0.22"></stop>
      <stop offset="100%" stop-color=${C.GOLD} stop-opacity="0.02"></stop></linearGradient></defs>
    <path d=${area} fill=${'url(#' + gid + ')'}></path>
    <path d=${d} fill="none" stroke=${color} stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></path>
    <circle cx=${last[0].toFixed(1)} cy=${last[1].toFixed(1)} r="3" fill=${color}></circle></svg>`;
}

export const PERSONAS = ['engineer', 'investor', 'consumer', 'owner', 'regulator'];
export const LENS = {
  engineer: 'find where to ship labor — fees, energy, migrations',
  investor: 'find where to ship capital — margins & retained-earnings slopes',
  consumer: 'find the most competitive prices nearby',
  owner: 'benchmark against peers · adopt crowdsourced wins',
  regulator: 'verify published = recomputed across the ledger'
};
export const SEC_ORDER = ['cafe', 'bakery', 'restaurant', 'roaster', 'retail', 'bar'];
export const SORT_LABELS = { rev: 'revenue', margin: 'margin', trend: 'retained-earnings slope', name: 'name' };

// ---------- state + the single update() ----------
export const state = {
  persona: 'investor', sector: 'all', query: '', sortKey: 'rev', sortDir: -1,
  tab: 'opps', selected: null, clock: fmtClock(new Date()), eventsToday: 48213, eventsPerMin: 47, events: []
};
let _render = () => {};
export const update = () => _render();          // every mutation → one re-render; lit diffs the DOM
const set = patch => { Object.assign(state, patch); update(); };
export const on = {
  persona: p => set({ persona: p }),
  sector: k => set({ sector: k }),
  query: q => set({ query: q }),
  tab: t => set({ tab: t }),
  sort: k => set(state.sortKey === k ? { sortDir: -state.sortDir } : { sortKey: k, sortDir: k === 'name' ? 1 : -1 }),
  open: id => set({ selected: id }),
  close: () => set({ selected: null })
};

// ---------- shared chrome ----------
const nabla = html`<svg width="38" height="42" viewBox="126 0 1031 1152" style="display:block;flex:none;"><defs><linearGradient id="oo-nabla" x1="0" y1="0" x2="0.45" y2="1"><stop offset="0%" stop-color="#f4b73c"></stop><stop offset="100%" stop-color="#df722a"></stop></linearGradient></defs><path d="M616 1152 126 0H1157ZM665 860 1038 77H331Z" fill="url(#oo-nabla)"></path></svg>`;

const masthead = (opts) => html`<header class="oo-masthead"><div class="oo-band">
  <div style="display:flex;align-items:center;gap:0;">${nabla}
    <div style="display:flex;flex-direction:column;line-height:1.05;margin-left:-2px;">
      <div style="font-size:18px;font-weight:600;letter-spacing:-.02em;"><span>openlyoperated</span><span style="background:linear-gradient(95deg,#f4b73c,#df722a);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;display:inline-block;">.biz</span></div>
      <div class="oo-desktoponly mono" style="font-size:10px;letter-spacing:.03em;color:#8a887d;margin-top:3px;">the public record of openly-operated business</div>
    </div>
  </div>
  <div style="flex:1;"></div>
  ${opts.mockLink ? html`<a class="mono" href="index.mock.html" title="view the site populated with mock data" style="font-size:12px;color:#df722a;text-decoration:none;border:1px solid #df722a;padding:7px 12px;border-radius:9px;white-space:nowrap;margin-right:28px;">mock site ↗</a>` : nothing}
  <div class="oo-desktoponly" style="position:relative;width:280px;max-width:34vw;">
    <span class="mono" style="position:absolute;left:11px;top:50%;transform:translateY(-50%);color:#a8a698;font-size:13px;">⌕</span>
    <input class="mono" placeholder="search businesses · accounts · events" .value=${state.query} @input=${e => on.query(e.target.value)}
      style="width:100%;padding:8px 12px 8px 28px;background:#fffefb;border:1px solid #d4d1c5;border-radius:9px;font-size:12.5px;color:#1a1a17;outline:none;" />
  </div>
  <div class="mono" style="display:flex;align-items:center;gap:8px;font-size:12px;color:#6d6b61;">
    <span class="dot" style="box-shadow:0 0 0 3px #df722a22;"></span>
    <span style="color:#1a1a17;font-weight:500;letter-spacing:.02em;">${state.clock}</span>
    <span class="oo-desktoponly" style="color:#c4c1b4;">·</span>
    <span class="oo-desktoponly ${opts.hasMin ? '' : 'oo-ph'}">${opts.hasMin ? state.eventsPerMin + '/min' : '—/min'}</span>
  </div>
  <a class="oo-desktoponly mono" href="#" style="font-size:12px;color:#1a1a17;text-decoration:none;border:1px solid #d4d1c5;padding:7px 12px;border-radius:9px;white-space:nowrap;">api ↗</a>
</div></header>`;

const persona = () => html`<div class="oo-persona"><div class="oo-band">
  <span class="mono" style="font-size:10.5px;text-transform:uppercase;letter-spacing:.13em;color:#9a988c;flex:none;">reading as</span>
  <div style="display:flex;gap:7px;">${PERSONAS.map(p => { const a = state.persona === p; return html`<button class="oo-chip" @click=${() => on.persona(p)}
    style="padding:5px 13px;border-radius:999px;border:1px solid ${a ? '#1a1a17' : '#d4d1c5'};background:${a ? '#1a1a17' : 'transparent'};color:${a ? '#f6f5f1' : '#6d6b61'};font:inherit;font-size:12.5px;font-weight:500;cursor:pointer;transition:all .12s;letter-spacing:.01em;white-space:nowrap;">${p}</button>`; })}</div>
  <div class="oo-desktoponly" style="flex:1;"></div>
  <span class="oo-desktoponly mono" style="font-size:12px;color:#7d7b71;text-align:right;">${LENS[state.persona]}</span>
</div></div>`;

const tabbar = () => { const t = (id, label, svg) => html`<button class="oo-tabbtn ${state.tab === id ? 'active' : ''}" @click=${() => on.tab(id)}>${svg}${label}</button>`;
  return html`<nav class="oo-tabbar">
    ${t('opps', 'opportunities', html`<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M4 5h16l-8 14z"/></svg>`)}
    ${t('biz', 'businesses', html`<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/></svg>`)}
    ${t('stream', 'stream', html`<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12h5l2-6 4 12 2-6h7"/></svg>`)}
  </nav>`; };

const footer = () => html`<footer class="oo-footer"><div class="oo-band"><span class="mono" style="font-size:11px;color:#8a887d;line-height:1.6;">openlyoperated.biz · a public, spec-compliant surface over api.openlyoperated.biz — every published number is recomputable from the published event stream. private businesses don't appear here.</span></div></footer>`;

// sector filter list — shared (counts injected: a map, or null to show "—")
export const sectorsTpl = (counts) => {
  const rows = [{ k: 'all', l: 'all sectors', c: counts ? counts.all : '—' }, ...SEC_ORDER.map(k => ({ k, l: k, c: counts ? (counts[k] || 0) : '—' }))];
  return html`<div class="oo-sectors">${rows.map(r => { const a = state.sector === r.k; return html`<button @click=${() => on.sector(r.k)}
    style="display:flex;justify-content:space-between;align-items:center;gap:8px;width:100%;padding:7px 10px;border-radius:8px;border:1px solid ${a ? '#d4d1c5' : 'transparent'};background:${a ? '#fffefb' : 'transparent'};color:${a ? '#1a1a17' : '#6d6b61'};font:inherit;font-size:12.5px;font-weight:${a ? 600 : 500};cursor:pointer;text-align:left;"><span>${r.l}</span><span class="mono" style="color:#${counts ? 'a8a698' : 'c4c1b4'};">${r.c}</span></button>`; })}</div>`;
};

// ---------- mount ----------
// regions: { ticker, kpi, opps, left, center, right, drawer? } — each a () => lit template.
// opts: { root, banner?, hasMin?, tick? }   tick() runs every 1100ms (live data); the clock always ticks.
export function mount({ root, regions, banner = false, hasMin = false, mockLink = false, tick = null }) {
  const appView = () => html`<div class="oo-app">
    ${banner ? bannerTpl() : nothing}
    ${masthead({ hasMin, mockLink })}
    ${regions.ticker()}
    ${persona()}
    <div id="oo-content" class="tab-${state.tab}">
      ${regions.kpi()}
      ${regions.opps()}
      <div class="oo-band oo-grid">${regions.left()}${regions.center()}${regions.right()}</div>
      ${footer()}
    </div>
    ${tabbar()}
    <div id="oo-drawer">${regions.drawer ? regions.drawer() : nothing}</div>
  </div>`;
  _render = () => render(appView(), root);
  update();
  setInterval(() => { state.clock = fmtClock(new Date()); update(); }, 1000);
  if (tick) setInterval(() => { tick(); update(); }, 1100);
}

const bannerTpl = () => html`<div class="oo-banner"><span style="width:7px;height:7px;border-radius:50%;background:#fff;flex:none;"></span>site loaded with mock data for demonstration<a href="index.html">view live site →</a></div>`;
