/* The popup is a remote control. It asks the content script what it can see,
 * tells it to fill, and reports back — every decision is the server's. */

const API = "http://127.0.0.1:8787";
const $ = (id) => document.getElementById(id);

let ctx = null;

async function tab() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  return t;
}

function tag(text, ok) {
  const s = document.createElement("span");
  s.className = "tag" + (ok ? " ok" : "");
  s.textContent = text;
  return s;
}

async function init() {
  // Is the app running? Without it there is no profile, no résumé, no mapping.
  try {
    const r = await fetch(`${API}/stats`, { cache: "no-store" });
    const d = await r.json();
    $("server").textContent = `connected · ${d.counts_by_status?.tailored ?? 0} tailored`;
    $("server").className = "sub up";
  } catch {
    $("server").textContent = "AppliedIn is not running";
    $("server").className = "sub down";
    $("ft").textContent = "Start it with ./appliedin start, then reopen this.";
    return;
  }

  const t = await tab();
  try {
    ctx = await (await fetch(
      `${API}/extension/context?url=${encodeURIComponent(t.url)}`)).json();
  } catch { ctx = null; }

  if (ctx && (ctx.company || ctx.title)) {
    $("job").hidden = false;
    $("co").textContent = ctx.company || "This posting";
    $("role").textContent = ctx.title || t.title || "";
    const tags = $("tags");
    if (ctx.resume_url) tags.append(tag("tailored résumé", true));
    if (ctx.known_site) tags.append(tag("site rules known", true));
    if (ctx.status) tags.append(tag(ctx.status));
  }
  $("fill").disabled = false;
}

$("fill").addEventListener("click", async () => {
  const btn = $("fill");
  btn.disabled = true;
  btn.classList.add("busy");
  btn.textContent = "reading the form…";
  const t = await tab();

  let res;
  try {
    // all_frames: the form often lives in an embedded iframe, and only that
    // frame's copy of the script can see it. Whichever replies with a result wins.
    const replies = await chrome.tabs.sendMessage(t.id, { type: "APPLIEDIN_FILL" });
    res = replies;
  } catch (e) {
    res = { ok: false, error: "no form found on this page (reload it and retry)" };
  }

  btn.classList.remove("busy");
  btn.textContent = "Fill this application";
  btn.disabled = false;

  const out = $("out");
  out.hidden = false;
  out.innerHTML = "";
  const line = (n, text, warn) => {
    const d = document.createElement("div");
    d.className = "row" + (warn ? " warn" : "");
    d.innerHTML = `<b>${n}</b><span>${text}</span>`;
    out.append(d);
  };

  if (!res || !res.ok) {
    line("—", res?.error || "could not fill this page", true);
    return;
  }
  line(res.filled, "fields filled from your profile");
  if (res.resume) line("1", "tailored résumé attached");
  if (res.essays?.length) line(res.essays.length, "open questions drafted");
  if (res.sanctions) line("✓", `sanctions question — ${res.sanctions}`);
  if (res.remaining?.length) {
    line(res.remaining.length, "still need you:", true);
    const ul = document.createElement("ul");
    ul.className = "list";
    for (const r of res.remaining.slice(0, 6)) {
      const li = document.createElement("li");
      li.textContent = r;
      ul.append(li);
    }
    out.append(ul);
  }
  ctx = { ...(ctx || {}), pk: res.pk || ctx?.pk };
  $("done").hidden = false;
});

$("applied").addEventListener("click", async () => {
  if (!ctx?.pk) {
    $("ft").textContent = "This posting isn't on your board, so there's nothing to mark.";
    return;
  }
  try {
    const r = await (await fetch(`${API}/extension/applied`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pk: ctx.pk, confirmation: "submitted by hand via the extension" }),
    })).json();
    $("applied").textContent = r.ok ? "✓ marked applied on your board" : (r.error || "could not mark it");
    $("applied").disabled = !!r.ok;
  } catch {
    $("applied").textContent = "could not reach AppliedIn";
  }
});

init();
