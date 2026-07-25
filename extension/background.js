/* AppliedIn — the handoff.
 *
 * The pipeline applies on its own until it hits something it must not do: a
 * security check, a sign-in wall. Those are the only steps that genuinely need a
 * person, and everything around them should not.
 *
 * So this watches the server's handoff queue and does the rest of the work
 * itself — it opens each waiting application in a background tab, where the
 * content script fills it from the profile. The owner is not asked to find the
 * job, open it, or press fill. The badge counts what is genuinely waiting on
 * them; when they get to it, the form is already complete and the only thing
 * left is the check the pipeline is not allowed to touch, and Submit.
 */

const API = "http://127.0.0.1:8787";
const POLL_SECONDS = 45;
// Open a few at a time. A queue of thirty must not become thirty tabs.
const MAX_OPEN = 3;

async function state() {
  const s = await chrome.storage.local.get(["opened", "enabled"]);
  return { opened: s.opened || {}, enabled: s.enabled !== false };
}

async function setBadge(n) {
  await chrome.action.setBadgeText({ text: n ? String(n) : "" });
  await chrome.action.setBadgeBackgroundColor({ color: "#f2b13e" });
}

async function openHandoffs() {
  const { opened, enabled } = await state();
  if (!enabled) return;

  let queue;
  try {
    queue = await (await fetch(`${API}/extension/queue`, { cache: "no-store" })).json();
  } catch {
    await setBadge(0);                       // the app is not running
    return;
  }
  const jobs = queue.jobs || [];
  await setBadge(jobs.length);
  if (!jobs.length) return;

  // Only open what is not already open. A tab the owner closed without
  // finishing stays out of the way until the server drops it from the queue.
  const tabs = await chrome.tabs.query({});
  const openUrls = new Set(tabs.map((t) => (t.url || "").split("?")[0]));
  const fresh = jobs.filter((j) => !opened[j.pk] && !openUrls.has((j.url || "").split("?")[0]));

  for (const job of fresh.slice(0, MAX_OPEN)) {
    try {
      // Background tab: the filling happens without stealing focus, so the
      // owner meets a finished form rather than watching one being typed.
      await chrome.tabs.create({ url: job.url, active: false });
      opened[job.pk] = Date.now();
    } catch { /* a bad URL must not stop the rest */ }
  }
  await chrome.storage.local.set({ opened });
}

/* A job that left the queue (submitted, or skipped) should be openable again
 * later if it ever comes back — otherwise a retry would silently do nothing. */
async function forget(pks) {
  const { opened } = await state();
  let changed = false;
  for (const pk of Object.keys(opened)) {
    if (!pks.has(pk)) { delete opened[pk]; changed = true; }
  }
  if (changed) await chrome.storage.local.set({ opened });
}

async function tick() {
  await openHandoffs();
  try {
    const q = await (await fetch(`${API}/extension/queue`, { cache: "no-store" })).json();
    await forget(new Set((q.jobs || []).map((j) => j.pk)));
  } catch { /* offline */ }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("appliedin-poll", { periodInMinutes: POLL_SECONDS / 60 });
  tick();
});
chrome.runtime.onStartup.addListener(tick);
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "appliedin-poll") tick(); });

chrome.runtime.onMessage.addListener((msg, _s, respond) => {
  if (msg?.type === "APPLIEDIN_SYNC") { tick().then(() => respond({ ok: true })); return true; }
  if (msg?.type === "APPLIEDIN_DONE") {
    // A confirmed application leaves the queue on the next poll; refresh now so
    // the badge is honest immediately.
    tick().then(() => respond({ ok: true }));
    return true;
  }
  return false;
});
