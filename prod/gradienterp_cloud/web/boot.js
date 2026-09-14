// authed tabs and the oauth callback skip the pre-rendered landing (no login flash): cleared at parse
// time, before the module fetches; app.js re-clears idempotently on boot. A file, not an inline
// script, so the page's CSP allows no inline script at all.
if (sessionStorage.getItem("id_token") || location.search.includes("code=")) document.getElementById("root").replaceChildren();

// a demo thumb that fails to load goes, the way app.js's own render drops one (`@error`): errors that
// already happened before this ran, and any after
const dropThumb = (img) => { if (img.closest(".thumb")) img.remove(); };
document.querySelectorAll(".thumb img").forEach((img) => { if (img.complete && img.naturalWidth === 0) dropThumb(img); });
document.addEventListener("error", (e) => { if (e.target instanceof HTMLImageElement) dropThumb(e.target); }, true);
