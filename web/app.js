// AppliedIn — a calm console for the job-application pipeline.
// Vanilla ES module, no build step. One screen: a control bar with the two-step
// flow (Discover → Process), a tabbed work area (Pipeline board / Applications
// table / Needs you / Unable to do it), and a live activity rail.
// Page load only SHOWS current state — nothing runs until a button is pressed.

import { auth } from "./auth.js";

const CONFIG = window.APPLIEDIN_CONFIG || {};
const DEMO = CONFIG.demo === true || new URLSearchParams(location.search).has("demo");

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = (p) => (CONFIG.apiUrl || "").replace(/\/$/, "") + p;

const state = {
  apps: [],
  stats: {},
  events: [],
  tab: "pipeline",    // pipeline | apps | needs | stuck | logs
  filter: "all",      // status chip on the Applications table
  logKind: "all",     // kind chip on the Logs view
  liveState: "off",   // SSE connection state (off | connecting | live | demo)
  query: "",          // free-text search
  coFilter: "",       // board filter: show one company only ("" = all)
  openPk: "",         // pk in the open detail drawer (streams its agent log)
  companies: [],      // watchlist names for the discovery picker
  picked: new Set(),  // companies picked for the next discovery ("" empty = all)
  skipped: new Set(), // lowercase names excluded from un-scoped Discover/Process
  filters: {},        // {company_lower: [title keyword, ...]} per-company title filters
  activity: {},       // pk -> {detail, at} — the live step, shown on active cards
  coQuery: "",        // search inside the company picker
  mode: "gated",
  headless: false,
  paused: false,
  autoMin: 8,
};

