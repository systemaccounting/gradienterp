// the card page's script (card.html): a file, so the page's CSP allows no inline script. The
// publishable key arrives in the page's `stripe-key` meta tag, filled in by the BFF.
const say = (msg, cls) => { const s = document.getElementById("status"); s.textContent = msg; s.className = "status " + (cls || ""); };
const frag = new URLSearchParams(location.hash.slice(1));
const clientSecret = frag.get("cs") || "";
// the return is ours or nothing: a url from another origin would make this page a redirector
let returnUrl = "";
try { const u = new URL(frag.get("return") || "", location.origin); if (u.origin === location.origin) returnUrl = u.href; } catch (_) {}
history.replaceState(null, "", location.pathname);   // the secret leaves the address bar
const key = document.querySelector('meta[name="stripe-key"]')?.content || "";
if (!clientSecret || !returnUrl || !key || !window.Stripe) {
  say("This card link is incomplete. Go back and start the card step again.", "err");
} else {
  const stripe = Stripe(key);
  const elements = stripe.elements({ clientSecret, appearance: { theme: "night" } });
  const el = elements.create("payment");
  el.mount("#element");
  el.on("ready", () => { document.getElementById("save").disabled = false; });
  document.getElementById("f").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("save");
    btn.disabled = true; say("saving…");
    const { error } = await stripe.confirmSetup({ elements, confirmParams: { return_url: returnUrl } });
    // confirmSetup leaves the page on success; an error is the only thing that comes back
    if (error) { say(error.message || "The card was not saved.", "err"); btn.disabled = false; }
  });
}
