/* The popup is a readout, not a control panel. The handoff runs by itself in the
 * background; this exists to say what is waiting and why, and to give a way in
 * if the owner wants one. */

const API = "http://127.0.0.1:8787";
const $ = (id) => document.getElementById(id);

const WHY_ICON = {
  "a security check the pipeline must not solve": "🔒",
  "the portal wants a sign-in": "👤",
};

async function currentTab() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  return t;
}

async function render() {
  let stats, queue;
  try {
    stats = await (await fetch(`${API}/stats`, { cache: "no-store" })).json();
    queue = await (await fetch(`${API}/extension/queue`, { cache: "no-store" })).json();
  } catch {
    $("server").textContent = "AppliedIn is not running";
    $("server").className = "sub down";
    $("count").textContent = "—";
    $("cap").textContent = "start it with ./appliedin start";
    return;
  }

  $("server").textContent = `connected · ${queue.mode} mode`;
  $("server").className = "sub up";

  const jobs = queue.jobs || [];
  $("count").textContent = String(jobs.length);
  $("cap").textContent = jobs.length === 0
    ? "nothing needs you — the pipeline has it"
    : jobs.length === 1 ? "application is open and filled, waiting on you"
                        : "applications are open and filled, waiting on you";

  const list = $("list");
  list.innerHTML = "";
  for (const j of jobs.slice(0, 6)) {
    const row = document.createElement("button");
    row.className = "row";
    row.innerHTML =
      `<span class="ic">${WHY_ICON[j.why] || "✓"}</span>` +
      `<span class="txt"><b>${j.company}</b><i>${j.title}</i>` +
      `<em>${j.why}</em></span>`;
    row.onclick = () => chrome.tabs.create({ url: j.url, active: true });
    list.append(row);
  }
  if (jobs.length > 6) {
    const more = document.createElement("div");
    more.className = "more";
    more.textContent = `+${jobs.length - 6} more`;
    list.append(more);
  }

  $("ft").textContent = jobs.length
    ? "Each one is already filled. Clear the security check and press the site's Submit — it marks itself applied."
    : `${stats.counts_by_status?.tailored ?? 0} tailored · ${stats.counts_by_status?.applied ?? 0} applied`;

  // Offer a manual fill only when the owner is actually looking at a form.
  const t = await currentTab();
  try {
    const probe = await chrome.tabs.sendMessage(t.id, { type: "APPLIEDIN_PROBE" });
    $("here").hidden = !(probe && probe.score >= 25);
  } catch { $("here").hidden = true; }
}

$("fill").addEventListener("click", async () => {
  const t = await currentTab();
  $("fill").textContent = "filling…";
  try {
    const r = await chrome.tabs.sendMessage(t.id, { type: "APPLIEDIN_FILL" });
    $("fill").textContent = r?.ok ? `filled ${r.filled} fields` : (r?.error || "could not fill");
  } catch { $("fill").textContent = "no form on this page"; }
});

$("enabled").addEventListener("change", async (e) => {
  await chrome.storage.local.set({ enabled: e.target.checked });
  if (e.target.checked) chrome.runtime.sendMessage({ type: "APPLIEDIN_SYNC" });
});

chrome.storage.local.get("enabled").then((s) => { $("enabled").checked = s.enabled !== false; });
render();