// --- text helpers ----------------------------------------------------------
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}
// Minimal, self-contained markdown → HTML for agent messages (bold, italic,
// code, links, bullet/numbered lists). Escapes first, so it's XSS-safe.
function md(src) {
  let s = esc(src || "");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+?)\*/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [];
  let ul = false, ol = false;
  const closeLists = () => {
    if (ul) { out.push("</ul>"); ul = false; }
    if (ol) { out.push("</ol>"); ol = false; }
  };
  for (const line of s.split("\n")) {
    let m;
    if ((m = line.match(/^\s*[-*]\s+(.*)/))) {
      if (ol) { out.push("</ol>"); ol = false; }
      if (!ul) { out.push("<ul>"); ul = true; }
      out.push(`<li>${m[1]}</li>`);
    } else if ((m = line.match(/^\s*\d+\.\s+(.*)/))) {
      if (ul) { out.push("</ul>"); ul = false; }
      if (!ol) { out.push("<ol>"); ol = true; }
      out.push(`<li>${m[1]}</li>`);
    } else if (line.trim() === "") {
      closeLists(); out.push("");
    } else {
      closeLists(); out.push(line + "<br>");
    }
  }
  closeLists();
  return out.join("\n");
}
// Some log payloads arrive JSON-escaped — decode common escapes so raw logs
// read as text/lines, not backslash noise. Runs BEFORE esc()/md().
function unraw(s) {
  return String(s ?? "")
    .replace(/\\u([0-9a-fA-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "  ");
}
function ago(ts) {
  if (!ts) return "—";
  const d = (Date.now() - new Date(ts).getTime()) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}
function when(ts) {
  return ts ? new Date(ts).toLocaleString([], { month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit" }) : "—";
}
// Demo rows use "#" as a stand-in URL — fine for links, broken for <img>.
const imgOk = (u) => !!u && u !== "#";

// --- status vocabulary -----------------------------------------------------
const STATUS_META = {
  found:          { label: "found",            cls: "s-found" },
  tailoring:      { label: "tailoring…",       cls: "s-tailoring" },
  tailored:       { label: "tailored",         cls: "s-work" },
  submitting:     { label: "applying…",        cls: "s-live" },
  applied:        { label: "applied",          cls: "s-ok" },
  applied_manual: { label: "applied (manual)", cls: "s-ok" },
  needs_human:    { label: "needs you",        cls: "s-warn" },
  failed:         { label: "failed",           cls: "s-bad" },
  error:          { label: "error",            cls: "s-bad" },
  uncertain:      { label: "uncertain",        cls: "s-bad" },
  job_gone:       { label: "job gone",         cls: "s-dim" },
  skipped:        { label: "skipped",          cls: "s-dim" },
  capped:         { label: "capped",           cls: "s-dim" },
};
function tagHtml(s) {
  const m = STATUS_META[s] || { label: String(s || "—").replace(/_/g, " "), cls: "s-dim" };
  return `<span class="pill ${m.cls}">${esc(m.label)}</span>`;
}
// Honest, human labels for WHY a job is waiting — never jargon.
const GATE_LABEL = {
  approval: "awaiting your approval",
  unknown_field: "needs an answer from you",
  captcha: "CAPTCHA — finish manually",
  no_account: "account/login needed",
  low_confidence: "needs review",
};
const gateLabel = (r) => GATE_LABEL[r] || String(r || "review").replace(/_/g, " ");
function defaultGateText(r) {
  if (r.gate_reason === "no_account") return "Auto-signup was blocked. Approve once the account exists.";
  if (r.gate_reason === "captcha") return "A CAPTCHA blocked the bot — apply manually, then approve to mark done.";
  return "Paused for your go-ahead — approve to continue.";
}

// The deliberately-kept "Unable to do it" bucket: things the automation could
// not finish — each with its reason, screenshot and timeline, nothing hidden.
// (Skipped/capped are deliberate outcomes, so they live under the closed
// filter instead.)
const isStuck = (r) => ["failed", "error", "job_gone", "uncertain"].includes(r.status);
const RETRYABLE = ["failed", "error", "capped", "uncertain"];

const FILTERS = [
  ["all", "all", () => true],
  ["pipeline", "in pipeline", (r) => ["found", "tailored", "submitting", "capped"].includes(r.status)],
  ["needs", "needs you", (r) => r.status === "needs_human"],
  ["applied", "applied", (r) => ["applied", "applied_manual"].includes(r.status)],
  ["stuck", "unable", isStuck],
  ["closed", "closed", (r) => ["skipped", "job_gone", "capped"].includes(r.status)],
];

// Confidence → colour. 0 = red … 10 = green, continuous through amber.
function scoreColor(n) {
  const h = Math.max(0, Math.min(10, n)) / 10 * 125;
  return `hsl(${Math.round(h)} 68% 45%)`;
}
function scoreHtml(n) {
  if (n == null) return '<span class="score">—</span>';
  const c = scoreColor(n);
  return `<span class="score" style="color:${c}"><span class="score-bar">` +
    `<span style="width:${n * 10}%;background:${c}"></span></span>${n}</span>`;
}

// --- filtering (shared by every view) --------------------------------------
const byCompany = (list) =>
  state.coFilter ? list.filter((r) => r.company === state.coFilter) : list;

function visible(list) {
  const q = state.query.trim().toLowerCase();
  let f = byCompany(list);
  if (q) f = f.filter((r) => `${r.company} ${r.title}`.toLowerCase().includes(q));
  return [...f].sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
}
const filtersActive = () => !!(state.coFilter || state.query.trim());
function emptyFiltered() {
  return `<div class="empty">Nothing matches your current filters.
    <div class="empty-act"><button class="btn btn-ghost" data-clear-filters="1">Clear filters</button></div>
  </div>`;
}

// --- control bar -----------------------------------------------------------
const pickedAll = () =>
  state.picked.size === 0 || (state.companies.length > 0 && state.picked.size === state.companies.length);
const discoverScope = () => (pickedAll() ? [] : [...state.picked]);

function renderDiscoverLabel() {
  // ONE company selection scopes BOTH actions: Discover scans just the picked
  // companies, and Process runs the pipeline on just their discovered jobs.
  const el = $("#discover-label");
  if (state.stats.discovering) el.textContent = "Discovering…";
  else el.textContent = pickedAll()
    ? "Discover · All"
    : `Discover · ${state.picked.size} selected`;
  const pl = $("#process-label");
  if (state.stats.processing) pl.textContent = "Processing…";
  else pl.textContent = pickedAll()
    ? "Process applications"
    : `Process · ${state.picked.size} selected`;
}

function renderPicker() {
  const list = $("#cp-list");
  const keepScroll = list.scrollTop; // don't jump on select-all/clear
  if (!state.companies.length) {
    list.innerHTML = `<div class="cp-none">No watchlist loaded yet.</div>`;
  } else {
    const q = state.coQuery.trim().toLowerCase();
    const rows = state.companies
      .filter((c) => !q || c.toLowerCase().includes(q))
      .map((c) => {
        const sk = state.skipped.has(c.toLowerCase());
        return `<div class="cp-item ${sk ? "skipped" : ""}">
          <label class="cp-name">
            <input type="checkbox" value="${esc(c)}" ${state.picked.has(c) ? "checked" : ""}>
            <span>${esc(c)}</span></label>
          <button class="cp-skip" type="button" data-name="${esc(c)}" data-skip="${sk ? 0 : 1}"
            title="${sk ? "Bring this company back into Discover + Process"
                        : "Skip this company — Discover + Process pass over it"}">${sk ? "↺" : "⊘"}</button>
        </div>`;
      })
      .join("");
    list.innerHTML = rows || `<div class="cp-none">No companies match “${esc(state.coQuery)}”.</div>`;
  }
  list.scrollTop = keepScroll;
  renderPickerState();
}
// State line + footer only — checkbox toggles call this so the list DOM (and
// its scroll position) stays put while you tick companies.
function renderPickerState() {
  const n = state.picked.size, total = state.companies.length;
  $("#cp-state").textContent = pickedAll() ? `all ${total}` : `${n} of ${total}`;
  const sk = state.skipped.size;
  const skNote = sk ? ` <span class="cp-skipnote">· ${sk} skipped</span>` : "";
  const line = pickedAll()
    ? `Discover + Process run on the <b>whole watchlist</b>${total ? ` (${total - sk} of ${total} companies)` : ""}.`
    : `Discover + Process run on <b>${n} compan${n === 1 ? "y" : "ies"}</b> only.`;
  // Pick exactly ONE company → a clear per-company title filter + run button,
  // right where you decide to run it (not buried in the skip list).
  let single = "";
  if (n === 1) {
    const one = [...state.picked][0];
    const f = (state.filters[one.toLowerCase()] || []).join(", ");
    single = `<div class="cp-one">
      <div class="cp-one-fl">
        <label for="cp-one-filter">Only ${esc(one)} titles containing</label>
        <input id="cp-one-filter" class="cp-search" data-filter-co="${esc(one)}"
          value="${esc(f)}" placeholder="e.g. Staff  ·  blank = all titles"
          autocomplete="off" spellcheck="false">
      </div>
      <button class="cp-runone" data-runone="${esc(one)}"
        title="Discover ${esc(one)}'s postings (matching the filter), score + tailor them — stops before applying">▶ Discover + tailor ${esc(one)}</button>
    </div>`;
  }
  $("#cp-foot").innerHTML = line + skNote + single;
}

function renderSkipPicker() {
  const list = $("#sp-list");
  if (!list) return;
  const q = (state.spQuery || "").trim().toLowerCase();
  const rows = state.companies
    .filter((c) => !q || c.toLowerCase().includes(q))
    .map((c) => {
      const sk = state.skipped.has(c.toLowerCase());
      return `<label class="cp-item sp-item ${sk ? "skipped" : ""}">
        <input type="checkbox" value="${esc(c)}" ${sk ? "checked" : ""}>
        <span>${esc(c)}</span></label>`;
    }).join("");
  list.innerHTML = rows || `<div class="cp-none">No companies match.</div>`;
  const n = state.skipped.size, badge = $("#skip-count");
  if (badge) { badge.hidden = !n; badge.textContent = n; }
  const b = $("#btn-skips");
  if (b) b.classList.toggle("on", n > 0);
}

function renderLlmBanner() {
  const b = $("#llm-banner");
  if (!b) return;
  const err = state.stats.llm_error;
  if (!err || !err.msg) { b.hidden = true; return; }
  $("#llm-banner-msg").textContent =
    `LLM failure in ${err.where || "the pipeline"}: ${err.msg} — screening degraded ` +
    `to the keyword filter; scoring/tailoring/applying will fail until this is fixed.`;
  b.hidden = false;
}

function renderDeck() {
  renderLlmBanner();
  const s = state.stats || {};
  const c = s.counts_by_status || {};
  const waiting = s.found_waiting ?? c.found ?? 0;

  $("#v-wait").textContent = waiting;
  $("#v-cap").textContent = s.today_submitted ?? 0;
  const needs = c.needs_human || 0;
  $("#v-needs").textContent = needs;
  $("#vital-needs").classList.toggle("hot", needs > 0);

  const ORDER = [
    ["found", "found"], ["tailored", "tailored"], ["submitting", "applying"],
    ["needs_human", "needs you"], ["applied", "applied"], ["applied_manual", "manual"],
    ["failed", "failed"], ["error", "errors"], ["uncertain", "uncertain"],
    ["skipped", "skipped"], ["job_gone", "gone"], ["capped", "capped"],
  ];
  $("#breakdown").innerHTML = ORDER.filter(([k]) => c[k])
    .map(([k, l]) => `<span class="bd ${(STATUS_META[k] || {}).cls || ""}"><i></i>${l} ${c[k]}</span>`)
    .join("") || `<span class="bd dim">no jobs tracked yet</span>`;

  const disc = $("#btn-discover"), proc = $("#btn-process");
  disc.disabled = !!s.discovering;
  disc.classList.toggle("running", !!s.discovering);
  renderDiscoverLabel();
  proc.disabled = !!s.processing;
  proc.classList.toggle("running", !!s.processing);
  renderDiscoverLabel();  // both action labels reflect run-state + company scope
  const badge = $("#proc-badge");
  badge.textContent = waiting;
  badge.classList.toggle("zero", !waiting);

  $("#paused-pill").hidden = !state.paused;
  $("#runbar").dataset.active = (s.discovering || s.processing) ? "true" : "false";
  renderMenuState();
}
function renderMenuState() {
  const pause = $("#m-pause-state");
  pause.textContent = state.paused ? "paused" : "running";
  pause.className = "menu-state mono " + (state.paused ? "warn" : "on");
  const mode = $("#m-mode-state");
  mode.textContent = { auto: "auto ☾", assisted: "assisted ⎋", gated: "gated" }[state.mode] || "gated";
  mode.className = "menu-state mono " + (state.mode === "gated" ? "" : "on");
  const hl = $("#m-headless-state");
  if (hl) {
    hl.textContent = state.headless ? "headless" : "visible";
    hl.className = "menu-state mono " + (state.headless ? "warn" : "on");
  }
  $("#m-theme-state").textContent = document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

// --- tabs + company filter -------------------------------------------------
function renderTabs() {
  $$("#tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
  // The Logs tab is the roomy version of the live rail — hide the rail there.
  $(".board").classList.toggle("logs-open", state.tab === "logs");
  $("#tab-n-apps").textContent = state.apps.length;
  const needs = state.apps.filter((a) => a.status === "needs_human").length;
  const stuck = state.apps.filter(isStuck).length;
  const nb = $("#tab-n-needs"); nb.hidden = !needs; nb.textContent = needs;
  const sb = $("#tab-n-stuck"); sb.hidden = !stuck; sb.textContent = stuck;
  renderCoFilter();
}

let _coSig = null;
function renderCoFilter() {
  const sel = $("#co-filter");
  const cos = [...new Set(state.apps.map((a) => a.company).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b));
  if (state.coFilter && !cos.includes(state.coFilter)) { state.coFilter = ""; _coSig = null; }
  sel.classList.toggle("on", !!state.coFilter);
  const sig = cos.join("|");
  if (sig === _coSig) return; // don't rebuild (and close) the dropdown needlessly
  _coSig = sig;
  sel.innerHTML = `<option value="">All companies</option>` + cos.map((co) =>
    `<option value="${esc(co)}">${esc(co)}</option>`).join("");
  sel.value = state.coFilter;
}

// --- Pipeline board (kanban lanes by status) -------------------------------
const awaitsApproval = (r) => r.gate_reason === "approval" && !!r.gate_question;
// Every tailored job can be applied with one click — including older rows
// tailored before approval gates carried the status.
const canApply = (r) => r.status === "tailored" || awaitsApproval(r);

const LANES = [
  { label: "Found",      st: ["found"],                cls: "ln-found",
    match: (r) => r.status === "found",
    hint: "Discovered — waiting for a Process run" },
  { label: "Tailored",   st: ["tailored", "tailoring", "needs_human"], cls: "ln-ready",
    match: (r) => r.status === "tailored" || r.status === "tailoring"
                  || (r.status === "needs_human" && awaitsApproval(r)),
    hint: "Résumé tailored (or tailoring now, in yellow) — the apply waits for your ▶ approval" },
  { label: "Submitting", st: ["submitting"],           cls: "ln-live",
    hint: "The browser is filling the application now" },
  { label: "Applied",    st: ["applied", "applied_manual"], cls: "ln-ok",
    hint: "Submitted — confirmation captured" },
  { label: "Human check", st: ["needs_human"],         cls: "ln-captcha",
    match: (r) => r.status === "needs_human" && r.gate_reason === "captcha",
    hint: "Filled and ready — a CAPTCHA needs 10 seconds of human. One click re-opens the window" },
  { label: "Needs you",  st: ["needs_human"],          cls: "ln-warn",
    match: (r) => r.status === "needs_human" && !awaitsApproval(r)
                  && r.gate_reason !== "captcha",
    hint: "Paused on a question — answer in the Needs-you tab" },
  { label: "Flagged",    st: ["failed"],               cls: "ln-flag",
    match: (r) => r.status === "failed" && r.fail_kind === "spam_flagged",
    hint: "The portal called the submit 'possible spam' — Retry re-runs it with human-style clicks" },
  { label: "Unable",     st: ["failed", "error", "job_gone", "uncertain"], cls: "ln-bad",
    match: (r) => ["failed", "error", "job_gone", "uncertain"].includes(r.status)
                  && r.fail_kind !== "spam_flagged",
    hint: "The automation couldn't finish — reason & screenshot inside" },
];

// A job's location, marked when it matches the FIRST tier of the location
// preference (Washington) so the ranking is visible at a glance rather than
// something you have to open each card to check.
const TOP_LOCATIONS = ["seattle", "bellevue", "washington", "redmond", "kirkland"];
function locHtml(loc) {
  if (!loc) return "";
  const l = String(loc).toLowerCase();
  const top = TOP_LOCATIONS.some((t) => l.includes(t));
  const remote = /\bremote\b/.test(l);
  const cls = top ? "kc-loc top" : remote ? "kc-loc remote" : "kc-loc";
  return `<div class="${cls}" title="${esc(loc)}">${top ? "★ " : ""}${esc(String(loc).slice(0, 44))}</div>`;
}

function laneCard(r) {
  let retry = "";
  if (r.status === "failed" && r.fail_kind === "spam_flagged") {
    retry = `<button class="kc-retry" data-act="retry" data-pk="${esc(r.pk)}"
       title="Re-run this application with human-style clicks">↻ Retry</button>`;
  } else if (r.status === "needs_human" && r.gate_reason === "captcha") {
    retry = `<button class="kc-retry kc-apply" data-act="answer" data-pk="${esc(r.pk)}"
       title="Re-opens the filled application in Chrome — solve the CAPTCHA there and click Submit">▶ Solve &amp; submit</button>`;
  } else if (canApply(r)) {
    retry = `<button class="kc-retry kc-apply" data-act="answer" data-pk="${esc(r.pk)}"
       title="Approve — the browser applies with the tailored résumé">▶ Apply</button>`;
  } else if (r.status === "found") {
    retry = `<button class="kc-retry kc-run" data-act="run-now" data-pk="${esc(r.pk)}"
       title="Score + tailor this job now (stops at ready-to-apply)">▶ Run now</button>`;
  }
  const act = state.activity[r.pk];
  const live = (["tailoring", "submitting"].includes(r.status) && act && act.detail)
    ? `<div class="kc-live"><span class="kc-live-dot"></span>${esc(act.detail.replace(/^[^\w]+/, "").slice(0, 70))}</div>` : "";
  return `<div class="kcard" data-open="${esc(r.pk)}" role="button" tabindex="0"
      title="Open details">
    <div class="kc-co">${esc(r.company)}</div>
    <div class="kc-role">${esc(r.title)}</div>
    ${locHtml(r.location)}
    ${live}
    <div class="kc-foot">${scoreHtml(r.match_score)}${tagHtml(r.status)}${retry}</div>
  </div>`;
}
function viewPipeline() {
  if (!state.apps.length) {
    return `<div class="empty"><div class="empty-big">No applications yet</div>
      Click <b>Discover · All</b> above to find jobs from your watchlist —<br>
      they'll move across this board: found → tailored → applied.</div>`;
  }
  let shown = 0;
  const lanes = LANES.map((l) => {
    const rows = visible(state.apps.filter((r) => l.match ? l.match(r) : l.st.includes(r.status)));
    shown += rows.length;
    const runAll = (l.label === "Found" && rows.length)
      ? `<button class="kl-runall" data-run-all="1" title="Score + tailor all ${rows.length} found jobs (stops each at ready-to-apply)">▶ Run all</button>`
      : "";
    return `<div class="klane ${l.cls}">
      <div class="kl-head" title="${esc(l.hint)}">
        <span class="kl-dot"></span><span class="kl-name">${l.label}</span>
        <span class="kl-n mono">${rows.length}</span>${runAll}
      </div>
      <div class="kl-cards">${rows.map(laneCard).join("") || `<div class="kl-empty">—</div>`}</div>
    </div>`;
  }).join("");
  if (!shown && filtersActive()) return emptyFiltered();
  const nApprove = visible(state.apps.filter(canApply)).length;
  const approveNote = nApprove ? `<div class="pane-note">
    <span>${nApprove} tailored job${nApprove === 1 ? "" : "s"} await${nApprove === 1 ? "s" : ""} your apply approval.</span>
    <button class="btn btn-amber" data-approve-all="1">Approve all ${nApprove}</button>
  </div>` : "";
  const base = byCompany(state.apps);
  const hidden = [["skipped", "skipped"], ["capped", "capped"]]
    .map(([k, l]) => [base.filter((r) => r.status === k).length, l])
    .filter(([n]) => n)
    .map(([n, l]) => `${n} ${l}`);
  const note = hidden.length
    ? `<div class="knote">Not on the board: ${hidden.join(" · ")} — see the Applications tab.</div>` : "";
  return `${approveNote}<div class="kboard">${lanes}</div>${note}`;
}

// --- Applications table ----------------------------------------------------
function viewApps() {
  const base = byCompany(state.apps);
  const counts = {};
  for (const [k, , pred] of FILTERS) counts[k] = base.filter(pred).length;
  const chips = FILTERS.map(([k, label]) =>
    `<button class="chip" data-filter="${k}" aria-selected="${state.filter === k}">
      ${label}<span class="n">${counts[k]}</span></button>`).join("");
  const pred = FILTERS.find(([k]) => k === state.filter)[2];
  const rows = visible(state.apps.filter(pred));
  const body = rows.map((r) => {
    const cv = (r.resume_url || r.resume_version)
      ? `<button class="rowlk" data-resume="${esc(r.pk)}" title="View tailored résumé">cv</button>`
      : `<span class="rowlk off">cv</span>`;
    const jd = r.jd_url
      ? `<a class="rowlk" href="${esc(r.jd_url)}" target="_blank" rel="noopener" title="Open job posting">jd</a>`
      : `<span class="rowlk off">jd</span>`;
    const sc = r.screenshot_url
      ? `<a class="rowlk" href="${esc(r.screenshot_url)}" target="_blank" rel="noopener" title="Last screenshot">shot</a>`
      : `<span class="rowlk off">shot</span>`;
    return `<tr data-pk="${esc(r.pk)}">
      <td>${tagHtml(r.status)}</td>
      <td class="t-co">${esc(r.company)}</td>
      <td class="t-role" title="${esc(r.title)}">${esc(r.title)}</td>
      <td>${scoreHtml(r.match_score)}</td>
      <td><span class="t-links">${cv}${jd}${sc}</span></td>
      <td class="t-when">${ago(r.updated_at)}</td>
    </tr>`;
  }).join("");
  const empty = !state.apps.length
    ? `<div class="empty"><div class="empty-big">No applications yet</div>
        Click <b>Discover</b> above to find and queue jobs,<br>
        then <b>Process applications</b> to score, tailor and apply.</div>`
    : (rows.length ? "" : (filtersActive() ? emptyFiltered()
        : `<div class="empty">Nothing under this status filter yet.</div>`));
  return `<div class="chips">${chips}</div>
    <div class="tablewrap"><table class="data"><thead><tr>
      <th>status</th><th>company</th><th>role</th><th>match</th><th>links</th><th>updated</th>
    </tr></thead><tbody>${body}</tbody></table>${empty}</div>`;
}

// --- Needs-you cards -------------------------------------------------------
function needsCard(r) {
  return `<article class="gatecard">
    <header>
      <div><span class="gc-co">${esc(r.company)}</span><span class="gc-role">${esc(r.title)}</span></div>
      <div class="gc-side">
        <span class="gatepill">${esc(gateLabel(r.gate_reason))}</span>
        <button class="btn btn-ghost" data-open="${esc(r.pk)}">Details</button>
      </div>
    </header>
    <div class="gc-q md">${md(r.gate_question || defaultGateText(r))}</div>
    <div class="gc-answer">
      <textarea class="gate-input" rows="2" data-answer-for="${esc(r.pk)}"
        placeholder="Type an answer — or leave blank to just approve…"></textarea>
      <div class="gc-actions">
        <button class="btn btn-primary" data-act="answer" data-pk="${esc(r.pk)}">Approve &amp; continue</button>
        <button class="btn btn-ghost" data-act="skip" data-pk="${esc(r.pk)}">Skip job</button>
        ${r.jd_url ? `<a class="gc-jd" href="${esc(r.jd_url)}" target="_blank" rel="noopener">view posting ↗</a>` : ""}
      </div>
    </div>
  </article>`;
}
function viewNeeds() {
  const all = state.apps.filter((a) => a.status === "needs_human");
  const items = visible(all);
  if (!items.length) {
    return all.length && filtersActive() ? emptyFiltered()
      : `<div class="empty"><div class="empty-big">Nothing needs you</div>
          The pipeline is running clean — questions and approvals will land here.</div>`;
  }
  const approvable = items.filter((a) => a.gate_reason === "approval").length;
  const head = `<div class="pane-note">
    <span>${items.length} waiting on you — answer or approve to continue.</span>
    ${approvable ? `<button class="btn btn-amber" data-approve-all="1">
      Approve all ${approvable} ready</button>` : ""}
  </div>`;
  return head + items.map(needsCard).join("");
}

// --- Unable-to-do-it cards -------------------------------------------------
// Full failure visibility: the plain-English reason, what the browser last
// saw (click to enlarge), the step timeline, and a path to every detail.
function viewStuck() {
  const all = state.apps.filter(isStuck);
  const items = visible(all);
  if (!items.length) {
    return all.length && filtersActive() ? emptyFiltered()
      : `<div class="empty"><div class="empty-big">Nothing is stuck</div>
          When the automation can't finish an application it lands here,
          with the reason and what the browser last saw.</div>`;
  }
  return items.map((r) => {
    const reason = r.closed_reason || r.gate_question || "Couldn't complete this application.";
    const shot = imgOk(r.screenshot_url)
      ? `<a class="stuck-shot" href="${esc(r.screenshot_url)}" target="_blank" rel="noopener"
           title="What the browser last saw — click to enlarge">
           <img src="${esc(r.screenshot_url)}" alt="what the browser saw" loading="lazy"></a>`
      : `<div class="stuck-shot none">no screenshot</div>`;
    const steps = (r.timeline || []).slice(-4).map((t) =>
      `<div class="mini-tl"><span class="mini-dot"></span><span class="mini-lbl">${esc(t.label)}</span>
        <span class="mini-when mono">${when(t.at)}</span></div>`).join("");
    const nf = (r.fields || []).length;
    return `<article class="stuckcard">
      <div class="stuck-main">
        <header>
          <div><span class="gc-co">${esc(r.company)}</span><span class="gc-role">${esc(r.title)}</span></div>
          ${tagHtml(r.status)}
        </header>
        <div class="stuck-reason">${esc(reason)}</div>
        ${steps ? `<div class="mini-timeline">${steps}</div>` : ""}
        <div class="stuck-meta mono">
          ${r.discovered_at ? `found ${when(r.discovered_at)} · ` : ""}${esc(r.pk)}
          ${r.jd_url ? ` · <a href="${esc(r.jd_url)}" target="_blank" rel="noopener">posting ↗</a>` : ""}
        </div>
        <div class="gc-actions">
          ${RETRYABLE.includes(r.status)
            ? `<button class="btn btn-primary" data-act="retry" data-pk="${esc(r.pk)}">Retry</button>` : ""}
          <button class="btn btn-ghost" data-open="${esc(r.pk)}">
            Full details${nf ? ` · ${nf} fields` : ""}</button>
        </div>
      </div>
      ${shot}
    </article>`;
  }).join("");
}

// --- Logs: the full-height, filterable version of the live stream ----------
// The owner watches the agents work step-by-step here. Same events as the
// side rail, with room to read: kind chips + the shared company filter and
// search box up top, newest first.
const LOG_FILTERS = [
  ["all", "all", () => true],
  ["model", "model", (e) => e.kind === "response" || e.kind === "input"],
  ["calls", "tool calls", (e) => e.kind === "action" || e.kind === "result"],
  ["gate", "gates", (e) => e.kind === "gate"],
  ["found", "discovery", (e) => e.kind === "discovered" || e.kind === "discovery"],
  ["applied", "applied", (e) => e.kind === "applied"],
  ["error", "errors", (e) => e.kind === "error"],
];
const eventCompany = (e) =>
  e.pk ? ((state.apps.find((a) => a.pk === e.pk) || {}).company || "") : "";

function logFiltered() {
  const pred = (LOG_FILTERS.find(([k]) => k === state.logKind) || LOG_FILTERS[0])[2];
  const q = state.query.trim().toLowerCase();
  let evs = state.events.filter(pred);
  if (state.coFilter) evs = evs.filter((e) => eventCompany(e) === state.coFilter);
  if (q) {
    evs = evs.filter((e) =>
      `${eventCompany(e)} ${e.agent || ""} ${e.detail || ""} ${e.input || ""} ${e.output || ""}`
        .toLowerCase().includes(q));
  }
  return evs;
}
function viewLogs() {
  const chips = LOG_FILTERS.map(([k, label]) =>
    `<button class="chip" data-logkind="${k}" aria-selected="${state.logKind === k}">${label}</button>`)
    .join("");
  const evs = logFiltered();
  const st = state.liveState;
  const hint = st === "live" ? "live" : st === "demo" ? "demo replay" : "connecting…";
  const empty = !state.events.length
    ? `<div class="empty"><div class="empty-big">Nothing logged yet</div>${DEMO
        ? "Demo replay only — run the local backend to watch the agents live."
        : "Every agent step streams here the moment a run starts — try <b>Discover</b>."}</div>`
    : (evs.length ? "" : (state.logKind !== "all" || filtersActive()
        ? `<div class="empty">No log lines match your filters.
            <div class="empty-act"><button class="btn btn-ghost" data-clear-filters="1">Clear filters</button></div></div>`
        : ""));
  return `<div class="log-head">
      <div class="chips">${chips}</div>
      <span class="log-live mono"><span class="feed-dot" id="log-dot" data-state="${st}"></span>
        <span id="log-hint">${hint}</span><span class="log-sep">·</span>
        <span id="log-count">${evs.length} event${evs.length === 1 ? "" : "s"}</span></span>
    </div>
    <div class="logview" id="log-stream">${evs.slice(0, 500).map(feedItem).join("")}</div>${empty}`;
}

function renderPane() {
  // Preserve half-typed gate answers across re-renders (SSE refreshes etc).
  const saved = {};
  let focusPk = null;
  $$("#pane textarea[data-answer-for]").forEach((t) => {
    if (t.value) saved[t.dataset.answerFor] = t.value;
    if (document.activeElement === t) focusPk = t.dataset.answerFor;
  });
  $("#pane").innerHTML =
    state.tab === "apps" ? viewApps() :
    state.tab === "needs" ? viewNeeds() :
    state.tab === "stuck" ? viewStuck() :
    state.tab === "logs" ? viewLogs() : viewPipeline();
  for (const [pk, v] of Object.entries(saved)) {
    const t = $(`#pane textarea[data-answer-for="${CSS.escape(pk)}"]`);
    if (t) t.value = v;
  }
  if (focusPk) {
    const t = $(`#pane textarea[data-answer-for="${CSS.escape(focusPk)}"]`);
    if (t) { t.focus(); t.selectionStart = t.selectionEnd = t.value.length; }
  }
}

function renderFooter() {
  const n = state.apps.length;
  $("#foot-count").textContent = `${n} application${n === 1 ? "" : "s"}`;
  $("#foot-updated").textContent = "updated " + new Date().toLocaleTimeString("en-GB");
}
function renderAll() {
  renderDeck(); renderTabs(); renderPane(); renderFeed(); renderFooter();
}

// --- live activity feed ----------------------------------------------------
const STAGE_LABEL = { scorer: "score", tailor: "tailor", critic: "critique",
  applier: "apply", browser: "browser", writer: "writer", workflow: "workflow",
  daemon: "pipeline", tailor_critique: "tailor", appliedin_pipeline: "pipeline" };
const KIND = {
  response:   { mark: "model",  cls: "k-model" },
  input:      { mark: "input",  cls: "k-in" },
  action:     { mark: "→ call", cls: "k-call" },
  result:     { mark: "← ret",  cls: "k-ret" },
  gate:       { mark: "gate",   cls: "k-gate" },
  applied:    { mark: "done ✓", cls: "k-ok" },
  discovered: { mark: "found",  cls: "k-found" },
  discovery:  { mark: "found",  cls: "k-found" },
  running:    { mark: "start",  cls: "k-start" },
  error:      { mark: "error",  cls: "k-bad" },
};
const CLIP_AT = 360; // longer than this → collapsed behind "show all"

// Turn machine payloads into something a human can skim.
function smartRaw(e, raw) {
  const t = (raw || "").trim();
  if (e.kind === "result" && /load_skill|run_skill_script/.test(e.detail || "")) {
    const m = t.match(/skill_name"?\s*:\s*"([^"]+)/);
    return `loaded skill${m ? `: ${m[1]}` : ""} (instructions hidden)`;
  }
  const sc = t.match(/^\{\s*"?score"?\s*:\s*(\d+)\s*,\s*"?reasoning"?\s*:\s*"([\s\S]*)/);
  if (sc) return `score ${sc[1]}/10 — ${sc[2].replace(/["}\s…]*$/, "")}`;
  if ((e.kind === "result" || e.kind === "response") && t.startsWith("{")) {
    try {
      const o = JSON.parse(t);
      return Object.entries(o).map(([k, v]) =>
        `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join("\n");
    } catch { /* truncated JSON — show as-is */ }
  }
  return raw;
}

function feedItem(e) {
  const t = e.at ? new Date(e.at).toLocaleTimeString("en-GB") : "";
  const app = e.pk ? state.apps.find((a) => a.pk === e.pk) : null;
  const co = app ? app.company : (e.company || "");
  const stage = STAGE_LABEL[e.agent] || e.agent || "";
  const coChip = !co ? ""
    : app ? `<span class="fe-co" data-open="${esc(e.pk)}" title="open ${esc(co)}">${esc(co)}</span>`
          : `<span class="fe-co">${esc(co)}</span>`;
  if (e.kind === "step") {
    return `<div class="fe fe-step mono">
      <span>${esc(STAGE_LABEL[e.detail] || e.detail || stage)}</span>${coChip}</div>`;
  }
  const k = KIND[e.kind] || { mark: e.kind || "·", cls: "" };
  let raw = e.detail || "";
  if (e.kind === "action") raw = `${e.detail}(${e.input ?? ""})`;
  else if (e.kind === "result") raw = `${e.detail}  ${e.output ?? ""}`;
  const text = unraw(smartRaw(e, raw));
  const isMd = e.kind === "response" || e.kind === "gate";
  const long = text.length > CLIP_AT;
  const shot = e.screenshot
    ? `<a class="fe-shot" href="${esc(e.screenshot)}" target="_blank" rel="noopener">
         <img src="${esc(e.screenshot)}" alt="screenshot" loading="lazy"></a>` : "";
  return `<div class="fe ${k.cls}">
    <div class="fe-h mono"><span class="fe-t">${t}</span>
      ${stage ? `<span class="fe-stg">${esc(stage)}</span>` : ""}
      <span class="fe-mark">${esc(k.mark)}</span>${coChip}</div>
    <div class="fe-b ${isMd ? "md" : ""} ${long ? "clipped" : ""}">${isMd ? md(text) : esc(text)}</div>
    ${long ? `<button class="fe-more mono">show all ▾</button>` : ""}
    ${shot}
  </div>`;
}

const FEED_MAX = 250;
let _feedTimer = null;
let _liveT = null;
function scheduleLive(pk) {
  // Only re-render if this pk is an actively-working card on the board.
  const a = state.apps.find((r) => r.pk === pk);
  if (!a || !["tailoring", "submitting"].includes(a.status)) return;
  if (_liveT) return;
  _liveT = setTimeout(() => {
    _liveT = null;
    if (!["apps", "needs", "stuck", "logs"].includes(state.tab)) renderPane();  // pipeline view
  }, 500);
}

function scheduleFeed() {
  if (_feedTimer) return;
  _feedTimer = setTimeout(() => { _feedTimer = null; renderFeed(); }, 180);
}
function renderFeed() {
  const el = $("#feed");
  if (!state.events.length) {
    el.innerHTML = `<div class="feed-empty mono">${DEMO
      ? "demo replay — run the local backend for the real stream"
      : "waiting for activity — every agent step streams here the moment a run starts"}</div>`;
  } else {
    el.innerHTML = state.events.slice(0, FEED_MAX).map(feedItem).join("");
  }
  // Keep the open Logs tab streaming too (rows only — filters stay put).
  if (state.tab === "logs") {
    const s = $("#log-stream");
    if (s) {
      const evs = logFiltered();
      s.innerHTML = evs.slice(0, 500).map(feedItem).join("");
      const c = $("#log-count");
      if (c) c.textContent = `${evs.length} event${evs.length === 1 ? "" : "s"}`;
    }
  }
}

function setFeedStatus(st) {
  state.liveState = st;
  const label = st === "live" ? "live" : st === "demo" ? "demo replay" : "connecting…";
  $("#feed-dot").dataset.state = st;
  $("#feed-hint").textContent = label;
  const tabDot = $("#tab-log-dot");
  if (tabDot) tabDot.dataset.state = st;
  const logDot = $("#log-dot");
  if (logDot) { logDot.dataset.state = st; $("#log-hint").textContent = label; }
}

function connectLive() {
  if (DEMO) { setFeedStatus("demo"); return; }
  const seen = new Set(); // SSE replays history on reconnect — dedupe it
  const open = () => {
    setFeedStatus("connecting");
    const es = new EventSource(api("/events"));
    es.onopen = () => setFeedStatus("live");
    es.onmessage = (m) => {
      try {
        const e = JSON.parse(m.data);
        const sig = `${e.at}|${e.kind}|${e.pk || ""}|${String(e.detail || "").slice(0, 80)}`;
        if (seen.has(sig)) return;
        seen.add(sig);
        if (seen.size > 5000) seen.clear();
        state.events.unshift(e);
        if (state.events.length > 1500) state.events.length = 1500;
        if (e.pk && e.detail && ["response", "running", "action", "gate", "applied", "error"].includes(e.kind)) {
          state.activity[e.pk] = { detail: String(e.detail), at: e.at, kind: e.kind };
          scheduleLive(e.pk);   // update the card's live line without a full reload
        }
        scheduleFeed();
        // Drawer open on this job? Stream the agent's work into it live, so
        // watching a run doesn't mean hitting refresh.
        if (e.pk && e.pk === state.openPk && !$("#drawer").hidden) scheduleAgentLog(e.pk);
        if (["discovered", "applied", "gate", "running", "error"].includes(e.kind)) scheduleReload();
      } catch { /* ignore malformed lines */ }
    };
    es.onerror = () => { es.close(); setFeedStatus("connecting"); setTimeout(open, 3000); };
  };
  open();
}

// Demo mode has no SSE — replay the sample timelines as a plausible feed.
function demoEvents(apps) {
  const kindOf = (l) =>
    /submitted/i.test(l) ? "applied"
    : /discovered/i.test(l) ? "discovered"
    : /gated|captcha/i.test(l) ? "gate"
    : /closed|error|fail/i.test(l) ? "error" : "running";
  const evs = [];
  for (const a of apps) {
    for (const t of a.timeline || []) {
      evs.push({ kind: kindOf(t.label), pk: a.pk, agent: "pipeline",
        detail: `${t.label} — ${a.title} @ ${a.company}`, at: t.at });
    }
  }
  return evs.sort((x, y) => new Date(y.at) - new Date(x.at));
}

// --- data ------------------------------------------------------------------
function applyStats(s) {
  state.stats = s || {};
  if (s && s.apply_mode) state.mode = s.apply_mode;
  if (s && typeof s.headless === "boolean") state.headless = s.headless;
  if (s) state.paused = !!s.paused;
  if (s && s.auto_min_score != null) state.autoMin = s.auto_min_score;
}

async function load() {
  if (DEMO) {
    const d = await import("./demo-data.js");
    state.apps = d.applications;
    const counts = d.stats.counts_by_status || {};
    // Demo /stats lacks the live-only keys — derive them so the deck renders.
    applyStats({ ...d.stats, apply_mode: "gated", discovering: false,
      processing: false, found_waiting: counts.found || 0 });
    state.paused = d.paused;
    if (!state.events.length) state.events = demoEvents(d.applications);
  } else {
    const [apps, stats] = await Promise.all([
      fetch(api("/applications"), { headers: auth.header() }).then((r) => r.json()),
      fetch(api("/stats"), { headers: auth.header() }).then((r) => r.json()),
    ]);
    // Never render internal bookkeeping rows (meta# watermarks) as jobs.
    state.apps = (apps.items || []).filter((r) => !String(r.pk || "").startsWith("meta#"));
    applyStats(stats);
  }
  renderAll();
}

// The watchlist for the discovery picker. Demo has no /companies — fall back
// to the demo watchlist + the companies present in the sample rows.
async function loadCompanies() {
  if (DEMO) {
    const d = await import("./demo-data.js");
    state.companies = [...new Set([
      ...(d.companies || []).map((c) => c.name),
      ...(d.applications || []).map((a) => a.company),
    ])];
  } else {
    try {
      const r = await fetch(api("/companies"), { headers: auth.header() }).then((r) => r.json());
      state.companies = Array.isArray(r.companies) ? r.companies : [];
      state.skipped = new Set((r.skipped || []).map((s) => String(s).toLowerCase()));
      state.filters = r.filters || {};
    } catch { /* backend away — fall back below */ }
    if (!state.companies.length) {
      state.companies = [...new Set(state.apps.map((a) => a.company).filter(Boolean))];
    }
  }
  state.picked = new Set([...state.picked].filter((c) => state.companies.includes(c)));
  renderPicker();
  renderSkipPicker();
  renderDiscoverLabel();
}

async function loadApps() {
  if (DEMO) return;
  try {
    const a = await fetch(api("/applications"), { headers: auth.header() }).then((r) => r.json());
    state.apps = (a.items || []).filter((r) => !String(r.pk || "").startsWith("meta#"));
    renderTabs(); renderPane(); renderFooter(); scheduleFeed();
  } catch { /* transient — next poll wins */ }
}

// Poll /stats every ~3s: drives the running-state of the two buttons and the
// vitals. When a run finishes, refresh the board once.
async function pollStats() {
  if (DEMO) return;
  try {
    const s = await fetch(api("/stats"), { headers: auth.header() }).then((r) => r.json());
    const was = !!(state.stats.discovering || state.stats.processing);
    applyStats(s);
    renderDeck();
    const is = !!(s.discovering || s.processing);
    if (was && !is) { loadApps(); toast("Run finished — board updated."); }
  } catch { /* backend briefly away — keep last known state */ }
}

let _reloadTimer = null;
function scheduleReload() {
  if (DEMO) return;
  clearTimeout(_reloadTimer);
  _reloadTimer = setTimeout(() => { loadApps(); }, 800);
}

async function post(path, body) {
  try {
    const r = await fetch(api(path), {
      method: "POST",
      headers: { ...auth.header(), "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    return await r.json().catch(() => ({}));
  } catch {
    toast("Request failed — is the backend running?");
    return null;
  }
}
function demoGuard() {
  if (DEMO) { toast("Demo mode — run the local backend to use actions."); return true; }
  return false;
}

// --- primary actions -------------------------------------------------------
async function runDiscover() {
  if (demoGuard() || state.stats.discovering) return;
  const scope = discoverScope();
  state.stats.discovering = true;   // optimistic; poll confirms
  renderDeck();
  const d = await post("/actions/discover", { companies: scope });
  if (d && d.status === "already_running") toast("Discovery is already running.");
  else if (d && d.ok) toast(scope.length
    ? `Discovery started — scanning ${scope.length <= 3 ? scope.join(", ") : `${scope.length} companies`}.`
    : "Discovery started — scanning the whole watchlist.");
  else if (!d) { state.stats.discovering = false; renderDeck(); }
  pollStats();
}
async function runCompany(name, careersUrl) {
  if (demoGuard()) return;
  const d = await post("/actions/run-company", { name, careers_url: careersUrl || "" });
  if (d && d.status === "already_running") toast("A run is already going — wait for it to finish.");
  else if (d && d.ok) toast(`▶ ${name}: discover → score → tailor started. Tailored jobs will land on the board.`);
  else if (d) toast(d.error || "Could not start the run.");
  pollStats();
}

async function runProcess() {
  if (demoGuard() || state.stats.processing) return;
  const n = state.stats.found_waiting ?? 0;
  const scope = discoverScope();  // same picked companies as Discover
  state.stats.processing = true;    // optimistic; poll confirms
  renderDeck();
  const d = await post("/actions/process", { companies: scope });
  if (d && d.status === "already_running") toast("A processing run is already going.");
  else if (d && d.ok) toast(scope.length
    ? `Processing ${scope.length <= 3 ? scope.join(", ") : `${scope.length} companies`} only — score · tailor · apply.`
    : n ? `Processing ${n} waiting job${n === 1 ? "" : "s"} — score · tailor · apply.`
        : "Processing run started.");
  else if (!d) { state.stats.processing = false; renderDeck(); }
  pollStats();
}

function paneAction(act, pk) {
  if (demoGuard()) return;
  if (act === "answer") {
    const t = $(`#pane textarea[data-answer-for="${CSS.escape(pk)}"]`);
    const answer = (t?.value || "").trim() || "approved";
    post(`/actions/resume/${encodeURIComponent(pk)}`, { answer });
    toast("Sent — pipeline resuming.");
  } else if (act === "skip") {
    if (!confirm("Skip this job? It moves to closed.")) return;
    post(`/actions/skip/${encodeURIComponent(pk)}`);
    toast("Skipped.");
  } else if (act === "retry") {
    post(`/actions/retry/${encodeURIComponent(pk)}`);
    toast("Retrying — re-running the pipeline for this job.");
  } else if (act === "mark-applied") {
    post(`/actions/mark-applied/${encodeURIComponent(pk)}`);
    toast("Marked applied — won't resubmit.");
  }
  scheduleReload();
}

function approveAll() {
  if (demoGuard()) return;
  const n = state.apps.filter(
    (a) => a.status === "needs_human" && a.gate_reason === "approval").length;
  if (!confirm(`Approve and apply to ${n} job${n === 1 ? "" : "s"}? `
    + `They run a few at a time — finish any CAPTCHA windows as they open.`)) return;
  post("/actions/approve-all", { company: "__all__" });
  toast(`Approving ${n} — applications are running.`);
  scheduleReload();
}

async function togglePause() {
  if (demoGuard()) return;
  state.paused = !state.paused;
  renderDeck();
  await post("/actions/pause", { paused: state.paused });
  toast(state.paused ? "Automation paused — nothing will run on its own." : "Automation resumed.");
}
async function toggleHeadless() {
  if (demoGuard()) return;
  state.headless = !state.headless;
  renderMenuState();
  await post("/actions/browser-mode", { headless: state.headless });
  toast(state.headless
    ? "Headless — applies run with NO visible Chrome. (A CAPTCHA/handoff shows on the board, not a window.)"
    : "Visible — you'll see the Chrome windows while it applies.");
}

const MODES = ["gated", "auto", "assisted"];
async function toggleMode() {
  if (demoGuard()) return;
  state.mode = MODES[(MODES.indexOf(state.mode) + 1) % MODES.length];
  renderDeck();
  await post("/actions/mode", { mode: state.mode });
  toast({
    auto: `Auto ☾ — applies jobs scoring ≥ ${state.autoMin} by itself.`,
    gated: "Gated — every application waits for your approval.",
    assisted: "Assisted — jobs stop at Tailored; finish them in your own browser "
            + "with the extension. Slower, but employers see a real session.",
  }[state.mode]);
}
async function resetPipeline() {
  if (demoGuard()) return;
  if (!confirm("Reset the pipeline?\n\nClears all tracked jobs, the queue, live logs and "
    + "stored résumés/screenshots. Your facts and saved logins are kept.")) return;
  await post("/actions/reset");
  state.apps = [];
  state.events = [];
  toast("Pipeline reset.");
  load().catch(() => {});
}

// --- detail drawer ---------------------------------------------------------
function openDrawer(pk) {
  const r = state.apps.find((a) => a.pk === pk);
  if (!r) return;
  const links = [["JD", r.jd_url], ["Resume PDF", r.resume_url], ["Screenshot", r.screenshot_url]]
    .map(([l, h]) => h
      ? `<a class="lk" href="${esc(h)}" target="_blank" rel="noopener">${l}</a>`
      : `<span class="lk off">${l}</span>`).join("");

  const fields = (r.fields || []).map((f) => {
    const val = f.type === "checkbox"
      ? `<span class="check ${f.value ? "" : "off"}">✓</span>`
      : esc(f.value);
    const conf = f.confidence === "low"
      ? '<span class="conf low">gated</span>' : '<span class="conf">auto</span>';
    return `<div class="field"><div class="field-q">${esc(f.label)}${conf}</div>
      <div class="field-v">${val}</div></div>`;
  }).join("") || '<div class="empty" style="padding:20px">no captured fields</div>';

  const tl = (r.timeline || []).map((t) =>
    `<div class="tl"><div class="tl-dot ${t.done ? "done" : ""}"></div>
      <div><div class="tl-label">${esc(t.label)}</div><div class="tl-time">${when(t.at)}</div></div></div>`).join("");

  const gate = (r.status === "needs_human" || canApply(r)) ? `
    <div class="section gate-box">
      <div class="section-t">⏸ ${esc(gateLabel(r.gate_reason))}</div>
      <div class="gate-q md">${md(r.gate_question || defaultGateText(r))}</div>
      <textarea id="gate-answer" class="gate-input" rows="2"
        placeholder="Type your answer, or leave blank to approve &amp; continue…"></textarea>
      <div class="drawer-actions">
        <button class="btn btn-primary" data-act="answer" data-pk="${esc(r.pk)}">Approve &amp; continue</button>
        <button class="btn btn-ghost" data-act="mark-applied" data-pk="${esc(r.pk)}"
          title="It already went through (you got the email / saw the confirmation) — mark applied, don't resubmit">✓ Already applied</button>
        <button class="btn btn-ghost" data-act="skip" data-pk="${esc(r.pk)}">Skip</button>
      </div>
    </div>` : "";

  const canRetry = RETRYABLE.includes(r.status);
  const closed = r.closed_reason ? `
    <div class="section closed-box">
      <div class="section-t">why it ${canRetry ? "failed" : "closed"}</div>
      <div class="closed-why">${esc(r.closed_reason)}</div>
      ${canRetry ? `<div class="drawer-actions">
        <button class="btn btn-primary" data-act="retry" data-pk="${esc(r.pk)}">Retry</button>
      </div>` : ""}
    </div>` : "";

  // What the browser last saw — always visible when we have a screenshot, so
  // failures (and confirmations) are never a mystery.
  const shotSec = imgOk(r.screenshot_url) ? `
    <div class="section"><div class="section-t">what the browser last saw</div>
      <a class="drawer-shot" href="${esc(r.screenshot_url)}" target="_blank" rel="noopener"
        title="Click to open full size">
        <img src="${esc(r.screenshot_url)}" alt="last screenshot" loading="lazy"></a></div>` : "";

  // A job still moving through the pipeline can be pulled out.
  const active = ["found", "tailored", "submitting"].includes(r.status);
  const cancel = active ? `
    <div class="drawer-actions" style="margin-top:0;margin-bottom:22px">
      <button class="btn btn-danger" data-act="cancel" data-pk="${esc(r.pk)}">
        ✕ ${r.status === "submitting" ? "Cancel this application" : "Skip this job"}</button>
    </div>` : "";

  const diff = r.has_diff ? `
    <div class="section"><div class="section-t">what the tailor changed</div>
      <div id="diff-body" class="diff-body"><span class="muted">loading…</span></div></div>` : "";

  const metaRows = [
    ["resume version", `<span class="mono">${esc(r.resume_version || "—")}</span>`],
    ["match score", r.match_score != null ? `${r.match_score} / 10` : "—"],
    r.ats ? ["ATS", esc(r.ats)] : null,
    r.mode ? ["mode", esc(r.mode)] : null,
    ["confirmation", `<span class="mono">${esc(r.confirmation_id || "—")}</span>`],
    r.submitted_at ? ["submitted", when(r.submitted_at)] : null,
    ["job id", `<span class="mono">${esc(r.pk)}</span>`],
  ].filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

  $("#drawer-body").innerHTML = `
    ${gate}
    ${closed}
    <div class="section"><div class="section-t">status</div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        ${tagHtml(r.status)}${scoreHtml(r.match_score)}
        ${r.gate_reason && r.status === "needs_human"
          ? `<span class="mono" style="font-size:12px;color:var(--faint)">${esc(gateLabel(r.gate_reason))}</span>` : ""}
      </div></div>
    ${cancel}
    ${shotSec}
    ${diff}

    <div class="section"><div class="section-t">metadata used to apply</div>
      <dl class="meta-grid">${metaRows}</dl>
      ${r.resume_version ? `<button class="btn" style="margin-top:14px" data-resume="${esc(r.pk)}">
        View résumé · ${esc(r.resume_version)}</button>` : ""}</div>

    <div class="section"><div class="section-t">form answers · ${(r.fields || []).length} fields</div>
      <div class="fields">${fields}</div></div>

    ${r.jd_excerpt ? `<div class="section"><div class="section-t">JD snapshot</div>
      <div style="color:var(--muted);font-size:13px;border:1px solid var(--border);
        border-radius:var(--r-md);padding:13px 15px;max-height:160px;overflow:auto">${esc(r.jd_excerpt)}</div></div>` : ""}

    ${tl ? `<div class="section"><div class="section-t">timeline</div><div class="timeline">${tl}</div></div>` : ""}

    <div class="section"><div class="section-t">everything the agent did
      <button class="cp-lk agentlog-re" data-agentlog="${esc(r.pk)}" type="button">refresh</button></div>
      <div id="agentlog" class="agentlog"><span class="muted">loading…</span></div></div>

    <div class="section"><div class="section-t">artifacts</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">${links}</div></div>
  `;
  $("#drawer-title").textContent = r.company;
  // The posting URL, always one click away — not buried in the artifacts row.
  const loc = r.location ? ` · <span class="drawer-loc">${esc(r.location)}</span>` : "";
  $("#drawer-sub").innerHTML = r.jd_url
    ? `${esc(r.title)}${loc} · <a class="drawer-jd" href="${esc(r.jd_url)}" target="_blank"
         rel="noopener" title="${esc(r.jd_url)}">open job posting ↗</a>`
    : esc(r.title) + loc;
  $("#scrim").hidden = false;
  $("#drawer").hidden = false;
  requestAnimationFrame(() => {
    $("#scrim").classList.add("show");
    $("#drawer").classList.add("show");
  });
  state.openPk = r.pk;
  if (r.has_diff) loadDiff(r.pk);
  loadAgentLog(r.pk);
}

// Coalesce the live stream — a busy agent emits several events per second and
// each would otherwise be its own round trip.
let agentLogTimer = null;
function scheduleAgentLog(pk) {
  if (agentLogTimer) return;
  agentLogTimer = setTimeout(() => { agentLogTimer = null; loadAgentLog(pk); }, 700);
}

// Every event the agent emitted for ONE job — each step, tool call and result,
// in order. The card shows a one-line summary; when something stalls or gets
// refused, this is the whole story behind it.
function loadAgentLog(pk) {
  fetch(api(`/job-log/${encodeURIComponent(pk)}`), { headers: auth.header() })
    .then((r) => r.json())
    .then((d) => {
      const el = $("#agentlog");
      if (!el) return;
      // Was the reader parked at the bottom? Then keep following the transcript.
      const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 40 || !el.dataset.seeded;
      el.dataset.seeded = "1";
      const evs = d.events || [];
      if (!evs.length) {
        el.innerHTML = '<span class="muted">nothing logged for this job yet</span>';
        return;
      }
      el.innerHTML = evs.map((e) => {
        const body = [e.detail, e.input, e.output].filter(Boolean)
          .map((x) => unraw(typeof x === "string" ? x : JSON.stringify(x))).join("\n");
        return `<div class="al-e">
          <div class="al-h"><span class="al-k" data-kind="${esc(e.kind || "")}">${esc(e.kind || "·")}</span>
            ${e.agent ? `<span class="al-a">${esc(e.agent)}</span>` : ""}
            <span class="al-t">${when(e.at)}</span></div>
          ${body ? `<pre class="al-b">${esc(body)}</pre>` : ""}</div>`;
      }).join("");
      if (stick) el.scrollTop = el.scrollHeight;  // follow the tail, but only if already there
    })
    .catch(() => {
      const el = $("#agentlog");
      if (el) el.innerHTML = '<span class="muted">couldn\'t load the log</span>';
    });
}

function loadDiff(pk) {
  fetch(api(`/actions/diff/${encodeURIComponent(pk)}`), { headers: auth.header() })
    .then((r) => r.json())
    .then((d) => {
      const el = $("#diff-body");
      if (!el) return;
      const changes = (d.changes || []).filter((c) => c.before?.length || c.after?.length);
      if (!changes.length) {
        el.innerHTML = `<span class="muted">${esc(d.note || "no bullet changes")}</span>`;
        return;
      }
      el.innerHTML = changes.map((c) => `
        <div class="diff-item">
          ${(c.before || []).map((b) => `<div class="diff-line del">− ${esc(b)}</div>`).join("")}
          ${(c.after || []).map((a) => `<div class="diff-line add">+ ${esc(a)}</div>`).join("")}
        </div>`).join("");
    })
    .catch(() => {
      const el = $("#diff-body");
      if (el) el.innerHTML = `<span class="muted">couldn't load diff</span>`;
    });
}
function closeDrawer() {
  state.openPk = "";
  $("#scrim").classList.remove("show");
  $("#drawer").classList.remove("show");
  setTimeout(() => { $("#scrim").hidden = true; $("#drawer").hidden = true; }, 260);
}

// --- résumé viewer ---------------------------------------------------------
function openResume(pk) {
  const r = state.apps.find((a) => a.pk === pk);
  if (!r) return;
  const real = !!r.resume_url && !DEMO;
  $("#rmodal-title").textContent = `${r.company} · ${r.resume_version || "résumé"}`;
  const dl = $("#rmodal-download");
  if (real) { dl.href = r.resume_url; dl.hidden = false; } else dl.hidden = true;
  $("#rmodal-body").innerHTML = real
    ? `<iframe src="${esc(r.resume_url)}#toolbar=1" title="résumé"></iframe>`
    : DEMO
      ? demoResume(r)
      : `<div class="resume"><p class="r-lead">No PDF stored for this application
          (résumé version ${esc(r.resume_version || "—")}). It may still be rendering —
          check the artifacts section, or retry the job.</p></div>`;
  $("#rmodal").hidden = false;
  requestAnimationFrame(() => $("#rmodal").classList.add("show"));
}
function closeResume() {
  $("#rmodal").classList.remove("show");
  setTimeout(() => ($("#rmodal").hidden = true), 200);
}
// A believable one-page résumé for demo mode, lightly tailored to the row.
function demoResume(r) {
  const focus = /payment/i.test(r.title) ? "payments & ledgers"
    : /infra|distributed|platform/i.test(r.title) ? "distributed systems & infrastructure"
    : /data/i.test(r.title) ? "data platforms" : "backend systems";
  const skills = ["Python", "Go", "AWS", "DynamoDB", "Kafka", "Postgres", "Kubernetes", "gRPC"];
  return `<div class="resume">
    <h1>Sayantan Bhowmik</h1>
    <div class="r-contact">Backend Engineer · sayantan.bhowmik94@gmail.com · github.com/sayantan</div>
    <p class="r-lead">Backend engineer focused on ${focus}. Tailored for
      <b>${esc(r.company)} — ${esc(r.title)}</b> (resume ${esc(r.resume_version)}).</p>
    <h2>Experience</h2>
    <div class="r-job"><span>Senior Backend Engineer · FinScale</span><span class="when">2022 — present</span></div>
    <ul>
      <li>Led the ${focus} workstream serving 40M+ requests/day at four nines.</li>
      <li>Designed idempotent, conditionally-written ledger primitives eliminating double-writes.</li>
      <li>Cut p99 latency 38% by resharding the hot path and adding read-through caching.</li>
    </ul>
    <div class="r-job"><span>Backend Engineer · Cloudwork</span><span class="when">2019 — 2022</span></div>
    <ul>
      <li>Built event-driven services (Kafka, gRPC) powering the billing pipeline.</li>
      <li>Owned the on-call rotation and halved alert noise via SLO-based alerting.</li>
    </ul>
    <h2>Skills</h2>
    <div>${skills.map((s) => `<span class="r-tag">${s}</span>`).join("")}</div>
    <h2>Education</h2>
    <div class="r-job"><span>B.Tech, Computer Science · IIT</span><span class="when">2015 — 2019</span></div>
  </div>`;
}

// --- toast -----------------------------------------------------------------
let _toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("show"));
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => (el.hidden = true), 220);
  }, 2800);
}

// --- wiring ----------------------------------------------------------------
function wire() {
  $("#btn-discover").addEventListener("click", runDiscover);
  $("#btn-process").addEventListener("click", runProcess);

  // company picker (for discovery)
  const picker = $("#copicker"), coBtn = $("#btn-companies");
  const closePicker = () => {
    if (picker.hidden) return;
    picker.hidden = true;
    coBtn.setAttribute("aria-expanded", "false");
    coBtn.classList.remove("open");
  };
  coBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const show = picker.hidden;
    picker.hidden = !show;
    coBtn.setAttribute("aria-expanded", String(show));
    coBtn.classList.toggle("open", show);
    if (show) { renderPicker(); $("#cp-search").focus(); }
  });
  picker.addEventListener("click", (e) => e.stopPropagation());
  $("#cp-search").addEventListener("input", (e) => {
    state.coQuery = e.target.value;
    renderPicker();
  });
  $("#cp-all").addEventListener("click", () => {
    state.picked = new Set(state.companies);
    renderPicker(); renderDiscoverLabel();
  });
  $("#cp-none").addEventListener("click", () => {
    state.picked.clear();
    renderPicker(); renderDiscoverLabel();
  });
  // Add a company to the watchlist from the picker; the finder resolves its
  // ATS on the first discovery. The new company starts picked so "add → run
  // on just this one" is two clicks.
  const addCompany = async () => {
    if (demoGuard()) return;
    const name = $("#cp-add-name").value.trim();
    if (!name) { toast("Give the company a name first."); return; }
    const d = await post("/actions/watchlist",
                         { name, careers_url: $("#cp-add-url").value.trim() });
    if (d && d.ok) {
      $("#cp-add-name").value = ""; $("#cp-add-url").value = "";
      await loadCompanies();
      state.picked.add(name);
      renderPicker(); renderDiscoverLabel();
      toast(`${name} added to the watchlist — and selected.`);
    } else if (d) toast(d.error || "Could not add the company.");
  };
  $("#cp-add-btn").addEventListener("click", addCompany);
  $("#cp-add-run").addEventListener("click", async () => {
    if (demoGuard()) return;
    const name = $("#cp-add-name").value.trim();
    if (!name) { toast("Give the company a name first."); return; }
    const url = $("#cp-add-url").value.trim();
    $("#cp-add-name").value = ""; $("#cp-add-url").value = "";
    await runCompany(name, url);   // endpoint adds it to the watchlist if new
    await loadCompanies();
  });
  const commitOneFilter = async (inp) => {
    const name = inp.dataset.filterCo, titles = inp.value.trim();
    const d = await post("/actions/company-filter", { name, titles });
    if (d && d.ok) {
      state.filters = d.filters || {};
      toast(titles
        ? `${name}: only titles with "${titles}"${d.reconciled ? ` — ${d.reconciled} re-sorted` : ""}.`
        : `${name}: title filter cleared.`);
      loadApps();
    }
  };
  $("#cp-foot").addEventListener("click", (e) => {
    const b = e.target.closest("[data-runone]");
    if (!b) return;
    const inp = $("#cp-one-filter");           // save the filter before running
    if (inp && inp.value.trim() !== (state.filters[b.dataset.runone.toLowerCase()] || []).join(", ")) {
      commitOneFilter(inp).then(() => runCompany(b.dataset.runone));
    } else runCompany(b.dataset.runone);
  });
  $("#cp-foot").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.id === "cp-one-filter") { e.preventDefault(); e.target.blur(); }
  });
  $("#cp-foot").addEventListener("focusout", (e) => {
    if (e.target.id === "cp-one-filter") commitOneFilter(e.target);
  });
  $("#cp-add-url").addEventListener("keydown", (e) => { if (e.key === "Enter") addCompany(); });
  $("#cp-add-name").addEventListener("keydown", (e) => { if (e.key === "Enter") addCompany(); });

  $("#cp-list").addEventListener("click", async (e) => {
    const b = e.target.closest(".cp-skip");
    if (!b || demoGuard()) return;
    const name = b.dataset.name, skip = b.dataset.skip === "1";
    const d = await post("/actions/skip-company", { name, skip });
    if (d && d.ok) {
      state.skipped = new Set((d.skipped || []).map((s) => String(s).toLowerCase()));
      renderPicker(); renderSkipPicker(); renderDiscoverLabel();
      toast(skip ? `${name} skipped — un-scoped runs pass over it.`
                 : `${name} is back in rotation.`);
    }
  });
  $("#cp-list").addEventListener("change", (e) => {
    const cb = e.target;
    if (!cb.matches('input[type="checkbox"]')) return;
    if (cb.checked) state.picked.add(cb.value); else state.picked.delete(cb.value);
    renderPickerState(); renderDiscoverLabel(); // list DOM untouched — scroll stays
  });

  $("#tabs").addEventListener("click", (e) => {
    const t = e.target.closest(".tab");
    if (!t) return;
    state.tab = t.dataset.tab;
    renderTabs();
    renderPane();
  });

  const rp = $("#rolepicker"), rpBtn = $("#btn-role");
  const closeRp = () => { if (!rp.hidden) { rp.hidden = true; rpBtn.setAttribute("aria-expanded", "false"); } };
  const runRole = async () => {
    if (demoGuard()) return;
    const url = $("#rp-url").value.trim();
    if (!url) { toast("Paste the job URL first."); return; }
    const d = await post("/actions/apply-role", { url });
    if (d && d.ok) {
      $("#rp-url").value = ""; closeRp();
      toast("▶ Tailoring your résumé to that role — it'll land in Tailored when ready.");
      pollStats();
    } else if (d) toast(d.error || "Couldn't start — check the URL.");
  };
  rpBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const show = rp.hidden;
    rp.hidden = !show;
    rpBtn.setAttribute("aria-expanded", String(show));
    if (show) $("#rp-url").focus();
  });
  rp.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", (e) => { if (!rp.hidden && !e.target.closest(".rolemgr")) closeRp(); });
  $("#rp-go").addEventListener("click", runRole);
  $("#rp-url").addEventListener("keydown", (e) => { if (e.key === "Enter") runRole(); });

  // --- job preferences ---
  // What counts as a match. Every stage re-reads preferences.yaml per job, so a
  // save here changes the very next job scored — no restart, no file editing.
  const pf = $("#prefpicker"), pfBtn = $("#btn-prefs");
  const closePf = () => {
    if (!pf.hidden) { pf.hidden = true; pfBtn.setAttribute("aria-expanded", "false"); }
  };
  const csv = (v) => (v || "").split(",").map((s) => s.trim()).filter(Boolean);
  const loadPrefs = async () => {
    try {
      const r = await fetch(api("/preferences"), { headers: auth.header() });
      const p = (await r.json()) || {};
      $("#pf-titles").value = (p.titles || []).join(", ");
      $("#pf-include").value = (p.include_keywords || []).join(", ");
      $("#pf-exclude").value = (p.exclude_keywords || []).join(", ");
      $("#pf-seniority").value = (p.seniority || []).join(", ");
      $("#pf-locations").value = (p.locations || []).join(", ");
      $("#pf-score").value = p.min_match_score ?? 7;
      $("#pf-topn").value = p.max_new_per_run ?? 5;
      $("#pf-notes").value = p.notes || "";
      $("#pf-remote").checked = !!p.remote_only;
      state.prefsGithub = p.github || "";
    } catch { toast("Couldn't load preferences."); }
  };
  const savePrefs = async () => {
    if (demoGuard()) return;
    const st = $("#pf-state");
    const d = await post("/preferences", {
      titles: csv($("#pf-titles").value),
      include_keywords: csv($("#pf-include").value),
      exclude_keywords: csv($("#pf-exclude").value),
      seniority: csv($("#pf-seniority").value),
      locations: csv($("#pf-locations").value),
      min_match_score: Number($("#pf-score").value || 7),
      max_new_per_run: Number($("#pf-topn").value || 0),
      notes: $("#pf-notes").value,
      remote_only: $("#pf-remote").checked,
      github: state.prefsGithub || "",
    });
    if (d && d.ok) {
      st.textContent = "Saved — applies to the next job scored.";
      st.className = "saved";
      toast("◎ Preferences saved.");
      setTimeout(() => { st.className = ""; st.textContent = "Applies to the next job scored."; }, 4000);
    } else {
      st.textContent = (d && d.error) || "Couldn't save.";
      st.className = "failed";
    }
  };
  pfBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const show = pf.hidden;
    pf.hidden = !show;
    pfBtn.setAttribute("aria-expanded", String(show));
    if (show) { loadPrefs(); $("#pf-titles").focus(); }
  });
  pf.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", (e) => {
    if (!pf.hidden && !e.target.closest(".prefmgr")) closePf();
  });
  $("#pf-save").addEventListener("click", savePrefs);

  const sp = $("#skippicker"), spBtn = $("#btn-skips");
  const closeSp = () => {
    if (sp.hidden) return;
    sp.hidden = true;
    spBtn.setAttribute("aria-expanded", "false");
  };
  spBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const show = sp.hidden;
    sp.hidden = !show;
    spBtn.setAttribute("aria-expanded", String(show));
    if (show) { renderSkipPicker(); $("#sp-search").focus(); }
  });
  sp.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", (e) => {
    if (!sp.hidden && !e.target.closest(".skipmgr")) closeSp();
  });
  $("#sp-search").addEventListener("input", (e) => {
    state.spQuery = e.target.value;
    renderSkipPicker();
  });
  $("#sp-list").addEventListener("change", async (e) => {
    const cb = e.target;
    if (!cb.matches('input[type="checkbox"]')) return;
    if (demoGuard()) { cb.checked = !cb.checked; return; }
    const name = cb.value, skip = cb.checked;
    const d = await post("/actions/skip-company", { name, skip });
    if (d && d.ok) {
      state.skipped = new Set((d.skipped || []).map((s) => String(s).toLowerCase()));
      renderSkipPicker(); renderPicker(); renderDiscoverLabel();
      toast(skip ? `${name} skipped — Discover + Process pass over it.`
                 : `${name} is back in rotation.`);
    } else cb.checked = !skip;
  });

  $("#llm-banner-x").addEventListener("click", () => {
    $("#llm-banner").hidden = true;
    if (!DEMO) post("/actions/clear-llm-error");
  });
  $("#co-filter").addEventListener("change", (e) => {
    state.coFilter = e.target.value;
    e.target.classList.toggle("on", !!state.coFilter);
    renderPane();
  });
  $("#search").addEventListener("input", (e) => {
    state.query = e.target.value;
    renderPane();
  });
  $("#refresh-btn").addEventListener("click", async () => {
    if (DEMO) { renderAll(); toast("Refreshed."); return; }
    await Promise.all([loadApps(), pollStats(), loadCompanies()]);
    toast("Refreshed.");
  });

  $("#pane").addEventListener("click", (e) => {
    if (e.target.closest("a")) return;                 // links behave as links
    if (e.target.closest("[data-clear-filters]")) {
      state.coFilter = ""; state.query = ""; state.logKind = "all";
      $("#co-filter").value = ""; $("#co-filter").classList.remove("on");
      $("#search").value = "";
      renderPane();
      return;
    }
    const more = e.target.closest(".fe-more");
    if (more) {
      const b = more.parentElement.querySelector(".fe-b");
      const open = b.classList.toggle("open");
      more.textContent = open ? "show less ▴" : "show all ▾";
      return;
    }
    const lk = e.target.closest("[data-logkind]");
    if (lk) { state.logKind = lk.dataset.logkind; renderPane(); return; }
    const chip = e.target.closest("[data-filter]");
    if (chip) { state.filter = chip.dataset.filter; renderPane(); return; }
    const res = e.target.closest("[data-resume]");
    if (res) { openResume(res.dataset.resume); return; }
    if (e.target.closest("[data-approve-all]")) { approveAll(); return; }
    const runAllBtn = e.target.closest("[data-run-all]");
    if (runAllBtn) { e.stopPropagation(); runProcess(); return; }
    const runNow = e.target.closest('[data-act="run-now"]');
    if (runNow) {
      e.stopPropagation();
      if (!demoGuard()) {
        post(`/actions/run-job/${encodeURIComponent(runNow.dataset.pk)}`);
        toast("Running — scoring + tailoring this job now.");
        pollStats();
      }
      return;
    }
    const act = e.target.closest("[data-act]");
    if (act) { paneAction(act.dataset.act, act.dataset.pk); return; }
    const open = e.target.closest("[data-open]");
    if (open) { openDrawer(open.dataset.open); return; }
    const row = e.target.closest("tr[data-pk]");
    if (row) openDrawer(row.dataset.pk);
  });
  $("#pane").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const el = e.target.closest("[data-open]");
    if (el && !e.target.closest("textarea")) openDrawer(el.dataset.open);
  });

  $("#feed").addEventListener("click", (e) => {
    const more = e.target.closest(".fe-more");
    if (more) {
      const b = more.parentElement.querySelector(".fe-b");
      const open = b.classList.toggle("open");
      more.textContent = open ? "show less ▴" : "show all ▾";
      return;
    }
    const co = e.target.closest("[data-open]");
    if (co) openDrawer(co.dataset.open);
  });

  $("#drawer").addEventListener("click", (e) => {
    const relog = e.target.closest("[data-agentlog]");
    if (relog) { loadAgentLog(relog.dataset.agentlog); return; }
    const resume = e.target.closest("[data-resume]");
    if (resume) { openResume(resume.dataset.resume); return; }
    const act = e.target.closest("[data-act]");
    if (!act) return;
    if (demoGuard()) return;
    const pk = act.dataset.pk;
    const kind = act.dataset.act;
    if (kind === "answer") {
      const answer = ($("#gate-answer")?.value || "").trim() || "approved";
      post(`/actions/resume/${encodeURIComponent(pk)}`, { answer });
      toast("Sent — pipeline resuming.");
    } else if (kind === "retry") {
      post(`/actions/retry/${encodeURIComponent(pk)}`);
      toast("Retrying — re-running the pipeline.");
    } else if (kind === "cancel") {
      const st = (state.apps.find((a) => a.pk === pk) || {}).status;
      if (!confirm(`${st === "submitting" ? "Cancel this in-progress application"
        : "Skip this job"}? It moves to closed.`)) return;
      post(`/actions/skip/${encodeURIComponent(pk)}`);
      toast(st === "submitting" ? "Cancelled — moved to closed." : "Skipped.");
    } else {
      post(`/actions/skip/${encodeURIComponent(pk)}`);
      toast("Skipped.");
    }
    closeDrawer();
    scheduleReload();
  });

  $("#scrim").addEventListener("click", closeDrawer);
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#rmodal-close").addEventListener("click", closeResume);
  $("#rmodal").addEventListener("click", (e) => { if (e.target.id === "rmodal") closeResume(); });

  // settings menu
  const menu = $("#menu"), menuBtn = $("#menu-btn");
  const closeMenu = () => { menu.hidden = true; menuBtn.setAttribute("aria-expanded", "false"); };
  menuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    menuBtn.setAttribute("aria-expanded", String(!menu.hidden));
  });
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !e.target.closest(".menu-wrap")) closeMenu();
    if (!picker.hidden && !e.target.closest(".disco")) closePicker();
  });
  $("#m-pause").addEventListener("click", () => { togglePause(); closeMenu(); });
  $("#m-mode").addEventListener("click", () => { toggleMode(); closeMenu(); });
  $("#m-headless").addEventListener("click", () => { toggleHeadless(); closeMenu(); });
  $("#m-theme").addEventListener("click", () => {
    const root = document.documentElement;
    root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
    localStorage.setItem("appliedin.theme", root.dataset.theme);
    renderMenuState();
  });
  $("#m-reset").addEventListener("click", () => { closeMenu(); resetPipeline(); });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#rmodal").hidden) closeResume();
    else if (!$("#drawer").hidden) closeDrawer();
    else if (!picker.hidden) closePicker();
    else closeMenu();
  });
}

// --- boot ------------------------------------------------------------------
async function boot() {
  const savedTheme = localStorage.getItem("appliedin.theme");
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;
  wire();
  const env = DEMO ? "demo" : (CONFIG.env || "local");
  $("#env-pill").textContent = env;
  $("#foot-env").textContent = env;
  try {
    await load();
  } catch (e) {
    console.error(e);
    toast("Could not load data — is the backend running?");
    renderAll();
  }
  loadCompanies().catch(() => {});
  if (!DEMO) {
    connectLive();                                      // live activity (SSE)
    setInterval(pollStats, 3000);                       // button/vitals state
    setInterval(() => loadApps().catch(() => {}), 30000); // slow safety refresh
  } else {
    setFeedStatus("demo");
  }
}

boot();
