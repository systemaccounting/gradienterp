// the support page's form (support.html): a file, so the page's CSP allows no inline script
const f = document.getElementById("f"), st = document.getElementById("status"), btn = document.getElementById("send");
const say = (msg, cls) => { st.textContent = msg; st.className = "status " + (cls || ""); };
f.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { email: f.email.value.trim(), subject: f.subject.value.trim(), message: f.message.value.trim(), website: f.website.value };
  if (!body.email || !body.subject || !body.message) { say("email, subject and message are all needed", "err"); return; }
  btn.disabled = true; say("sending…");
  try {
    const r = await fetch("/api/support", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const out = await r.json().catch(() => ({}));
    if (r.ok) { say("Sent. Replies go to " + body.email + ".", "ok"); f.subject.value = ""; f.message.value = ""; }
    else say(out.error || ("not sent (" + r.status + ")"), "err");
  } catch (_) { say("not sent, try again", "err"); }
  btn.disabled = false;
});
