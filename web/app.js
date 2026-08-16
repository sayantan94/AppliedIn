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
  tab: "pipeline",    // pipeline | apps | needs | stuck | activity | logs
  filter: "all",      // status chip on the Applications table
  logKind: "all",     // kind chip on the Logs view
  liveState: "off",   // SSE connection state (off | connecting | live | demo)
  query: "",          // free-text search
  coFilter: "",       // board filter: show one company only ("" = all)
  openPk: "",         // pk in the open detail drawer (streams its agent log)
  locFilter: "",      // Tailored lane: show one location tier only ("" = all)
  profiles: [],       // identities an application can go out under
  profileDefault: "", // the one used by any job that has not chosen
  rotation: [],       // companies whose applications get an alias each, per /rotation
  collapsed: null,    // folded location buckets (null = pick the default once)
  dayShut: null,      // folded tailoring-date groups in Ready to apply
  companies: [],      // watchlist names for the discovery picker
  picked: new Set(),  // companies picked for the next discovery ("" empty = all)
  skipped: new Set(), // lowercase names excluded from un-scoped Discover/Process
  filters: {},        // {company_lower: [title keyword, ...]} per-company title filters
  cprefs: {},         // {company_lower: {field: value}} per-company preference OVERRIDES
  prefs: {},          // the global job preferences, so overrides can show what they inherit
  detailCo: null,     // company open in the picker's preference pane
  activity: {},       // pk -> {detail, at} — the live step, shown on active cards
  coQuery: "",        // search inside the company picker
  mode: "gated",
  headless: false,
  paused: false,
  autoMin: 8,
  queue: null,
  verifying: [],     // sessions waiting on a one-time code        // /apply-queue snapshot: who runs, who waits, the limit
  dead: [],           // /apply-queue/dead: jobs that used every attempt
  secOpen: { closed: false }, // pipeline stack: which foldable sections are open
  openCos: new Set(), // pipeline stack: companies expanded inside Found
  openQCos: new Set(), // pipeline stack: companies expanded inside Queued to apply
  qPicked: new Set(),  // queued jobs ticked for a bulk Skip / Remove
  page: {},           // pipeline stack: rows revealed per list key
  heat: null,         // /activity payload for the heatmap ({days, totals})
  fresh: null,        // /fresh payload, fetched once at the widest window
  freshHours: 48,     // the Fresh tab's selected window, in hours
  passed: null,
  scanLog: null,        // /scan-log: what each company produced this run
  passedOpen: new Set(), // companies unfolded in the passed over trail          // /passed-over: what recent scans rejected, and why
  freshPicked: new Set(), // companies picked for a scan run FROM the Fresh tab.
                      // The tab's own selection: state.picked belongs to the
                      // Discover picker and is never touched from here.
  freshCoQuery: "",   // search inside the Fresh tab's company list
  freshStatus: "",    // the Fresh tab's status facet ("" = any status)
  freshNote: "",      // a refused scan explains itself here, next to the button
  scanHours: 0,       // Discover's run window in hours (0 = any posting age).
                      // Run-scoped: bounds the next scan only, never saved prefs.
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
  captcha: "CAPTCHA, finish it manually",
  no_account: "account/login needed",
  low_confidence: "needs review",
};
const gateLabel = (r) => GATE_LABEL[r] || String(r || "review").replace(/_/g, " ");
function defaultGateText(r) {
  if (r.gate_reason === "no_account") return "Automatic signup was blocked. Approve once the account exists.";
  if (r.gate_reason === "captcha") return "A CAPTCHA blocked the bot. Apply manually, then approve to mark it done.";
  return "Paused for your approval. Approve to continue.";
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

// Discover's run window — how far back a scan looks, by the employer's publish
// date. Deliberately run-scoped: it bounds the NEXT scan only and never touches
// a company's saved preferences. 0 = no age limit, the original behaviour.
const SCAN_WINDOWS = [[0, "Any age"], [24, "24 hours"], [48, "48 hours"], [168, "7 days"]];
const scanWindowText = (h) => (h === 168 ? "the last 7 days" : `the last ${h} hours`);
const scanTag = (h) => (h === 168 ? "7d" : `${h}h`);

// Keep the static chips in the picker honest about which window is chosen.
function renderScanWindow() {
  $$("#cp-window [data-scan-w]").forEach((b) => {
    b.setAttribute("aria-selected",
      String(Number(b.dataset.scanW) === (Number(state.scanHours) || 0)));
  });
}

function renderDiscoverLabel() {
  // ONE company selection scopes BOTH actions: Discover scans just the picked
  // companies, and Process runs the pipeline on just their discovered jobs.
  const el = $("#discover-label");
  // A windowed scan says so on the button itself — the press is bounded, and
  // that should be readable before pressing, not only in the picker.
  const wtag = state.scanHours ? ` · ${scanTag(state.scanHours)}` : "";
  // No "Discovering…" takeover: scans are per company, the button stays
  // pressable while one runs, and the scanning-now strip below the deck is
  // what says which crawls are in flight. A label that reads as a status
  // would claim the button is busy when it is not.
  el.textContent = pickedAll()
    ? `Discover · All${wtag}`
    : `Discover · ${state.picked.size} selected${wtag}`;
  const pl = $("#process-label");
  if (state.stats.processing) pl.textContent = "Processing…";
  else pl.textContent = pickedAll()
    ? "Process applications"
    : `Process · ${state.picked.size} selected`;
}

/* The company picker for a pasted role. A dropdown of what is already tracked,
   because the name is a KEY — the queue, the per-company preferences and the board
   filter all match on it, so "Waymo" typed slightly differently is a second
   company that inherits none of the first one's settings. Free text stays
   available for a genuinely new employer, behind an explicit choice. */
function renderCompanyOptions() {
  const sel = $("#rp-url-company");
  if (!sel) return;
  const keep = sel.value;
  const names = (state.companies || []).map((c) => (c && c.name) || c).filter(Boolean);
  sel.innerHTML = `<option value="">Guess it from the URL</option>`
    + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("")
    + `<option value="__new__">＋ Add a new company…</option>`;
  if (keep) sel.value = keep;
  toggleNewCompany();
}

function toggleNewCompany() {
  const sel = $("#rp-url-company"), box = $("#rp-url-newco");
  if (!sel || !box) return;
  const adding = sel.value === "__new__";
  box.hidden = !adding;
  if (adding) box.focus();
}

/* What the role should be filed under: the typed name when adding one, the
   selection otherwise, and empty to let the server derive it. */
function chosenCompany() {
  const sel = $("#rp-url-company"), box = $("#rp-url-newco");
  if (!sel) return "";
  if (sel.value === "__new__") return (box?.value || "").trim();
  return sel.value;
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
        // A row says only whether this company screens on its own rules or the
        // defaults. The rules themselves live in the detail pane, because they
        // are the same seven fields as Job preferences and will not fit here.
        const over = state.cprefs[c.toLowerCase()] || {};
        const nOver = Object.keys(over).length;
        const sel = state.detailCo === c;
        const pending = (state.pendingScan || {}).co === c;
        return `<div class="cp-item ${sk ? "skipped" : ""} ${sel ? "sel" : ""}">
          <label class="cp-name">
            <input type="checkbox" value="${esc(c)}" ${state.picked.has(c) ? "checked" : ""}>
            <span>${esc(c)}</span></label>
          ${pending ? `<span class="cp-pending" title="Its rules changed. It scans as soon as the current scan finishes.">scan waiting</span>` : ""}
          <button class="cp-roles ${nOver ? "custom" : ""}" type="button" data-detail="${esc(c)}"
            title="${nOver ? `${nOver} preference${nOver === 1 ? "" : "s"} set for ${esc(c)}; the rest use your defaults`
                           : `${esc(c)} uses your default job preferences`}"
            >${nOver ? `${nOver} custom` : "default"}</button>
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
  renderDetail();
}
// The per-company preference pane. Deliberately the SAME seven fields as the Job
// preferences panel: a company either screens the way you screen everywhere, or
// it differs on a field you can point at. Every input left blank INHERITS, and
// its placeholder shows the default it is inheriting — so "what does Google
// actually screen on" is answerable without opening two panels and comparing.
const CPREF_FIELDS = [
  { k: "titles",           label: "Target titles",  list: true },
  { k: "include_keywords", label: "Raises fit",     list: true },
  { k: "exclude_keywords", label: "Never a fit",    list: true },
  { k: "seniority",        label: "Seniority",      list: true },
  { k: "locations",        label: "Locations",      list: true },
  { k: "min_match_score",  label: "Score bar",      num: true },
  { k: "max_new_per_run",  label: "Top N / run",    num: true },
  { k: "remote_only",      label: "Remote only",    bool: true },
  { k: "notes",            label: "Hard rules",     area: true },
  { k: "profile_id",       label: "Apply as",       prof: true },
];

const cpAsText = (v, f) => {
  if (v === undefined || v === null) return "";
  if (f.list) return Array.isArray(v) ? v.join(", ") : String(v);
  if (f.bool) return v ? "yes" : "no";
  return String(v);
};

// The pane always shows ONE company, pre-filled with what it actually screens
// on: its own override where it has one, your default everywhere else. So every
// field is populated and editable, and changing any of them overrides just that
// field for just this company.
//
// The inheritance is kept by COMPARING on save rather than by leaving fields
// blank: a value equal to the default stores no override, so raising your global
// score bar later still moves every company that never disagreed with it.
function renderDetail() {
  const box = $("#cp-detail");
  if (!box) return;
  const co = state.detailCo;
  const dflt = state.prefs || {};
  if (!co) {
    box.innerHTML = `<div class="cp-dt-h"><div class="cp-dt-co">Company rules</div></div>
      <div class="cp-dt-pick">Pick a company to see and change what it screens on.</div>`;
    return;
  }
  const over = state.cprefs[co.toLowerCase()] || {};
  const n = Object.keys(over).length;
  // Whether this company rotates its address. Read once here: the "Apply as"
  // field needs it to show the right selection, and the footer needs it for the
  // one action that follows — which lives there rather than beside the field,
  // because the field is halfway down a scrolling list and the footer is pinned.
  const rotc = (state.rotation || []).find((r) => r.company === co.toLowerCase());

  const field = (f) => {
    const isOver = over[f.k] !== undefined;
    const value = cpAsText(isOver ? over[f.k] : dflt[f.k], f);   // pre-filled either way
    const tag = isOver
      ? `<em class="cp-dt-inh overridden">only here</em>`
      : `<em class="cp-dt-inh">shared</em>`;
    const attrs = `class="cp-search cp-dt-in" data-cpref="${f.k}" data-co="${esc(co)}"`;
    if (f.bool) {
      return `<label class="pf-f cp-dt-f"><span class="pf-l">${f.label}${tag}</span>
        <select ${attrs}>
          <option value="yes"${value === "yes" ? " selected" : ""}>yes</option>
          <option value="no"${value === "no" ? " selected" : ""}>no</option>
        </select></label>`;
    }
    if (f.prof) {
      // Which identity this company's applications go out under. "" is the
      // default profile, and is a real choice rather than an empty one, so it
      // gets a named option instead of a blank row.
      const dn = (state.profiles.find((x) => x.id === state.profileDefault) || {}).label
                 || "default profile";
      // Rotation is chosen HERE, in the same control as any other identity,
      // because "who does this company hear from" is one question with one
      // answer. Picking a rotating profile binds the company instead of storing
      // a per-company override — the address is decided per application.
      const opts = [`<option value=""${value === "" && !rotc ? " selected" : ""}>${esc(dn)}</option>`]
        .concat(state.profiles
          .filter((x) => x.id !== state.profileDefault && x.kind !== "rotating")
          .map((x) => `<option value="${esc(x.id)}"${value === x.id && !rotc ? " selected" : ""}
            >${esc(x.label || x.id)}${x.email ? ` · ${esc(x.email)}` : ""}</option>`))
        .concat(state.profiles.filter((x) => x.kind === "rotating")
          .map((x) => `<option value="rot:${esc(x.id)}"${rotc && rotc.profile === x.id ? " selected" : ""}
            >↻ ${esc(x.label || x.id)} — a new address every ${x.limit || 5}</option>`));
      return `<label class="pf-f cp-dt-f"><span class="pf-l">${f.label}${tag}</span>
        <select ${attrs}>${opts.join("")}</select></label>`;
    }
    if (f.area) {
      return `<label class="pf-f cp-dt-f"><span class="pf-l">${f.label}${tag}</span>
        <textarea ${attrs} rows="3">${esc(value)}</textarea></label>`;
    }
    return `<label class="pf-f cp-dt-f"><span class="pf-l">${f.label}${tag}</span>
      <input ${attrs} ${f.num ? 'type="number" min="0"' : 'type="text"'}
        value="${esc(value)}" autocomplete="off" spellcheck="false" /></label>`;
  };

  box.innerHTML = `
    <div class="cp-dt-h">
      <div class="cp-dt-co">${esc(co)}</div>
      <span class="cp-dt-badge ${n ? "custom" : ""}">${
        n ? `${n} only here` : "all shared"}</span>
    </div>
    <div class="cp-dt-body">${CPREF_FIELDS.map(field).join("")}</div>
    ${rotc ? `<div class="cp-dt-rotbar" title="Each application to ${esc(co)} goes out
      under its own address; a new one is minted when this one reaches the limit.">
      <span class="cp-dt-rotmark">↻</span>
      <span class="cp-dt-rotmail">${rotc.email
        ? esc(rotc.email) : "first address on the next application"}</span>
      ${rotc.email ? `<span class="cp-dt-rotn mono">${rotc.used}/${rotc.limit}</span>` : ""}
    </div>` : ""}
    <div class="cp-dt-foot">
      <span class="cp-dt-hint">${rotc ? "" : `Edits apply to ${esc(co)} only.`}</span>
      ${n ? `<button class="cp-lk cp-add-btn" id="cp-dt-reset" type="button"
        title="Drop ${esc(co)}'s own values and share yours again">Use shared</button>` : ""}
      ${rotc ? `<button class="cp-lk cp-add-btn cp-rot-go" id="cp-dt-rotgo" type="button"
        data-co="${esc(co)}"
        title="Point every un-sent ${esc(co)} job at the rotating address, tailor what has no résumé yet, and queue it all — no further approvals. Bounded by what the address has room for; anything gated on a real question is left alone.">Rotate &amp; approve all</button>` : ""}
      <button class="cp-lk cp-add-btn cp-add-go" id="cp-dt-scan" type="button"
        title="Scan ${esc(co)} now with these rules, score and tailor what it finds. Stops before applying.">Scan now</button>
    </div>`;
}

// State line + footer only — checkbox toggles call this so the list DOM (and
// its scroll position) stays put while you tick companies.
function renderPickerState() {
  const n = state.picked.size, total = state.companies.length;
  $("#cp-state").textContent = pickedAll() ? `all ${total}` : `${n} of ${total}`;
  const sk = state.skipped.size;
  const skNote = sk ? ` <span class="cp-skipnote">· ${sk} skipped</span>` : "";
  // One plain sentence saying what pressing Discover will now do: who gets
  // scanned, and how far back the scan looks. The window bounds Discover only;
  // Process still follows the same company selection.
  const hours = Number(state.scanHours) || 0;
  const who = pickedAll()
    ? `the <b>whole watchlist</b>${total ? ` (${total - sk} of ${total} companies)` : ""}`
    : `<b>${n} compan${n === 1 ? "y" : "ies"}</b> only`;
  const win = hours
    ? `postings from <b>${scanWindowText(hours)}</b>`
    : `postings of <b>any age</b>`;
  const line = `Discover scans ${who}, ${win}.${skNote}`
    + ` Process runs on the same companies.`;
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
  $("#cp-foot").innerHTML = line + single;
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
  // Two different problems share this banner and they need opposite reactions.
  // An orchestration outage degrades screening and stays broken until fixed. A
  // browser rate limit submits nothing, retries itself, and only needs the owner
  // if it persists. Telling them "screening degraded, will fail until fixed" for
  // the second one would send them hunting a fault that is already handling
  // itself.
  const isBrowser = (err.where || "") === "browser";
  $("#llm-banner-msg").textContent = isBrowser
    ? `${err.msg}`
    : `LLM failure in ${err.where || "the pipeline"}: ${err.msg} — screening degraded `
      + `to the keyword filter; scoring/tailoring/applying will fail until this is fixed.`;
  b.classList.toggle("soft", isBrowser);
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
  // Per company, not global. Scans claim one company at a time on the server,
  // so another company's crawl is no reason to kill this button: it goes dead
  // only when every company the press would cover is already being scanned.
  // (Process below keeps its global gate on purpose — the server runs one
  // process pass at a time, so there its flag and its reach agree.)
  const busyScan = new Set((s.scanning || []).map((c) => String(c).toLowerCase()));
  const discScope = pickedAll()
    ? state.companies.filter((c) => !state.skipped.has(String(c).toLowerCase()))
    : [...state.picked];
  disc.disabled = !!discScope.length && busyScan.size > 0
    && discScope.every((c) => busyScan.has(String(c).toLowerCase()));
  disc.classList.toggle("running", !!s.discovering);
  renderScanNow();
  const stopBtn = $("#btn-stop");
  if (stopBtn) stopBtn.hidden = !s.discovering;   // scan only
  const stopApply = $("#btn-stop-apply");
  if (stopApply) stopApply.hidden = !(s.applying > 0);   // from /stats, polled always
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

/* ─── Scan progress, in the deck ────────────────────────────────────────────
   A watchlist sweep is hours of sequential browser work, minutes per company.
   Three questions must be answerable from any tab without clicking: which
   company is being read RIGHT NOW (one chip, one clock — claims are taken for
   the whole batch up front, so the claim list says nothing about progress),
   how far through the batch the run is (a bar plus counts), and what each
   finished company produced (the tally below, where zero is stated, never
   blank). Each block rebuilds its DOM only when its content actually changes;
   between polls a 1s ticker advances the clock's text alone, so the strip
   never flickers on the 3 second poll. */
function scanElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}
// A completed duration: "48s", "4m 32s", "4h 32m".
function scanTook(secs) {
  const s = Math.max(0, Math.round(Number(secs) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}
// Assign innerHTML only when it changed. This is what keeps the deck calm on
// the poll: an unchanged strip is not rebuilt, and the results list keeps its
// scroll position while a run works through the watchlist.
function setHtmlOnce(host, html) {
  if (host.dataset.sig === html) return false;
  host.dataset.sig = html;
  host.innerHTML = html;
  return true;
}

// The clock between polls. The server's elapsed_s is the anchor (it survives
// reloads and pages opened mid run) and is re anchored on every poll; the
// ticker advances only the text of #sn-clock, never the strip around it.
let _snTicker = null;
let _snAnchor = null;   // {secs, at}: server side elapsed + when we heard it
function _snTickClock() {
  const el = $("#sn-clock");
  if (el && _snAnchor)
    el.textContent = scanElapsed(_snAnchor.secs * 1000 + (Date.now() - _snAnchor.at));
}
function snClock(on) {
  if (on && !_snTicker) _snTicker = setInterval(_snTickClock, 1000);
  else if (!on && _snTicker) { clearInterval(_snTicker); _snTicker = null; }
}

function renderScanNow() {
  const host = $("#scan-now");
  if (!host) return;
  const names = state.stats.scanning || [];
  if (!names.length) {
    snClock(false); _snAnchor = null;
    if (renderScanFinished()) return;
    host.hidden = true; host.innerHTML = ""; delete host.dataset.sig;
    return;
  }

  // ONE chip for the company actually being read, not one per claimed company.
  // Claims are taken for the whole batch up front, so a 47 company sweep used
  // to render 47 chips all showing the same clock, which said nothing about
  // progress and made a working run look identical to a hung one. Crawls are
  // sequential: exactly one company is ever being read.
  const disp = new Map(state.companies.map((c) => [String(c).toLowerCase(), c]));
  // scan_active arrives from a daemon new enough to report it. An older daemon
  // sends only the claim list, and rendering 47 chips from that is the very
  // thing this replaced, so fall back to a count rather than a wall of names.
  const a = state.stats.scan_active || {};
  const legacy = !("scan_active" in state.stats);
  const active = a.company ? (disp.get(String(a.company).toLowerCase()) || a.company) : "";
  const total = Number(a.total) || names.length;
  const done = Math.max(0, Math.min(total, Number(a.done) || 0));
  const left = Math.max(0, total - done);

  const chip = active
    ? `<span class="sn-chip"><span class="sn-dot"></span>${esc(active)}`
      + `<span id="sn-clock" class="sn-t mono" aria-live="off"></span></span>`
    : legacy
      ? `<span class="sn-chip"><span class="sn-dot"></span>${names.length}
           compan${names.length === 1 ? "y" : "ies"} queued for this run</span>`
      : `<span class="sn-chip"><span class="sn-dot"></span>starting…</span>`;

  // The bar answers "how far through". A four hour sweep needs visible motion,
  // not only a fraction. Only for a real batch: one company has no arc.
  const bar = !legacy && total > 1
    ? `<span class="sn-track" role="progressbar" aria-valuemin="0"`
      + ` aria-valuemax="${total}" aria-valuenow="${done}" aria-label="scan progress">`
      + `<span id="sn-fill" class="sn-fill"></span></span>`
      + `<span class="sn-prog">${done} done, ${left} to go</span>`
    : "";

  setHtmlOnce(host, `<span class="sn-label">Scanning</span>${chip}${bar}`);
  host.hidden = false;
  // The moving parts are poked in place, so the strip itself never redraws.
  const fill = $("#sn-fill");
  if (fill) fill.style.width = (total ? (done / total) * 100 : 0) + "%";
  _snAnchor = { secs: Number(a.elapsed_s) || 0, at: Date.now() };
  _snTickClock();
  snClock(!!active);
}

/* The run tally: every finished company and what it produced, newest first,
   kept while a run is live and through the receipt window after it ends.
   Zero is a RESULT and is written out: "nothing new" from a finished company
   is a different fact from a company the sweep has not reached. The list caps
   its own height and scrolls, so 47 finished companies never push the board
   down the page. */
function renderScanResults() {
  const host = $("#scan-results");
  if (!host) return;
  const log = state.scanLog || {};
  const rows = log.companies || [];
  const scanning = (state.stats.scanning || []).length;
  if (!rows.length || (!scanning && !_scanFinishedAt)) {
    host.hidden = true; host.innerHTML = ""; delete host.dataset.sig;
    return;
  }
  const total = (log.run && log.run.total) || rows.length;
  const gained = rows.reduce((n, c) => n + (Number(c.enqueued) || 0), 0);
  // The header answers "how far along, and is it worth watching" — both of which
  // are only questions WHILE the run is going. Once it ends the strip above says
  // the same two numbers ("2 companies in 19s", "3 new jobs queued"), and the
  // list below breaks them down per company, so keeping it meant one scan of two
  // companies reported itself three times and "2 of 2" appeared as news.
  const head = scanning
    ? `<div class="sr-head">Scanning`
      + ` <span class="sr-n mono">${rows.length} of ${total}</span>`
      + `<span class="sr-sum${gained ? "" : " sr-sum-none"}">`
      + (gained ? `${gained} new job${gained === 1 ? "" : "s"} so far` : "nothing new yet")
      + `</span></div>`
    : "";
  const html = head
    + `<ol class="sr-list">` + rows.map((c) => {
        const got = Number(c.enqueued) > 0
          ? `<span class="sr-got">${c.enqueued} new</span>`
          : `<span class="sr-none">${c.note ? esc(c.note) : "nothing new"}</span>`;
        return `<li class="sr-row">`
          + `<span class="sr-co" title="${esc(c.company)}">${esc(c.company)}</span>`
          + got
          + `<span class="sr-t mono">${scanTook(c.seconds)}</span></li>`;
      }).join("") + `</ol>`;
  setHtmlOnce(host, html);
  host.hidden = false;
}

/* A finished run has to SAY so. A four hour sweep whose indicator silently
   vanishes cannot be told apart from a dead daemon or a stale tab, so the
   strip holds a receipt for ten minutes: how many companies, how long the run
   took, what it yielded, and how long ago it ended. */
let _scanFinishedAt = 0;
let _scanFinishedN = 0;

function noteScanFinished(companyCount) {
  _scanFinishedAt = Date.now();
  _scanFinishedN = companyCount || 0;
}

// A reload must not eat the receipt. The server's scan log outlives the page,
// so on boot a run that ended inside the receipt window is adopted as finished
// and the strip says so, exactly as if the tab had watched it end.
function adoptFinishedRun() {
  if ((state.stats.scanning || []).length || _scanFinishedAt) return;
  const rows = (state.scanLog || {}).companies || [];
  if (!rows.length) return;
  const end = Math.max(...rows.map((c) => (Number(c.at) || 0) * 1000));
  if (end && Date.now() - end < 10 * 60000) {
    _scanFinishedAt = end;
    _scanFinishedN = rows.length;
  }
}

function renderScanFinished() {
  const host = $("#scan-now");
  if (!host || !_scanFinishedAt) return false;
  const mins = Math.floor((Date.now() - _scanFinishedAt) / 60000);
  if (mins >= 10) { _scanFinishedAt = 0; return false; }   // said its piece
  const when = mins < 1 ? "just now" : `${mins} min ago`;

  const log = state.scanLog || {};
  const rows = log.companies || [];
  const n = rows.length || _scanFinishedN;
  const t0 = Number((log.run || {}).at) || 0;
  const end = rows.length ? Math.max(...rows.map((c) => Number(c.at) || 0)) : 0;
  const took = t0 && end > t0 ? ` in ${scanTook(end - t0)}` : "";
  const gained = rows.reduce((s, c) => s + (Number(c.enqueued) || 0), 0);
  const produced = rows.length
    ? (gained
        ? `${gained} new job${gained === 1 ? "" : "s"} queued, `
        : "nothing new anywhere, ")
    : "";

  setHtmlOnce(host, `<span class="sn-label sn-done">Scan finished</span>`
    + `<span class="sn-chip sn-chip-done"><span class="sn-ok">✓</span>`
    + (n ? `${n} compan${n === 1 ? "y" : "ies"}${took}` : `run complete`)
    + `</span>`
    + `<span class="sn-prog">${produced}${when}</span>`);
  host.hidden = false;
  return true;
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

/* The pipeline stack. Rank on screen equals rank in the owner's head:
   1 gates blocking an application, 2 tailored work awaiting a go, 3 live
   submits, 4 fresh confirmations, 5 the raw backlog, 6 closed work.
   Sections replace the old eight equal lanes, whose columns treated five
   blocking questions and 575 raw finds as peers. */
/* One classifier, one section per row. Every row lands somewhere, so a status
   the backend invents tomorrow falls to Closed instead of off the board. */
/* pks sitting in the apply queue. A queued job keeps its `tailored` status (the
   queue, not the row, is what holds the order), so without this it renders in
   "Ready to apply" looking exactly like a job nobody has approved — you press
   Apply, the card does not move, and it reads as though the click was lost. */
function queuedPks() {
  const q = state.queue || {};
  return new Set((q.pending || []).map((it) => it.pk));
}

function sectionOf(r, queued) {
  if (r.status === "needs_human") {
    if (queued && queued.has(r.pk)) return "queued";
    return awaitsApproval(r) ? "ready" : "needs";
  }
  if (r.status === "tailored" || r.status === "tailoring") {
    return (queued && queued.has(r.pk)) ? "queued" : "ready";
  }
  if (r.status === "submitting") return "flight";
  if (["applied", "applied_manual"].includes(r.status)) return "applied";
  if (r.status === "found") return "found";
  return "closed";
}

// A job's location, marked when it matches the FIRST tier of the location
// preference (Washington) so the ranking is visible at a glance rather than
// something you have to open each card to check.
const TOP_LOCATIONS = ["seattle", "bellevue", "washington", "redmond", "kirkland"];

/* Location tiers, in the order of the preference ranking. The Tailored lane
   groups by these so the roles worth approving first are literally first —
   scanning thirty cards for the word "Seattle" is not a review process. */
const LOC_TIERS = [
  { key: "wa",     label: "Seattle · Bellevue · WA", short: "WA", head: "Seattle · Bellevue", cls: "bk-wa",
    test: (l) => TOP_LOCATIONS.some((t) => l.includes(t)) },
  { key: "ca",     label: "California",              short: "CA", head: "California", cls: "bk-ca",
    test: (l) => /california|san francisco|bay area|mountain view|palo alto|los angeles|\bca\b|sunnyvale|san jose/.test(l) },
  { key: "remote", label: "Remote",                  short: "Remote", head: "Remote", cls: "bk-remote",
    test: (l) => /\bremote\b|anywhere|distributed/.test(l) },
  { key: "other",  label: "Elsewhere",               short: "Other", head: "Elsewhere", cls: "bk-other", test: () => true },
];
function locTier(loc) {
  const l = String(loc || "").toLowerCase();
  if (!l) return LOC_TIERS[LOC_TIERS.length - 1];
  return LOC_TIERS.find((t) => t.test(l)) || LOC_TIERS[LOC_TIERS.length - 1];
}
function locHtml(loc) {
  if (!loc) return "";
  const l = String(loc).toLowerCase();
  const top = TOP_LOCATIONS.some((t) => l.includes(t));
  const remote = /\bremote\b/.test(l);
  const cls = top ? "kc-loc top" : remote ? "kc-loc remote" : "kc-loc";
  return `<div class="${cls}" title="${esc(loc)}">${top ? "★ " : ""}${esc(String(loc).slice(0, 44))}</div>`;
}

/* Only shown when a job uses something other than the default identity — a
   badge on every card would be noise, but a job quietly going out from the wrong
   address is exactly the thing worth surfacing. */
function profHtml(id, company) {
  // A job at a rotating company with no identity yet is NOT on the default one:
  // it was deliberately taken off whatever it had, and it gets its address as it
  // is dispatched. Saying nothing here would read as "goes out as you".
  if (!id && (state.rotation || []).some((r) => r.company === (company || "").trim().toLowerCase())) {
    return `<div class="kc-prof is-rot" title="This company rotates its address. This job gets one the moment it is applied — the current one, or a fresh one if that is full.">↻ rotating</div>`;
  }
  if (!id || id === state.profileDefault) return "";
  const p = state.profiles.find((x) => x.id === id) || aliasById(id);
  return `<div class="kc-prof" title="${esc((p && p.email) || id)}">◐ ${esc((p && p.label) || id)}</div>`;
}

/* A minted alias lives in the rotation ledger rather than the profile list, so
   a card stamped with one would otherwise show a bare id. The ADDRESS is the
   thing worth reading before approving a submission, so it is the label. */
function aliasById(id) {
  for (const co of state.rotation || []) {
    const hit = (co.aliases || []).find((a) => a.id === id);
    if (hit) return { id, label: hit.email, email: hit.email };
  }
  return null;
}

function laneCard(r) {
  let retry = "";
  if (r.status === "failed" && r.fail_kind === "spam_flagged") {
    retry = `<button class="kc-retry" data-act="retry" data-pk="${esc(r.pk)}"
       title="Run this application again with human style clicks">↻ Retry</button>`;
  } else if (r.status === "needs_human" && r.gate_reason === "captcha") {
    retry = `<button class="kc-retry kc-apply" data-act="answer" data-pk="${esc(r.pk)}"
       title="Opens the filled application in Chrome again. Solve the CAPTCHA there and click Submit">▶ Solve &amp; submit</button>`;
  } else if (canApply(r)) {
    // Everything tailored is already queued, so Apply no longer means "add this"
    // — it means "do this one first". Remove and Skip are the other two things
    // you can decide about a queued job, and they went missing when the separate
    // queue section was folded into this one; they belong on the card now.
    retry = `<button class="kc-retry kc-apply" data-act="apply-now" data-pk="${esc(r.pk)}"
       title="Run this one now rather than waiting for its turn">▶ Apply now</button>`;
  } else if (r.status === "found") {
    retry = `<button class="kc-retry kc-run" data-act="run-now" data-pk="${esc(r.pk)}"
       title="Score and tailor this job now. It stops before applying">▶ Run now</button>`;
  } else if (isStuck(r)) {
    // The recovery path that used to mean editing the store by hand.
    retry = `<button class="kc-retry kc-queue" data-act="queue-apply" data-pk="${esc(r.pk)}"
       title="Clear the failure and put this job back on the apply queue">↻ Requeue</button>`;
  }
  const aged = r.tailored_at && canApply(r)
    ? `<span class="kc-age" title="résumé written ${esc(new Date(r.tailored_at).toLocaleString())}">${esc(ageLabel(r.tailored_at))}</span>`
    : "";
  const act = state.activity[r.pk];
  const live = (["tailoring", "submitting"].includes(r.status) && act && act.detail)
    ? `<div class="kc-live"><span class="kc-live-dot"></span>${esc(act.detail.replace(/^[^\w]+/, "").slice(0, 70))}</div>` : "";
  return `<div class="kcard" data-open="${esc(r.pk)}" role="button" tabindex="0"
      title="Open details">
    <div class="kc-co">${esc(r.company)}</div>
    <div class="kc-role">${esc(r.title)}</div>
    ${locHtml(r.location)}
    ${profHtml(r.profile_id, r.company)}
    ${live}
    <div class="kc-foot">${scoreHtml(r.match_score)}${tagHtml(r.status)}${aged}${retry}</div>
  </div>`;
}
/* Group the approval queue by the DAY the résumé was written.

   A tailored résumé is a snapshot taken against the posting as it read that day,
   so its age is the thing worth seeing before approving: today's batch is fresh,
   last week's was written against a posting that may have changed and a base
   résumé that has since been edited. Location stays available as a filter — it
   ranks WHICH to approve, where the date says whether to approve at all. */
function dayKey(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`
    + `-${String(d.getDate()).padStart(2, "0")}`;
}

/* "Today" alone makes you work out what today is to compare two groups, and it
   goes stale on a page left open overnight. So the actual date is always shown,
   with the relative word in front of it only where that word helps. */
function dayLabel(key) {
  if (!key) return "No date recorded";
  const d = new Date(key + "T12:00:00");
  if (isNaN(d)) return "No date recorded";
  const exact = d.toLocaleDateString(undefined,
    { weekday: "short", day: "numeric", month: "short" });
  const today = dayKey(new Date().toISOString());
  if (key === today) return `Today · ${exact}`;
  const y = new Date(); y.setDate(y.getDate() - 1);
  if (key === dayKey(y.toISOString())) return `Yesterday · ${exact}`;
  return exact;
}

/* "3 days ago" on the card itself, because inside a folded group the heading is
   out of sight and the age is the point. Past a fortnight it counts in weeks
   and then months, so a role posted today and one posted five weeks ago can
   never be confused at a glance. */
function ageLabel(iso) {
  if (!iso) return "";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  if (isNaN(days)) return "";
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 14) return `${days}d ago`;
  if (days < 61) return `${Math.floor(days / 7)}w ago`;
  return `${Math.floor(days / 30.44)}mo ago`;
}

/* The employer's clock, not the pipeline's. `posted_at` is the date the job
   board itself published — present on a minority of rows, because many boards
   never state one. A posting inside this window counts as genuinely new; an
   undated posting never does. Unknown is unknown, so it shows nothing. */
const FRESH_BOARD_HOURS = 48;
function postedAgeHours(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return isNaN(t) ? null : (Date.now() - t) / 3600000;
}
const postedFresh = (r) => {
  const h = postedAgeHours(r.posted_at);
  return h != null && h <= FRESH_BOARD_HOURS;
};
function postedHtml(iso) {
  const h = postedAgeHours(iso);
  if (h == null) return "";
  const hot = h <= FRESH_BOARD_HOURS;
  return `<span class="jr-posted${hot ? " fresh" : ""}"
    title="the employer published this ${esc(new Date(iso).toLocaleString())}">${
    hot ? "● " : ""}posted ${esc(ageLabel(iso))}</span>`;
}

function bucketedByDate(rows) {
  if (state.locFilter) rows = rows.filter((r) => locTier(r.location).key === state.locFilter);
  if (!rows.length) return `<div class="kl-empty">none</div>`;
  const groups = new Map();
  for (const r of rows) {
    const k = dayKey(r.tailored_at);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(r);
  }
  // Newest first, and undated last: an unknown date is the least useful thing to
  // lead with, not the most.
  const keys = [...groups.keys()].sort((a, b) => (b || "").localeCompare(a || ""))
    .sort((a, b) => (a ? 0 : 1) - (b ? 0 : 1));

  if (state.dayShut === null) {
    // Newest day open, the rest folded. Otherwise 75 approvals is a wall.
    state.dayShut = new Set(keys.slice(1));
  }
  let html = "";
  for (const k of keys) {
    const inDay = groups.get(k).slice()
      .sort((a, b) => (b.match_score || 0) - (a.match_score || 0));
    const shut = state.dayShut.has(k);
    html += `<div class="bucket bk-day${shut ? " shut" : ""}">
      <button class="bk-head" data-day="${esc(k)}">
        <span class="bk-caret">${shut ? "▸" : "▾"}</span>
        <span class="bk-name">${esc(dayLabel(k))}</span>
        <span class="bk-n mono">${inDay.length}</span>
      </button>
      ${shut ? "" : `<div class="ps-grid">${inDay.map(laneCard).join("")}</div>`}
    </div>`;
  }
  return html;
}

/* Group by location tier, best tier first. Still used by the lanes that have no
   tailoring date to group by. */
function bucketed(rows) {
  if (!rows.length) return `<div class="kl-empty">none</div>`;
  const groups = new Map(LOC_TIERS.map((t) => [t.key, []]));
  for (const r of rows) groups.get(locTier(r.location).key).push(r);

  // Default: the best tier that has jobs is open, the rest are folded away. With
  // 29 tailored roles the lane is otherwise a wall, and the whole point of the
  // ranking is that the top tier is the one to act on.
  const present = LOC_TIERS.filter((t) => groups.get(t.key).length);
  if (state.collapsed === null) {
    state.collapsed = new Set(present.slice(1).map((t) => t.key));
  }

  let html = "";
  for (const tier of present) {
    const inTier = groups.get(tier.key);
    if (state.locFilter && state.locFilter !== tier.key) continue;
    inTier.sort((a, b) => (b.match_score || 0) - (a.match_score || 0));
    const shut = state.collapsed.has(tier.key) && !state.locFilter;
    html += `<div class="bucket ${tier.cls}${shut ? " shut" : ""}">
      <button class="bk-head" data-bucket="${tier.key}"
        aria-expanded="${!shut}" title="${esc(tier.label)}. Click to ${shut ? "show" : "hide"}">
        <span class="bk-caret" aria-hidden="true">${shut ? "▸" : "▾"}</span>
        <span class="bk-name">${tier.head}</span>
        <span class="bk-n mono">${inTier.length}</span>
      </button>
      ${shut ? "" : `<div class="bk-cards">${inTier.map(laneCard).join("")}</div>`}
    </div>`;
  }
  return html || `<div class="kl-empty">none in this location</div>`;
}

/* Reveal step for the dense lists. Fold plus paging keeps the DOM flat:
   the 575 found jobs render as 43 company rows until one is opened, and an
   opened company shows a page at a time. Nothing is hidden for good, since
   every fold carries its count and opens on one click. */
const REVEAL = 25;
const moreBtn = (key, shown, total, step = REVEAL) =>
  `<button class="ps-more" data-more="${esc(key)}" data-next="${shown + step}"
     title="Reveal the next rows">Show ${Math.min(step, total - shown)} more of ${total}</button>`;

/* Compact gate card for the top section. The question itself is on the card,
   so triage never needs a click; answering happens in the drawer or the
   Needs you tab, both one click away. */
function gateMini(r) {
  const captcha = r.gate_reason === "captcha";
  const act = captcha
    ? `<button class="kc-retry kc-apply" data-act="answer" data-pk="${esc(r.pk)}"
         title="Opens the filled application in Chrome again. Solve the CAPTCHA there and click Submit">▶ Solve &amp; submit</button>`
    : `<button class="kc-retry kc-apply" data-open="${esc(r.pk)}"
         title="Open the question and type an answer">✎ Answer</button>`;
  // Cut at a WORD, not at character 200. "create a Perplexity.ai thread (Option
  // 1: teach ..." stopped mid-phrase, so the card asked you to open the drawer to
  // find out what the question even was — which is the one thing this card exists
  // to save you.
  const full = String(r.gate_question || defaultGateText(r))
    .replace(/[#*_`>]/g, "").replace(/\s+/g, " ").trim();
  const q = full.length <= 240 ? full
    : full.slice(0, 240).replace(/\s+\S*$/, "") + "…";
  return `<div class="ncard" data-open="${esc(r.pk)}" role="button" tabindex="0"
      title="Open details">
    <div class="nc-co">${esc(r.company)}<span class="nc-role">${esc(r.title)}</span></div>
    <div class="nc-q">${esc(q)}</div>
    <div class="nc-foot"><span class="nc-gate">${esc(gateLabel(r.gate_reason))}</span>${act}</div>
  </div>`;
}

/* Dense one-line rows for the big states. A row is the whole record's handle:
   click opens the drawer, the action button on it is the same one the card
   carried. */
function foundRow(r) {
  const sc = r.match_score != null
    ? `<span class="jr-score">${scoreHtml(r.match_score)}</span>`
    : `<span class="jr-score jr-none mono">·</span>`;
  return `<div class="jrow" data-open="${esc(r.pk)}" role="button" tabindex="0" title="Open details">
    ${sc}
    <span class="jr-title">${esc(r.title)}</span>
    ${r.location ? `<span class="jr-loc" title="${esc(r.location)}">${esc(String(r.location).slice(0, 40))}</span>` : ""}
    ${postedHtml(r.posted_at)}
    <span class="jr-when mono">${r.updated_at ? esc(ago(r.updated_at)) : ""}</span>
    <button class="kc-retry kc-run jr-act" data-act="run-now" data-pk="${esc(r.pk)}"
      title="Score and tailor this job now. It stops before applying">▶ Run now</button>
  </div>`;
}
function appliedRow(r) {
  return `<div class="jrow" data-open="${esc(r.pk)}" role="button" tabindex="0" title="Open details">
    <span class="jr-mark">✓</span>
    <span class="jr-co">${esc(r.company)}</span>
    <span class="jr-title">${esc(r.title)}</span>
    ${r.status === "applied_manual" ? `<span class="ps-chip">manual</span>` : ""}
    <span class="jr-when mono">${r.updated_at ? esc(ago(r.updated_at)) : ""}</span>
  </div>`;
}
function closedRow(r) {
  const failed = isStuck(r);
  let act = "";
  if (r.status === "failed" && r.fail_kind === "spam_flagged") {
    act = `<button class="kc-retry jr-act" data-act="retry" data-pk="${esc(r.pk)}"
      title="Run this application again with human style clicks">↻ Retry</button>`;
  } else if (failed) {
    act = `<button class="kc-retry kc-queue jr-act" data-act="queue-apply" data-pk="${esc(r.pk)}"
      title="Clear the failure and put this job back on the apply queue">↻ Requeue</button>`;
  }
  const whyFull = r.closed_reason || r.fail_reason || r.skip_reason || "";
  const why = String(whyFull).replace(/\s+/g, " ").trim().slice(0, 90);
  return `<div class="jrow" data-open="${esc(r.pk)}" role="button" tabindex="0" title="Open details">
    <span class="jr-dot${failed ? " bad" : ""}"></span>
    <span class="jr-co">${esc(r.company)}</span>
    <span class="jr-title">${esc(r.title)}</span>
    ${failed ? tagHtml(r.status) : ""}
    ${why ? `<span class="jr-why" title="${esc(whyFull)}">${esc(why)}</span>` : ""}
    <span class="jr-when mono">${r.updated_at ? esc(ago(r.updated_at)) : ""}</span>
    ${act}
  </div>`;
}

// ── the six sections ──
function needsSec(rows) {
  const n = rows.length;
  return `<section class="psec ps-needs${n ? " has" : ""}">
    <div class="ps-head">
      <span class="ps-dot${n ? " on" : ""}"></span>
      <span class="ps-name">Needs you</span>
      <span class="ps-n mono${n ? " hot" : ""}">${n}</span>
      <span class="ps-hint">${n ? "A question is blocking each of these applications."
                                : "Nothing is waiting on you."}</span>
      ${n ? `<button class="ps-link on" data-goto-needs="1"
        title="The Needs you tab has room to write answers">Open the Needs you tab</button>` : ""}
    </div>
    ${n ? `<div class="ps-grid">${rows.map(gateMini).join("")}</div>` : ""}
  </section>`;
}

function readySec(rows) {
  const n = rows.length;
  const nApprove = rows.filter(canApply).length;
  // Location pills, carried over from the old Tailored lane: only tiers that
  // hold jobs are offered, and the counts read without opening anything.
  let locBar = "";
  if (n) {
    const present = LOC_TIERS
      .map((t) => ({ t, n: rows.filter((r) => locTier(r.location).key === t.key).length }))
      .filter((x) => x.n);
    if (present.length > 1) {
      const pill = (key, label, cnt, cls) =>
        `<button class="lp ${cls}${state.locFilter === key ? " on" : ""}" data-loc="${key}"
           title="${esc(label)}">${esc(label)}<span class="lp-n mono">${cnt}</span></button>`;
      locBar = `<div class="locbar">
        ${pill("", "All", n, "lp-all")}
        ${present.map((x) => pill(x.t.key, x.t.short, x.n, x.t.cls)).join("")}
      </div>`;
    }
  }
  return `<section class="psec ps-ready${n ? " has" : ""}">
    <div class="ps-head">
      <span class="ps-dot${n ? " on" : ""}"></span>
      <span class="ps-name">Ready to apply</span>
      <span class="ps-n mono">${n}</span>
      <span class="ps-hint">${n ? "Tailored and waiting for your approval. Apply sends one now, Queue lines it up."
                                : "Nothing is tailored yet. Run jobs from Found below."}</span>
      ${nApprove ? `<button class="ps-link on" data-approve-all="1"
        title="Approve all ${nApprove} and put them in the apply queue, one at a time per company">Approve all ${nApprove}</button>` : ""}
    </div>
    ${n ? `<div class="ps-ready-body">${locBar}${bucketedByDate(rows)}</div>` : ""}
  </section>`;
}

function flightSec(rows) {
  const n = rows.length;
  return `<section class="psec ps-flight${n ? " has" : ""}">
    <div class="ps-head">
      <span class="ps-dot${n ? " on live" : ""}"></span>
      <span class="ps-name">In flight</span>
      <span class="ps-n mono">${n}</span>
      <span class="ps-hint">${n ? "The browser is filling these right now."
                                : "No application is running right now."}</span>
    </div>
    ${n ? `<div class="ps-grid">${rows.map(laneCard).join("")}</div>` : ""}
  </section>`;
}

/* Queued to apply: approved work waiting its turn, grouped by COMPANY because
   the company is the unit the queue actually runs on — one application at a
   time per company, several companies in parallel up to the limit. Each group
   head is what the old flat list could not be: the company's own Process
   control at readable width, its place in line, and a fold that opens to
   every one of its jobs, so nothing hides behind an "and N more".

   Positions and "starts next" are worked out on the FULL queue BEFORE any
   filtering: hiding NVIDIA must not move a Microsoft job up the line, and
   renumbering it to 1 would claim it did. Groups keep first appearance order,
   which IS dispatch order of each company's next job. */
/* The selection bar, rendered on its own so a tick can refresh JUST this rather
   than the whole pane. Re-rendering 291 rows on every checkbox meant a quick
   second click landed on a node being replaced and was lost — the selection
   looked like it only ever held one job. */
/* Paint one row's ticked state and refresh the bar — in place, no re-render. */
function qselPaint(row, on) {
  if (row) row.classList.toggle("un-picked", !!on);
  const bar = document.getElementById("qsel-bar");
  if (bar) bar.innerHTML = qselBar((state.queue || {}).concurrency || 1);
}

function qselBar(cap) {
  const n = state.qPicked.size;
  if (!n) {
    return `<span class="ps-hint">By company, in dispatch order. Runs ${cap} at a time, one per company.</span>
      <button class="ps-link" data-qsel-all="1"
        title="Select everything listed here, then Skip or Remove them in one go">Select all</button>`;
  }
  return `<span class="ps-hint">${n} selected</span>
    <button class="ps-link on" data-qsel-none="1">Clear</button>
    <button class="ps-link" data-qsel-remove="1"
      title="Take the selected out of the queue. They stay on the board under Ready to apply">Remove ${n}</button>
    <button class="ps-link un-skip" data-qsel-skip="1"
      title="Skip the selected for good — they leave the queue and move to closed">Skip ${n}</button>`;
}

function queuedSec(rows) {
  const q = state.queue || {};
  const cap = q.concurrency || 1;
  const byPk = {};
  rows.forEach((r) => { byPk[r.pk] = r; });
  // `rows` has already been through the board's filters, so intersecting with it
  // is what makes "filter to Microsoft" show Microsoft's queue and not everyone's.
  const pend = (q.pending || [])
    .map((it, gi) => ({ ...it, pos: gi + 1, first: gi < cap && !it.blocked }))
    .filter((it) => byPk[it.pk]);
  if (!pend.length) return "";

  const running = new Set(q.running || []);
  const groups = new Map();
  for (const it of pend) {
    if (!groups.has(it.company)) groups.set(it.company, []);
    groups.get(it.company).push(it);
  }

  const row = (it) => {
    const r = byPk[it.pk] || {};
    const why = it.blocked === "company_busy" ? "waiting for the one running"
      : it.blocked === "backoff" ? `retry in ${Math.ceil(it.ready_in / 60)}m`
      : it.first ? "starts next" : "waiting for a slot";
    return `<li class="un-row${it.first ? " un-next" : ""}${
      state.qPicked.has(it.pk) ? " un-picked" : ""}">
      <input type="checkbox" class="un-sel" data-qsel="${esc(it.pk)}"
        ${state.qPicked.has(it.pk) ? "checked" : ""}
        title="Select for a bulk action" aria-label="Select ${esc(r.title || it.pk)}" />
      <span class="un-pos mono" title="position in the whole queue">${it.pos}</span>
      <span class="un-title" data-open="${esc(it.pk)}" role="button" tabindex="0">${esc(r.title || it.pk)}</span>
      <span class="un-rank mono" title="position within ${esc(it.company)}">#${it.company_rank}</span>
      <span class="un-aged mono" title="${r.tailored_at
        ? `résumé written ${esc(new Date(r.tailored_at).toLocaleString())}`
        : "no tailoring date recorded"}">${esc(ageLabel(r.tailored_at) || "")}</span>
      <span class="un-why">${why}</span>
      <span class="un-acts">
        <button class="un-x" data-act="apply-now" data-pk="${esc(it.pk)}"
          title="Run this one now, ahead of its turn">Now</button>
        <button class="un-x" data-act="queue-remove" data-pk="${esc(it.pk)}"
          title="Take it out of the queue. It stays on the board">Remove</button>
        <button class="un-x un-skip" data-act="queue-skip" data-pk="${esc(it.pk)}"
          title="Skip this job for good. It leaves the queue and moves to closed">Skip</button>
      </span>
    </li>`;
  };

  // Ticks are kept for jobs still IN the queue only, so one that drained or was
  // skipped elsewhere cannot sit in the selection and be acted on twice.
  const live = new Set(pend.map((it) => it.pk));
  for (const pk of [...state.qPicked]) if (!live.has(pk)) state.qPicked.delete(pk);
  const picked = state.qPicked.size;

  const single = groups.size === 1;
  const body = [...groups.entries()].map(([co, list]) => {
    const busy = running.has((co || "").trim().toLowerCase());
    const open = single || state.coFilter === co || state.openQCos.has(co);
    const head = list[0];
    const stateTxt = busy ? "one in flight now"
      : head.first ? "starts next"
      : head.blocked === "backoff" ? `retry in ${Math.ceil(head.ready_in / 60)}m`
      : `in line at ${head.pos}`;
    return `<div class="ung${open ? " open" : ""}">
      <div class="ung-head">
        <button class="ung-fold" data-qco-fold="${esc(co)}" aria-expanded="${open}"
          title="${open ? "Hide" : "Show"} the ${list.length} queued at ${esc(co)}">
          <span class="ps-caret" aria-hidden="true"></span>
          <span class="un-co">${esc(co)}</span>
          <span class="un-gn mono" title="${list.length} job${list.length === 1 ? "" : "s"} queued">${list.length}</span>
          <span class="un-gstate${busy ? "" : head.first ? " next" : ""}">${stateTxt}</span>
        </button>
        ${(state.rotation || []).some((r) => r.company === (co || "").trim().toLowerCase())
          ? `<button class="ps-link un-rot" data-act="rot-co" data-company="${esc(co)}"
              title="Re-point ${esc(co)}'s un-sent jobs at its rotating address — including these, which were queued under the old one — and put every tailored one in line. It stops at the queue; Process runs them."
            >↻ Rotate &amp; queue</button>` : ""}
        <button class="ps-link un-go${busy ? "" : " on"}" data-act="drain-co"
          data-company="${esc(co)}"${busy ? " disabled" : ""}
          title="${busy ? `${esc(co)} already has one running`
                        : `Work through ${esc(co)}'s ${list.length} queued job${list.length === 1 ? "" : "s"}, one after another`}"
        >${busy ? "● Running" : "▶ Process"}</button>
      </div>
      ${open ? `<ol class="un-list">${list.map(row).join("")}</ol>` : ""}
    </div>`;
  }).join("");

  return `<section class="psec ps-queued has">
    <div class="ps-head">
      <span class="ps-dot on"></span>
      <span class="ps-name">Queued to apply</span>
      <span class="ps-n mono">${pend.length}</span>
      <span class="ps-chip">${groups.size} compan${groups.size === 1 ? "y" : "ies"}</span>
      <span class="qsel-bar" id="qsel-bar">${qselBar(cap)}</span>
    </div>
    <div class="un-groups">${body}</div>
  </section>`;
}


function appliedSec(rows) {
  const n = rows.length;
  // Opens at REVEAL like every other lane. It used to open at six, which made a
  // long submitted history look like it had been lost: 72 applications behind a
  // fold that showed six and stepped 25 at a time. Everything you have sent is
  // work you can be asked about, so it stays one click from the board.
  const shown = state.page.applied || REVEAL;
  return `<section class="psec ps-applied">
    <div class="ps-head">
      <span class="ps-dot"></span>
      <span class="ps-name">Applied</span>
      <span class="ps-n mono">${n}</span>
      <span class="ps-hint">${n ? "Submitted, confirmation captured. Newest first."
                                : "Nothing submitted yet."}</span>
      ${n > shown ? `<button class="ps-all" data-see-applied
        title="Open the full history in the Applications table">See all ${n}</button>` : ""}
    </div>
    ${n ? `<div class="jrows">${rows.slice(0, shown).map(appliedRow).join("")}</div>
      ${n > shown ? moreBtn("applied", shown, n) : ""}` : ""}
  </section>`;
}

/* The 575. Raw material, so it renders as an aggregate first: one row per
   company, best score forward, and postings appear only when a company is
   opened, a page at a time. A company under the active company filter opens
   itself, and a narrow search opens every match. */
function foundGroup(co, rows, forceOpen) {
  const open = forceOpen || state.openCos.has(co);
  const best = rows.reduce((m, r) => Math.max(m, r.match_score ?? -1), -1);
  const scored = rows.filter((r) => r.match_score != null).length;
  // Recently published postings must survive the fold: the chip rides inside
  // the company cell so the header's grid keeps reading as a table.
  const freshN = rows.filter(postedFresh).length;
  const freshChip = freshN ? `<span class="fg-fresh"
    title="${freshN} posting${freshN === 1 ? "" : "s"} the employer published in the last 2 days">${freshN} new</span>` : "";
  const key = `co:${co}`;
  const shown = state.page[key] || REVEAL;
  const rotates = (state.rotation || []).some((x) => x.company === (co || "").trim().toLowerCase());
  return `<div class="fgroup${open ? " open" : ""}">
    <div class="fg-headrow">
    <button class="fg-head" data-co-fold="${esc(co)}" aria-expanded="${open}"
      title="${open ? "Hide" : "Show"} the postings found at ${esc(co)}">
      <span class="ps-caret" aria-hidden="true"></span>
      <span class="fg-co">${esc(co)}${freshChip}</span>
      <span class="fg-n mono" title="${rows.length} posting${rows.length === 1 ? "" : "s"}">${rows.length}</span>
      ${best >= 0
        ? `<span class="fg-best mono">best ${scoreHtml(best)} · ${scored} scored</span>`
        : `<span class="fg-best mono">not scored yet</span>`}
    </button>
    ${rotates ? `<button class="fg-rot" data-act="rot-co" data-company="${esc(co)}"
      title="${esc(co)} rotates its address. These have no résumé yet, so nothing is decided for them and no address is spent — press to put everything of ${esc(co)}'s that IS ready into the queue under the rotating address.">↻</button>` : ""}
    </div>
    ${open ? `<div class="jrows fg-rows">${rows.slice(0, shown).map(foundRow).join("")}
      ${rows.length > shown ? moreBtn(key, shown, rows.length) : ""}</div>` : ""}
  </div>`;
}
function foundSec(rows) {
  const n = rows.length;
  const groups = new Map();
  for (const r of rows) {
    const k = r.company || "Unknown";
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(r);
  }
  const gs = [...groups.entries()];
  for (const [, list] of gs) {
    // A posting the employer published in the last two days leads its company,
    // even unscored: acting on a fresh role early is the whole advantage, and
    // score-first would sink an unscored arrival to the bottom of the fold.
    list.sort((a, b) => postedFresh(b) - postedFresh(a)
      || (b.match_score ?? -1) - (a.match_score ?? -1)
      || new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
  }
  gs.sort((a, b) => {
    const ba = a[1][0]?.match_score ?? -1, bb = b[1][0]?.match_score ?? -1;
    return bb - ba || b[1].length - a[1].length || a[0].localeCompare(b[0]);
  });
  const openAll = filtersActive() && n <= 120;   // a narrow search shows its hits
  const freshN = rows.filter(postedFresh).length;
  return `<section class="psec ps-found">
    <div class="ps-head">
      <span class="ps-dot"></span>
      <span class="ps-name">Found</span>
      <span class="ps-n mono">${n}</span>
      ${n ? `<span class="ps-chip">${groups.size} compan${groups.size === 1 ? "y" : "ies"}</span>` : ""}
      <span class="ps-hint">${n ? "The backlog. Open a company to browse what discovery found there."
                                : "Nothing discovered is waiting. Discover finds new postings."}</span>
      ${freshN ? `<button class="ps-link on" data-goto-fresh="1"
        title="Employers published ${freshN} of these in the last 2 days. The Fresh tab shows them across every window">✦ ${freshN} new in the last 2 days</button>` : ""}
      ${n ? `<button class="ps-link on" data-run-all="1"
        title="Score and tailor all ${n} found jobs. Each stops before applying">▶ Run all</button>` : ""}
    </div>
    ${n ? `<div class="fgroups">${gs.map(([co, list]) =>
        foundGroup(co, list, openAll || state.coFilter === co)).join("")}</div>` : ""}
  </section>`;
}

function closedSec(rows) {
  const n = rows.length;
  if (!n) return "";
  const fails = rows.filter(isStuck);
  const skips = rows.filter((r) => !isStuck(r));
  const open = !!state.secOpen.closed;
  const shown = state.page.skipped || REVEAL;
  const body = !open ? "" : `<div class="ps-closed-body">
    ${fails.length ? `<div class="ps-sub">Could not finish</div>
      <div class="jrows">${fails.map(closedRow).join("")}</div>` : ""}
    ${skips.length ? `<div class="ps-sub">Skipped or capped</div>
      <div class="jrows">${skips.slice(0, shown).map(closedRow).join("")}</div>
      ${skips.length > shown ? moreBtn("skipped", shown, skips.length) : ""}` : ""}
  </div>`;
  return `<section class="psec ps-closed">
    <button class="ps-head ps-fold" data-sec-fold="closed" aria-expanded="${open}"
      title="${open ? "Hide" : "Show"} closed work">
      <span class="ps-caret">${open ? "▾" : "▸"}</span>
      <span class="ps-name">Closed</span>
      <span class="ps-n mono">${n}</span>
      ${fails.length ? `<span class="ps-chip bad">${fails.length} failed</span>` : ""}
      ${skips.length ? `<span class="ps-chip">${skips.length} skipped</span>` : ""}
      <span class="ps-hint">Reference only. Nothing here is waiting on you.</span>
    </button>
    ${body}
  </section>`;
}

/* The bar that appears when the board is filtered to ONE company: everything
   about that employer's identity, where you are already looking at its pipeline.
   Rotation used to be reachable only from Discover, which is the pane for
   finding jobs, not for deciding who the applications come from. */
/* Which rotating profile a company is bound to — needed when only the limit is
   being changed, since binding is one call that carries both. */
function rotProfileFor(co) {
  const r = (state.rotation || []).find((x) => x.company === (co || "").trim().toLowerCase());
  return (r && r.profile) || ((state.profiles || []).find((p) => p.kind === "rotating") || {}).id || "";
}

function companyBar() {
  const co = state.coFilter;
  if (!co) return "";
  const rot = (state.rotation || []).find((r) => r.company === co.trim().toLowerCase());
  const rotators = (state.profiles || []).filter((p) => p.kind === "rotating");
  if (!rot && !rotators.length) return "";   // nothing to offer until one exists

  const body = rot ? `
    <span class="cbar-mark">↻</span>
    <span class="cbar-mail" title="Every application to ${esc(co)} goes out under this address until it reaches its limit, then a fresh one is minted.">${
      rot.email ? esc(rot.email) : "first address on the next application"}</span>
    ${rot.email ? `<span class="cbar-n mono">${rot.used}/${rot.limit}</span>` : ""}
    <label class="cbar-lim" title="How many applications ONE address may carry at ${esc(co)}. Your notes: Ramp allows 2, Coinbase 3, OpenAI and Waymo 5.">cap
      <input type="number" min="1" max="50" value="${rot.limit}" data-rot-limit="${esc(co)}" /></label>
    <button class="ps-link cp-rot-go" data-act="rot-co" data-company="${esc(co)}"
      title="Re-point ${esc(co)}'s un-sent jobs at the rotating address and queue everything that has a résumé. Nothing is applied.">Rotate &amp; queue</button>
    <button class="ps-link" data-rot-off="${esc(co)}"
      title="Stop rotating for ${esc(co)}. Addresses already used are kept on record.">off</button>`
  : `<span class="cbar-hint">${esc(co)} applies under your default identity.</span>
    <button class="ps-link cp-rot-go" data-rot-on="${esc(co)}" data-profile="${esc(rotators[0].id)}"
      title="Give ${esc(co)} its own rotating address: a new one every ${rotators[0].limit || 5} applications, so no single address carries more than the employer's cap allows.">↻ Rotate addresses here</button>`;

  return `<div class="cbar${rot ? " on" : ""}">
    <span class="cbar-co">${esc(co)}</span>${body}</div>`;
}

function viewPipeline() {
  if (!state.apps.length) {
    return `<div class="empty"><div class="empty-big">No applications yet</div>
      Click <b>Discover · All</b> above to find jobs from your watchlist.<br>
      They move down this board: found, then tailored, then applied.</div>`;
  }
  const S = { needs: [], ready: [], queued: [], flight: [], applied: [], found: [], closed: [] };
  const inQ = queuedPks();
  for (const r of visible(state.apps)) S[sectionOf(r, inQ)].push(r);
  const shown = Object.values(S).reduce((a, b) => a + b.length, 0);
  if (!shown && filtersActive()) return emptyFiltered();
  return `<div class="pstack">
    ${companyBar()}
    ${needsSec(S.needs)}
    ${readySec(S.ready)}
    ${queuedSec(S.queued)}
    ${flightSec(S.flight)}
    ${appliedSec(S.applied)}
    ${foundSec(S.found)}
    ${closedSec(S.closed)}
  </div>`;
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
  const allRows = visible(state.apps.filter(pred));
  // Paginate. All 1737 rows at once produced 1.1MB of DOM and blocked the main
  // thread for 145ms on every switch to this tab and every re-render after it,
  // which is exactly what makes a click feel dead. Every other lane on this board
  // already pages; this table was the exception.
  const shownN = state.page.appsTable || 50;
  const rows = allRows.slice(0, shownN);
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
      </tr></thead><tbody>${body}</tbody></table>${empty}
      ${allRows.length > rows.length
        ? moreBtn("appsTable", rows.length, allRows.length, 50)
        : ""}</div>`;
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

// --- Activity heatmap: a year of discovery and sending, one cell per day ---
/* Two series, one grid. Found and applied differ by an order of magnitude
   (hundreds of finds against tens of sends), so a single shared colour ramp
   would pin every applied day at the palest step, and a per-series toggle
   would put the two halves of one story behind a click. Instead the ONE grid
   carries each series on its own channel: the cell's tint is a --scan ramp
   for jobs FOUND, stepped at the quartiles of the non-zero days so the scale
   tracks what this pipeline actually produces rather than a fixed ceiling;
   applications SENT are an ink dot ON the cell, sized against applied's own
   range. A dot's presence is binary and unmissable, which is what keeps a
   3-application day exactly as legible as a 600-find day — and the overlay
   also shows conversion (deep cell, no dot = found plenty, sent nothing).
   A day with no activity keeps the bare card surface: zero reads as zero,
   never as "a little". */
let _heatBusy = false;
async function loadActivity() {
  if (_heatBusy) return;
  _heatBusy = true;
  try {
    state.heat = DEMO ? demoActivity()
      : await fetch(api("/activity?days=371"), { headers: auth.header() }).then((r) => r.json());
  } catch { if (!state.heat) state.heat = { error: true }; }
  _heatBusy = false;
  if (state.tab === "activity") refreshPane();
}

// Demo has no /activity endpoint — synthesise a plausible, deterministic year.
function demoActivity() {
  const days = [];
  const totals = { found: 0, applied: 0 };
  for (let i = 370; i >= 0; i--) {
    const date = dayKey(new Date(Date.now() - i * 86400000).toISOString());
    let found = 0, applied = 0;
    if (i < 84) {  // the tool is new: a quiet year, then twelve busy weeks
      const h = [...date].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 9973, 7);
      found = h % 4 ? h % 320 : 0;
      applied = h % 3 ? h % 19 : 0;
    }
    totals.found += found; totals.applied += applied;
    days.push({ date, found, applied });
  }
  return { days, totals };
}

const noonOf = (iso) => new Date(iso + "T12:00:00");

function viewActivity() {
  const h = state.heat;
  if (!h) { loadActivity(); return `<div class="empty">Loading activity…</div>`; }
  if (h.error) {
    return `<div class="empty"><div class="empty-big">No activity to show</div>
      Could not reach the backend. Try refresh once the daemon is running.</div>`;
  }
  const days = (h.days || []).map((d) => ({
    date: d.date, found: Number(d.found) || 0, applied: Number(d.applied) || 0 }));
  if (!days.length) return `<div class="empty">Nothing recorded yet.</div>`;

  // Quantile cuts, each series against its own non-zero days, so both ramps
  // spread over the numbers this pipeline actually produces.
  const q = (arr, p) => arr.length ? arr[Math.min(arr.length - 1, Math.floor(p * arr.length))] : 0;
  const nzF = days.map((d) => d.found).filter(Boolean).sort((a, b) => a - b);
  const nzA = days.map((d) => d.applied).filter(Boolean).sort((a, b) => a - b);
  const fCut = [q(nzF, 0.25), q(nzF, 0.5), q(nzF, 0.75)];
  const aCut = [q(nzA, 1 / 3), q(nzA, 2 / 3)];
  const fLvl = (n) => !n ? 0 : n < fCut[0] ? 1 : n < fCut[1] ? 2 : n < fCut[2] ? 3 : 4;
  const aLvl = (n) => !n ? 0 : n < aCut[0] ? 1 : n < aCut[1] ? 2 : 3;

  const cell = (d) => {
    if (!d) return `<span class="hm-c hm-pad"></span>`;
    const label = noonOf(d.date).toLocaleDateString(undefined,
      { weekday: "short", day: "numeric", month: "short", year: "numeric" });
    return `<span class="hm-c hm-f${fLvl(d.found)}" data-hd="${esc(label)}"
      data-hf="${d.found}" data-ha="${d.applied}">${
      d.applied ? `<i class="hm-dot hm-a${aLvl(d.applied)}"></i>` : ""}</span>`;
  };

  // GitHub layout: one column per week, Sunday at the top. Pad the first and
  // last weeks so every column is a full seven rows.
  const cells = [];
  for (let i = noonOf(days[0].date).getDay(); i > 0; i--) cells.push(null);
  cells.push(...days);
  while (cells.length % 7) cells.push(null);
  const nWeeks = cells.length / 7;

  // A month label sits over the week that starts it; drop one that would
  // crowd the label before it (two changes can land three columns apart).
  const monthRow = [];
  let lastM = -1, lastCol = -9;
  for (let w = 0; w < nWeeks; w++) {
    const firstDay = cells.slice(w * 7, w * 7 + 7).find(Boolean);
    if (!firstDay) continue;
    const m = noonOf(firstDay.date).getMonth();
    if (m === lastM) continue;
    lastM = m;
    if (w - lastCol < 3) continue;
    lastCol = w;
    monthRow.push(`<span style="grid-column:${w + 1} / span ${Math.min(4, nWeeks - w)}">${
      esc(noonOf(firstDay.date).toLocaleDateString(undefined, { month: "short" }))}</span>`);
  }

  const tf = Number((h.totals || {}).found ?? days.reduce((a, d) => a + d.found, 0)) || 0;
  const ta = Number((h.totals || {}).applied ?? days.reduce((a, d) => a + d.applied, 0)) || 0;
  const lgCell = (cls, dot) => `<span class="hm-c ${cls}">${dot || ""}</span>`;
  return `<div class="hm-card">
    <div class="hm-sum">
      <span class="hm-tot"><b class="mono">${tf}</b> jobs found</span>
      <span class="hm-tot"><b class="mono">${ta}</b> applications sent</span>
      <span class="hm-when">one cell per day for the past year</span>
    </div>
    <div class="hm-scroll">
      <div class="hm-frame">
        <span></span>
        <div class="hm-months mono" style="grid-template-columns:repeat(${nWeeks}, 14px)">${monthRow.join("")}</div>
        <div class="hm-wdays mono" aria-hidden="true">
          <span></span><span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span>
        </div>
        <div class="hm-grid" role="img"
          aria-label="Daily activity for the past year: ${tf} jobs found and ${ta} applications sent">${
          cells.map(cell).join("")}</div>
      </div>
    </div>
    <div class="hm-legend">
      <span class="hm-lg"><span class="hm-lgl">Jobs found</span>
        <span class="hm-lgt">none</span>
        ${lgCell("hm-f0")}${lgCell("hm-f1")}${lgCell("hm-f2")}${lgCell("hm-f3")}${lgCell("hm-f4")}
        <span class="hm-lgt">more</span></span>
      <span class="hm-lg"><span class="hm-lgl">Applications sent</span>
        ${lgCell("hm-f0", '<i class="hm-dot hm-a1"></i>')}
        ${lgCell("hm-f0", '<i class="hm-dot hm-a2"></i>')}
        ${lgCell("hm-f0", '<i class="hm-dot hm-a3"></i>')}
        <span class="hm-lgt">a dot marks a day that applied, bigger means more</span></span>
    </div>
    <div class="hm-tip mono" id="hm-tip" hidden></div>
  </div>`;
}

// --- Fresh: roles the employer published inside a chosen window --------------
/* The board answers "what is waiting on me"; this tab answers "what appeared
   while I was not looking". It stands on one honest rule: only a posting whose
   job board published a date can be called new, and most boards publish none,
   so on a real store nearly every row is undated. The view therefore always
   says how many rows it could not judge, and an undated role is never shown
   as fresh. One fetch at the widest window; the narrower windows filter that
   same payload in place, so switching feels like turning a lens, not loading
   a page. */
const FRESH_WINDOWS = [[24, "Last 24 hours"], [48, "Last 48 hours"], [168, "Last 7 days"]];
const FRESH_MAX_HOURS = FRESH_WINDOWS[FRESH_WINDOWS.length - 1][0];
const freshWindowText = () =>
  (FRESH_WINDOWS.find(([h]) => h === state.freshHours) || FRESH_WINDOWS[1])[1]
    .replace(/^Last/, "last");

let _freshBusy = false;
/* What the last scans decided NOT to keep. Fetched with the fresh view because
   it answers the question the fresh view provokes: a role you can see on the
   careers page is missing, and the three reasons need three different fixes. */
/* Per company results as the sweep completes them. Fetched only while a run is
   live or has just ended, because outside that window it is a list of things that
   already happened and the board has better places to say so. */
async function loadScanLog() {
  if (DEMO) return;
  try {
    state.scanLog = await fetch(api("/scan-log"), { headers: auth.header() })
      .then((r) => r.json());
  } catch { /* leave the last good list rather than blanking it */ }
}

async function loadPassedOver() {
  if (DEMO) { state.passed = { jobs: [], by_reason: {} }; return; }
  try {
    state.passed = await fetch(api("/passed-over?limit=200"), { headers: auth.header() })
      .then((r) => r.json());
  } catch { state.passed = null; }
}

async function loadFresh() {
  if (_freshBusy) return;
  _freshBusy = true;
  try {
    await loadPassedOver();
    state.fresh = DEMO ? demoFresh()
      : await fetch(api(`/fresh?hours=${FRESH_MAX_HOURS}`), { headers: auth.header() })
          .then((r) => r.json());
  } catch { if (!state.fresh) state.fresh = { error: true }; }
  _freshBusy = false;
  if (state.tab === "fresh") refreshPane();
}

// Demo has no /fresh endpoint — stamp a plausible, deterministic recent week
// onto a handful of the sample rows.
function demoFresh() {
  const ages = [2, 7, 19, 30, 45, 70, 96, 130, 158];
  const jobs = state.apps.slice(0, ages.length).map((a, i) => {
    const iso = new Date(Date.now() - ages[i] * 3600000).toISOString();
    return { pk: a.pk, company: a.company, title: a.title, location: a.location,
      jd_url: a.jd_url, status: a.status, match_score: a.match_score,
      posted_at: iso, age_hours: ages[i], posted_label: ago(iso) };
  });
  return { hours: FRESH_MAX_HOURS, jobs, count: jobs.length,
    undated: Math.max(0, state.apps.length - jobs.length),
    companies: new Set(jobs.map((j) => j.company)).size };
}

/* One role, freshest first inside its day. The board's record wins over the
   snapshot where the two disagree, so a job run from here changes its pill
   without waiting for a refetch, and the row opens the same drawer as
   everywhere else. */
function freshRow(j, byPk) {
  const r = byPk.get(j.pk) || j;
  const hot = (Number(j.age_hours) || 0) <= 24;
  const run = r.status === "found"
    ? `<button class="kc-retry kc-run jr-act" data-act="run-now" data-pk="${esc(j.pk)}"
        title="Score and tailor this job now. It stops before applying">▶ Run now</button>` : "";
  return `<div class="jrow fresh-row${hot ? " hot" : ""}" data-open="${esc(j.pk)}"
      role="button" tabindex="0" title="Open details">
    <span class="fr-age mono${hot ? " hot" : ""}"
      title="the employer published this ${esc(new Date(j.posted_at).toLocaleString())}">${
      esc(j.posted_label || ageLabel(j.posted_at))}</span>
    <span class="jr-co">${esc(j.company)}</span>
    <span class="jr-title">${esc(j.title)}</span>
    ${j.location ? `<span class="jr-loc" title="${esc(j.location)}">${esc(String(j.location).slice(0, 40))}</span>` : ""}
    <span class="fr-side">${r.match_score != null ? scoreHtml(r.match_score) : ""}${tagHtml(r.status)}${run}</span>
  </div>`;
}

/* The empty state is the COMMON state on a young store, so it must read as a
   deliberate answer, not a failure. Two different silences get two different
   sentences: "nothing dated landed in this window" is a claim about dated
   postings only, and the standing footer carries how much of the board has no
   publish date at all. */
/* Every facet currently narrowing the list, in words. A company tick is saved
   to localStorage and restored on the next visit, so the commonest way to see
   an empty Fresh tab is a choice made days ago against a company that has
   published nothing since. "Nothing matches your current filters" sends you
   hunting through a rail of 47 checkboxes for one you do not remember ticking,
   so the empty state names the filter instead of alluding to it. */
function freshActiveFacets() {
  const q = state.query.trim();
  const out = [];
  if (state.freshPicked.size)
    out.push(`a company tick on <b>${esc([...state.freshPicked].sort().join(", "))}</b>`);
  if (state.freshStatus) out.push(`the <b>${esc(state.freshStatus)}</b> status facet`);
  if (state.coFilter) out.push(`the company filter <b>${esc(state.coFilter)}</b>`);
  if (q) out.push(`a search for <b>${esc(q)}</b>`);
  return out;
}
function freshEmpty(windowed, undated, winTxt) {
  if (windowed.length) {                         // the window has roles; filters hid them
    const on = freshActiveFacets();
    if (!on.length) return emptyFiltered();
    const n = windowed.length;
    return `<div class="empty">
      <div class="empty-big">${n} dated role${n === 1 ? "" : "s"} in this window,
        every one of them hidden</div>
      <div class="fresh-claim">Hidden by ${on.join(", and by ")}. A tick outlives
        the tab it was made in, so this can be a choice from an earlier visit
        rather than anything the scan did.</div>
      <div class="empty-act">
        <button class="btn btn-ghost" data-clear-filters="1">Clear filters</button>
      </div>
    </div>`;
  }
  const newest = (state.fresh.jobs || [])[0];
  const wider = newest
    ? `<div class="fresh-hint">The newest dated posting appeared
        <b>${esc(newest.posted_label || ageLabel(newest.posted_at))}</b> at
        <b>${esc(newest.company)}</b>. Widen the window above to see it.</div>`
    : "";
  const scope = undated
    ? "That is a statement about dated postings only."
    : "Every posting on the board carries a publish date, so this is the whole story.";
  return `<div class="fresh-empty">
    <div class="empty-big">Nothing provably new in the ${esc(winTxt)}</div>
    <div class="fresh-claim">No posting with a publish date landed inside this window. ${scope}</div>
    ${wider}
  </div>`;
}

/* ── The facet rail: window, status, companies, scan — one column. ──
   The three blocks this tab used to be (scan slab, chip bar, results) become
   facets of one browse surface. Every facet option carries a count, and every
   count is computed against the OTHER active facets, so the number beside an
   option answers "what would the list show if I chose this". A zero stays
   visible and greyed rather than hidden: the absence is the information.

   DELIBERATE UNIFICATION: the company facet IS the scan picker. A tick does
   two jobs at once — it narrows the visible list to the ticked companies, and
   it chooses what the next Run will crawl. The two readings agree: "the
   companies this page is about right now". An empty set means "about
   everything": it filters nothing and leaves Run disabled, so neither
   behaviour is lost. The selection is the tab's own (state.freshPicked);
   state.picked belongs to the Discover picker and is never touched here. */
function freshByPk() { return new Map(state.apps.map((a) => [a.pk, a])); }
// The board's record wins over the scan snapshot — same rule as freshRow, so a
// job run from this tab counts under its new status without a refetch.
function freshStatusOf(j, byPk) { return (byPk.get(j.pk) || j).status || "found"; }
function freshFacetFilters(byPk) {
  const q = state.query.trim().toLowerCase();
  return {
    win: (j) => (Number(j.age_hours) || 0) <= state.freshHours,
    co: (j) => !state.freshPicked.size || state.freshPicked.has(j.company),
    st: (j) => !state.freshStatus || freshStatusOf(j, byPk) === state.freshStatus,
    // The workbar's company dropdown and the search box apply to every tab, so
    // here they sit under every facet count rather than being facets themselves.
    q: (j) => (!state.coFilter || j.company === state.coFilter)
           && (!q || `${j.company} ${j.title}`.toLowerCase().includes(q)),
  };
}
function freshScanCos() {
  const q = state.freshCoQuery.trim().toLowerCase();
  return state.companies.filter((c) => !q || c.toLowerCase().includes(q));
}
/* The company facet's rows: every watchlist name, plus any company the payload
   still remembers that has since left the watchlist. Zero counts are greyed,
   never hidden — "this board published nothing dated in this window" is
   exactly the fact that tells you where a scan is worth pointing, and the
   checkbox stays live because scanning a quiet board is the point of a scan. */
function freshScanList() {
  const f = state.fresh || {};
  const jobs = f.jobs || [];
  const F = freshFacetFilters(freshByPk());
  const q = state.freshCoQuery.trim().toLowerCase();
  const names = [...new Set([...state.companies, ...jobs.map((j) => j.company)])]
    .filter((c) => !q || String(c).toLowerCase().includes(q))
    .sort((a, b) => String(a).localeCompare(String(b), undefined, { sensitivity: "base" }));
  const rows = names.map((c) => {
    const n = jobs.filter((j) => j.company === c && F.win(j) && F.st(j) && F.q(j)).length;
    return `<label class="fs-item${n ? "" : " zero"}">
      <input type="checkbox" data-fresh-co="1" value="${esc(c)}"
        ${state.freshPicked.has(c) ? "checked" : ""}>
      <span class="fs-nm">${esc(c)}</span>
      <span class="facet-n mono">${n}</span></label>`;
  }).join("");
  return rows || `<div class="cp-none">No companies match “${esc(state.freshCoQuery)}”.</div>`;
}
/* Honest about the cost before the press: a scan is a live browser crawl of
   each company's job board, roughly a minute per board, not an instant query. */
const freshEta = (n) => n === 1 ? "about a minute of browser work"
  : n <= 3 ? "a few minutes of browser work"
  : `about ${n} minutes of browser work`;
function freshScanFoot() {
  // Only names still on the watchlist can be crawled, so the promise on the
  // button never exceeds what Run would actually post.
  // Only the companies YOU picked matter. Scans run per company now, so another
  // company being read is not a reason you cannot start yours. This used to gate
  // on state.stats.discovering, which is true whenever anything anywhere is
  // scanning, so starting one company locked out all the others — exactly what
  // the per company claims were built to allow.
  const busy = new Set((state.stats.scanning || []).map((c) => String(c).toLowerCase()));
  const picked = [...state.freshPicked].filter((c) => state.companies.includes(c));
  const mine = picked.filter((c) => busy.has(c.toLowerCase()));
  const free = picked.filter((c) => !busy.has(c.toLowerCase()));
  const n = free.length;
  const winTxt = freshWindowText();

  if (busy.size && !picked.length) {
    return `<span class="fs-sum"><span class="fs-live-dot"></span>Scanning
        ${esc([...busy].join(", "))} now. Tick other companies to start them
        alongside it.</span>
      <button class="cp-add-btn fs-run" disabled>Pick companies</button>`;
  }
  if (picked.length && !n) {
    return `<span class="fs-sum"><span class="fs-live-dot"></span>
        ${esc(mine.join(", "))} ${mine.length === 1 ? "is" : "are"} already scanning.
        New postings land in the list the moment it finishes.</span>
      <button class="cp-add-btn fs-run" disabled>Scanning…</button>`;
  }
  const sum = n
    ? `A live crawl of <b class="mono">${n}</b> job board${n === 1 ? "" : "s"},
       ${esc(freshEta(n))}. Only roles the employer published in the
       <b>${esc(winTxt)}</b> count; the crawler stops once it reaches older listings.`
    : `Tick companies above. The window facet sets both what a scan fetches
       and what the list shows.`;
  const label = n
    ? `▶ Scan ${n} ${n === 1 ? "company" : "companies"} · ${esc(winTxt)}`
    : `▶ Run scan`;
  return `<span class="fs-sum">${sum}</span>
    <button class="cp-add-btn fs-run" data-fs-run="1" type="button" ${n ? "" : "disabled"}
      title="${n ? `Scan the ${n} ticked ${n === 1 ? "company" : "companies"} now, ${esc(freshEta(n))}`
                 : "Tick at least one company first"}">${label}</button>`;
}
/* The rail's data regions, each buildable on its own so a tick or a typed
   query can refresh COUNTS in place (freshRefresh below) without tearing down
   the checkboxes or the inputs around them. */
const facetRowHtml = (attr, val, label, n, sel) =>
  `<button class="facet-row${n ? "" : " zero"}" ${attr}="${esc(String(val))}"
    aria-selected="${sel}">
    <span class="facet-l">${esc(label)}</span>
    <span class="facet-n mono">${n}</span></button>`;

// WINDOW — cumulative by construction, so the counts only grow downwards.
function freshWinRows(f, byPk) {
  const jobs = f.jobs || [];
  const F = freshFacetFilters(byPk);
  return FRESH_WINDOWS.map(([h, label]) =>
    facetRowHtml("data-fresh-w", h,
      label,
      jobs.filter((j) => (Number(j.age_hours) || 0) <= h && F.co(j) && F.st(j) && F.q(j)).length,
      state.freshHours === h)).join("");
}

// STATUS — every status the payload actually holds, in pipeline order. One
// filtered down to nothing stays visible at zero: the absence is the news.
function freshStRows(f, byPk) {
  const jobs = f.jobs || [];
  const F = freshFacetFilters(byPk);
  const order = Object.keys(STATUS_META);
  const known = [...new Set(jobs.map((j) => freshStatusOf(j, byPk)))]
    .sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
  const stBase = (j) => F.win(j) && F.co(j) && F.q(j);
  return [
    facetRowHtml("data-fresh-st", "", "Any status", jobs.filter(stBase).length, !state.freshStatus),
    ...known.map((s) => {
      const m = STATUS_META[s];
      return facetRowHtml("data-fresh-st", s,
        m ? m.label : String(s).replace(/_/g, " "),
        jobs.filter((j) => stBase(j) && freshStatusOf(j, byPk) === s).length,
        state.freshStatus === s);
    }),
  ].join("");
}

function freshTickSum() {
  const picked = [...state.freshPicked].filter((c) => state.companies.includes(c)).length;
  return picked ? `${picked} of ${state.companies.length} ticked` : "all shown";
}

function freshRail(f, byPk) {
  return `<div class="frail-group">
      <div class="frail-h">Window</div>
      <div id="frail-win">${freshWinRows(f, byPk)}</div>
    </div>
    <div class="frail-group">
      <div class="frail-h">Status</div>
      <div id="frail-st">${freshStRows(f, byPk)}</div>
    </div>
    <div class="frail-group">
      <div class="frail-h">Companies
        <span class="frail-hn mono" id="frail-hn">${freshTickSum()}</span></div>
      <div class="frail-cos">
        <div class="fs-tools">
          <input id="fs-search" class="cp-search fs-search" type="search"
            placeholder="Filter companies…" value="${esc(state.freshCoQuery)}"
            autocomplete="off" spellcheck="false" aria-label="Filter companies">
          <button class="cp-lk" data-fs-all="1" type="button"
            title="Tick every company the filter shows">All</button>
          <button class="cp-lk" data-fs-none="1" type="button"
            title="Untick every company">Clear</button>
        </div>
        <div class="fs-list" id="fs-list" aria-label="Companies">${freshScanList()}</div>
        <div class="frail-note">A tick narrows the list and points the next scan
          at that company.</div>
      </div>
    </div>
    <div class="frail-group frail-scan">
      <div class="frail-h">Scan</div>
      <div class="frail-scanbody">
        ${state.freshNote ? `<div class="fs-note">${esc(state.freshNote)}</div>` : ""}
        <div class="fs-foot" id="fs-foot">${freshScanFoot()}</div>
      </div>
    </div>`;
}

/* The headline row above everything: the same facts the rail carries, as
   numbers readable before touching anything. The two quiet tiles are the
   honesty figures — how much this view cannot judge, and how much the scans
   chose to drop. */
function freshStats(f) {
  const jobs = f.jobs || [];
  const inWin = jobs.filter((j) => (Number(j.age_hours) || 0) <= state.freshHours);
  const nCos = new Set(inWin.map((j) => j.company)).size;
  const tile = (v, l, dim) => `<div class="fstat${dim ? " dim" : ""}">
    <div class="fstat-v">${Number(v) || 0}</div>
    <div class="fstat-l">${esc(l)}</div></div>`;
  return `<div class="fresh-stats">
    ${tile(inWin.length, `New in the ${freshWindowText()}`)}
    ${tile(nCos, "Companies with something new")}
    ${tile(Number(f.undated) || 0, "Carry no publish date", true)}
    ${tile((state.passed && state.passed.count) || 0, "Passed over by recent scans", true)}
  </div>`;
}

/* "Passed over": the roles a recent scan saw and did not keep.

   Grouped by company, like Found and the queue, because that is the unit the
   owner scans in and because one noisy board can otherwise fill the whole list:
   a single careers page with two hundred old postings would bury every other
   company's two.

   The three outcomes stay distinct because they need different fixes. Too old
   means widen the window. Not a match means change the preferences. Missing from
   here as well means the scan never read the posting, which is a reading problem
   and is stated in the footer rather than left to be inferred from an absence. */
const PASSED_LABEL = { too_old: "too old", not_relevant: "not a match" };

function passedOverPanel() {
  const p = state.passed;
  const all = (p && p.companies) || [];
  if (!all.length) return "";

  /* NOT scoped by the company facet, though it used to be.

     That facet chooses what a scan will COVER. This panel is a record of what
     already happened, and the two are different questions. Scoping one by the
     other meant a single tick on a company with nothing dropped collapsed the
     whole trail to "0 of 30 companies, nothing was passed over" while 2034
     records sat behind it — and because the tick persists across reloads, the
     panel then looked permanently broken.

     Ticked companies are floated to the top instead, so the connection survives
     without anything being hidden. */
  const pickedLc = new Set([...state.freshPicked].map((c) => String(c).toLowerCase()));
  const groups = pickedLc.size
    ? [...all].sort((a, b) =>
        (pickedLc.has(String(b.company || "").toLowerCase()) ? 1 : 0)
        - (pickedLc.has(String(a.company || "").toLowerCase()) ? 1 : 0))
    : all;

  const body = groups.map((g) => {
    const co = g.company || "unknown";
    const open = state.passedOpen.has(co);
    const why = Object.entries(g.by_reason || {})
      .sort((a, b) => b[1] - a[1])
      .map(([k, n]) => `${n} ${PASSED_LABEL[k] || k}`).join(", ");
    const rows = g.jobs || [];
    // The count is the TRUTH; the list is a sample. Saying so beats letting a
    // company that rejected sixty roles look as though it rejected twenty five.
    // g.total is how many were rejected; g.kept is how many rows were stored.
    // Saying both keeps the header honest without pretending the list is whole.
    const more = g.total > rows.length
      ? `<li class="po-row po-note">showing the ${rows.length} most recent
           ${g.kept && g.kept < g.total ? `of ${g.kept} kept, out of ${g.total} rejected`
                                        : `of ${g.total}`}</li>`
      : "";
    const list = open ? `<ol class="po-list">${rows.map((j) => {
      const when = j.posted_label || "no publish date";
      const title = j.url
        ? `<a class="po-t" href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>`
        : `<span class="po-t">${esc(j.title)}</span>`;
      return `<li class="po-row">
        ${title}
        <span class="po-when mono">${esc(when)}</span>
        <span class="po-why">${esc(PASSED_LABEL[j.reason] || j.reason || "skipped")}</span>
      </li>`;
    }).join("")}${more}</ol>` : "";
    return `<div class="pog${open ? " open" : ""}">
      <button class="pog-head${pickedLc.has(co.toLowerCase()) ? " pog-picked" : ""}"
        data-passed-co="${esc(co)}" aria-expanded="${open}">
        <span class="ps-caret">${open ? "▾" : "▸"}</span>
        <span class="pog-co">${esc(co)}</span>
        <span class="pog-n mono">${g.total}</span>
        <span class="pog-why">${esc(why)}</span>
      </button>
      ${list}
    </div>`;
  }).join("");

  // Reasons and counts are summed over the SCOPED groups, so a narrowed panel
  // never quotes the whole store's numbers as its own.
  const byReason = {};
  for (const g of groups)
    for (const [k, n] of Object.entries(g.by_reason || {}))
      byReason[k] = (byReason[k] || 0) + (Number(n) || 0);
  const totals = Object.entries(byReason)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${n} ${PASSED_LABEL[k] || k}`).join(" · ");
  const nAll = groups.reduce((s, g) => s + (Number(g.total) || 0), 0);

  const none = `<div class="po-none">Nothing was passed over at the ticked
    companies. Recent scans dropped roles only at the ${all.length} others.</div>`;

  return `<section class="psec ps-passed">
    <div class="ps-head">
      <span class="ps-dot"></span>
      <span class="ps-name">Passed over</span>
      <span class="ps-n mono">${nAll}</span>
      <span class="ps-chip">${pickedLc.size
        ? `${groups.length} of ${all.length} companies`
        : `${groups.length} compan${groups.length === 1 ? "y" : "ies"}`}</span>
      <span class="ps-hint">Seen by a recent scan and not kept${totals
        ? `. ${esc(totals)}` : ""}.</span>
    </div>
    ${groups.length ? body : none}
    <div class="po-foot">A company missing from here had nothing rejected, or was
      not scanned. A role missing from its list was never read by the scan, which is
      a reading problem rather than a filtering one.</div>
  </section>`;
}

/* The results column: a persistent search box, the honest showing line, then
   the day groups — the same dayLabel voice the approval queue uses, so "what
   appeared today" is answerable in one glance. */
// "Showing N of M" over DATED roles only: M is every dated role the payload
// holds at the widest window, so the line owns up to what the facets hid.
function freshShowingHtml(f, byPk) {
  const F = freshFacetFilters(byPk);
  const all = f.jobs || [];
  const jobs = all.filter((j) => F.win(j) && F.co(j) && F.st(j) && F.q(j));
  return `Showing <b class="mono">${jobs.length}</b> of
    <b class="mono">${all.length}</b> dated roles`;
}

function freshDaysHtml(f, byPk) {
  const F = freshFacetFilters(byPk);
  const all = f.jobs || [];
  const windowed = all.filter(F.win);
  const jobs = windowed.filter((j) => F.co(j) && F.st(j) && F.q(j));
  if (!jobs.length) return freshEmpty(windowed, Number(f.undated) || 0, freshWindowText());
  const groups = new Map();
  for (const j of jobs) {
    const k = dayKey(j.posted_at);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(j);
  }
  return [...groups.entries()].map(([k, list]) => `<div class="fresh-day">
    <div class="fd-head"><span class="fd-name">${esc(dayLabel(k))}</span>
      <span class="fd-n mono">${list.length}</span></div>
    <div class="jrows">${list.map((j) => freshRow(j, byPk)).join("")}</div>
  </div>`).join("");
}

function freshResults(f, byPk) {
  const undated = Number(f.undated) || 0;
  // The far larger undated number gets the standing footer below, because it
  // needs a sentence of reasoning, not a figure in passing.
  const head = `<div class="fr-head">
    <input id="fr-search" class="search fr-search" type="search"
      placeholder="Search company or title…" value="${esc(state.query)}"
      autocomplete="off" spellcheck="false" aria-label="Search fresh roles">
    <span class="fr-showing" id="fr-showing">${freshShowingHtml(f, byPk)}</span>
  </div>`;

  return `<div class="fresh-card">
    ${head}
    <div id="fr-days">${freshDaysHtml(f, byPk)}</div>
    ${undated ? `<div class="fresh-foot">Freshness is judged by the date the employer's own
      board publishes. <b>${undated}</b> postings carry no such date, because many boards
      never state one, so this view cannot judge them. They are never shown as new and
      stay under Found on the Pipeline board.</div>` : ""}
  </div>`;
}

/* Refresh the Fresh view's DATA regions in place — counts, results, the scan
   slab, the passed over trail — leaving the inputs and the company checkbox
   list untouched. This is what a tick or a typed query calls instead of a
   full renderPane(): the element under the pointer is never destroyed, so a
   click can never be lost to its own consequences, and the caret never needs
   "restoring" because nothing happened to it. */
function freshRefresh(withList = false) {
  if (state.tab !== "fresh" || !state.fresh || state.fresh.error) return;
  const f = state.fresh;
  const byPk = freshByPk();
  const win = $("#frail-win");
  if (win) win.innerHTML = freshWinRows(f, byPk);
  const st = $("#frail-st");
  if (st) st.innerHTML = freshStRows(f, byPk);
  const hn = $("#frail-hn");
  if (hn) hn.textContent = freshTickSum();
  if (withList) {  // company counts follow the results search; ticks don't move them
    const fl = $("#fs-list");
    if (fl) { const keep = fl.scrollTop; fl.innerHTML = freshScanList(); fl.scrollTop = keep; }
  }
  const foot = $("#fs-foot");
  if (foot) foot.innerHTML = freshScanFoot();
  const sh = $("#fr-showing");
  if (sh) sh.innerHTML = freshShowingHtml(f, byPk);
  const days = $("#fr-days");
  if (days) days.innerHTML = freshDaysHtml(f, byPk);
  const po = $("#po-host");
  if (po) po.innerHTML = passedOverPanel();
}

function viewFresh() {
  const f = state.fresh;
  if (!f) { loadFresh(); return `<div class="empty">Loading fresh postings…</div>`; }
  if (f.error) {
    return `<div class="empty"><div class="empty-big">No fresh view to show</div>
      Could not reach the backend. Try refresh once the daemon is running.</div>`;
  }
  const byPk = freshByPk();
  return `${freshStats(f)}
    <div class="fresh-layout">
      <aside class="fresh-rail">${freshRail(f, byPk)}</aside>
      <div class="fresh-main">
        ${freshResults(f, byPk)}
        <div id="po-host">${passedOverPanel()}</div>
      </div>
    </div>`;
}

/* ── Repaint discipline ─────────────────────────────────────────────────────
   renderPane() rebuilds the pane's DOM wholesale and is for USER actions —
   a tab switch, a facet click, a search — where state just changed and the
   immediate repaint is the point of the click.

   Background work (the stats poll, SSE driven reloads, the 30s safety
   refresh) goes through refreshPane() instead. It compares a cheap signature
   of the DATA the current tab renders against the last render and does
   NOTHING when they match. That is the fix for the lost-click bug: between
   genuine data changes the pane DOM is never torn down, so a checkbox, a
   caret or a scroll position cannot be destroyed by a timer. UI-only state
   (facets, folds, queries) stays out of the signature because changing it
   always arrives through a direct renderPane() call. The live activity lines
   are out too: renderLiveLine() updates them in place. */

function appsSig() {
  let s = "";
  for (const a of state.apps) {
    s += a.pk + "\u0001" + a.status + "\u0001" + (a.updated_at || "") + "\u0001"
       + (a.gate_reason || "") + (a.gate_question ? "?" : "") + "\u0001"
       + (a.match_score ?? "") + "\u0001" + (a.tailored_at || "") + "\u0001"
       + (a.profile_id || "") + "\n";
  }
  return s;
}

function paneSig() {
  // A coarse clock so relative ages ("3m ago") refresh eventually even when
  // nothing else moves: at most one background repaint every five minutes.
  const parts = [state.tab, Math.floor(Date.now() / 300000), appsSig()];
  if (state.tab === "logs") {
    const e0 = state.events[0] || {};
    parts.push(state.events.length, e0.at || "", e0.kind || "");
  } else if (state.tab === "activity") {
    const h = state.heat || {};
    parts.push((h.days || []).length, JSON.stringify(h.totals || null), h.error ? "e" : "");
  } else if (state.tab === "fresh") {
    const f = state.fresh || {};
    parts.push((f.jobs || []).map((j) => j.pk).join(","), f.undated ?? "",
      f.error ? "e" : "", state.freshNote,
      (state.passed && state.passed.count) || 0,
      ((state.passed && state.passed.companies) || [])
        .map((g) => (g.company || "") + (g.total || 0)).join(","),
      state.companies.join(","));
  } else {
    // The pipeline stack renders the apply queue inline. The plain app tables
    // ride on appsSig; these extra parts simply never differ for them.
    const q = state.queue || {};
    parts.push((q.pending || []).map((it) => it.pk + (it.blocked ? "b" : "")).join(","),
      (q.running || []).join(","), q.concurrency || 0);
  }
  return parts.join("\u0002");
}

let _paneSig = null;      // signature of the data behind the last pane render
let _paneDirty = false;   // a background repaint arrived while a press was down
let _pressAt = 0;         // when the current pointer press began (0 = none)

function refreshPane() {
  // A press in flight: pointerdown has landed on an element and its click has
  // not been delivered yet. Rebuilding now would destroy the press target and
  // eat the click — the exact bug this exists to prevent — so hold the repaint
  // until the press resolves (wire() releases it after the click lands).
  if (_pressAt && Date.now() - _pressAt < 1500) { _paneDirty = true; return; }
  if (paneSig() === _paneSig) return;  // nothing this tab renders has changed
  renderPane();
}

function renderPane() {
  // The preservation below is now RARE-PATH insurance, not a per-poll crutch:
  // background repaints only reach here when data genuinely changed (a scan
  // landing rows every few seconds is the common case), and that can still
  // happen while the owner is typing an answer or a filter. Losing a
  // half-typed gate answer to a legitimate repaint is not acceptable either,
  // so the courtesy stays for the rebuilds that remain.
  const saved = {};
  let focusPk = null;
  $$("#pane textarea[data-answer-for]").forEach((t) => {
    if (t.value) saved[t.dataset.answerFor] = t.value;
    if (document.activeElement === t) focusPk = t.dataset.answerFor;
  });
  const paneInputs = ["fs-search", "fr-search"];
  const liveId = document.activeElement && paneInputs.includes(document.activeElement.id)
    ? document.activeElement.id : "";
  const liveCaret = liveId ? document.activeElement.selectionStart : 0;
  const fsListEl = $("#fs-list");
  const fsScroll = fsListEl ? fsListEl.scrollTop : 0;
  $("#pane").innerHTML =
    state.tab === "apps" ? viewApps() :
    state.tab === "needs" ? viewNeeds() :
    state.tab === "stuck" ? viewStuck() :
    state.tab === "logs" ? viewLogs() :
    state.tab === "fresh" ? viewFresh() :
    state.tab === "activity" ? viewActivity() : viewPipeline();
  for (const [pk, v] of Object.entries(saved)) {
    const t = $(`#pane textarea[data-answer-for="${CSS.escape(pk)}"]`);
    if (t) t.value = v;
  }
  if (focusPk) {
    const t = $(`#pane textarea[data-answer-for="${CSS.escape(focusPk)}"]`);
    if (t) { t.focus(); t.selectionStart = t.selectionEnd = t.value.length; }
  }
  if (liveId) {
    const t = $("#" + liveId);
    if (t) { t.focus(); try { t.selectionStart = t.selectionEnd = liveCaret; } catch { /* search inputs vary */ } }
  }
  if (fsScroll) {
    const fl = $("#fs-list");
    if (fl) fl.scrollTop = fsScroll;
  }
  // The heatmap's newest weeks matter most: when the grid overflows on a
  // narrow screen, land on today rather than a year ago.
  const hs = $("#pane .hm-scroll");
  if (hs) hs.scrollLeft = hs.scrollWidth;
  _paneSig = paneSig();
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
const _livePend = new Set();
function scheduleLive(pk) {
  // Only touch the board if this pk is an actively-working card on it.
  const a = state.apps.find((r) => r.pk === pk);
  if (!a || !["tailoring", "submitting"].includes(a.status)) return;
  _livePend.add(pk);
  if (_liveT) return;
  _liveT = setTimeout(() => {
    _liveT = null;
    const pks = [..._livePend];
    _livePend.clear();
    // The card's own line only, updated in place. This used to be a full
    // renderPane() — an every-half-second teardown of the whole pane during a
    // run, which is exactly when the owner is watching and clicking.
    if (!["apps", "needs", "stuck", "logs"].includes(state.tab)) pks.forEach(renderLiveLine);
  }, 500);
}

// Write one card's live step into its .kc-live line without rebuilding
// anything else. Same markup kcard() produces, so a later full render agrees.
function renderLiveLine(pk) {
  const a = state.apps.find((r) => r.pk === pk);
  const act = state.activity[pk];
  if (!a || !act || !act.detail || !["tailoring", "submitting"].includes(a.status)) return;
  const card = $(`#pane .kcard[data-open="${CSS.escape(pk)}"]`);
  if (!card) return;
  let el = card.querySelector(".kc-live");
  if (!el) {
    el = document.createElement("div");
    el.className = "kc-live";
    const foot = card.querySelector(".kc-foot");
    if (foot) card.insertBefore(el, foot); else card.appendChild(el);
  }
  el.innerHTML = `<span class="kc-live-dot"></span>${esc(act.detail.replace(/^[^\w]+/, "").slice(0, 70))}`;
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
        // NOT "running". That is progress on a job already on the board, and
        // scheduleLive() above already updates its line in place. Treating it as a
        // board change meant a full rebuild — a 650KB fetch and 800+ cards of
        // innerHTML — every couple of seconds during a run, which is most of a run,
        // so the page spent more time rebuilding than idle and nothing could be
        // clicked. These four are real transitions: a job appears, finishes, needs
        // you, or breaks.
        if (["discovered", "applied", "gate", "error"].includes(e.kind)) scheduleReload();
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
  trackScanning();
}

/* When each running scan was FIRST SEEN by this page. /stats says which
   companies are being crawled but not since when, so the strip times what it
   can honestly time: a scan observed starting gets a clock accurate to one
   poll; a scan already running when the page loaded gets no clock at all,
   because inventing a start it never saw would just be a lie with digits. */
const _scanSeen = new Map();  // lowercased name → first-seen ms (0 = unknown start)
let _scanBaselined = false;
function trackScanning() {
  const now = Date.now();
  const cur = new Set((state.stats.scanning || []).map((c) => String(c).toLowerCase()));
  for (const c of cur) if (!_scanSeen.has(c)) _scanSeen.set(c, _scanBaselined ? now : 0);
  for (const c of [..._scanSeen.keys()]) if (!cur.has(c)) _scanSeen.delete(c);
  _scanBaselined = true;
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
// The per-company pane shows the DEFAULT for every field the company has not
// overridden. Those defaults live behind the preferences endpoint, so fetch them
// once rather than making the pane say "not set" until the other panel is opened.
async function loadGlobalPrefs() {
  if (DEMO) return;
  try {
    const r = await fetch(api("/preferences"), { headers: auth.header() });
    state.prefs = (await r.json()) || {};
  } catch { /* the pane just shows "not set" until the panel is opened */ }
}

/* The Fresh tab's company selection, kept across reloads.

   A scan takes minutes, and losing the ticks partway through means no longer
   knowing what is being scanned. It lives in localStorage rather than only in
   memory so a reload mid scan does not erase the answer. */
const FRESH_PICK_KEY = "appliedin.freshpicked";

function saveFreshPicked() {
  try { localStorage.setItem(FRESH_PICK_KEY, JSON.stringify([...state.freshPicked])); }
  catch { /* private mode, or full — the selection simply does not survive */ }
}

function restoreFreshPicked() {
  try {
    const raw = JSON.parse(localStorage.getItem(FRESH_PICK_KEY) || "[]");
    if (Array.isArray(raw)) state.freshPicked = new Set(raw.filter((c) => typeof c === "string"));
  } catch { /* nothing saved, or unreadable */ }
}

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
      state.cprefs = r.prefs || {};
    } catch { /* backend away — fall back below */ }
    if (!state.companies.length) {
      state.companies = [...new Set(state.apps.map((a) => a.company).filter(Boolean))];
    }
  }
  // Alphabetical, case-insensitively. The watchlist arrives in the order it was
  // written to, which is the order companies were ADDED — fine at five, useless
  // at forty when you are hunting for one name.
  if (!Object.keys(state.prefs || {}).length) await loadGlobalPrefs();
  state.companies.sort((a, b) => String(a).localeCompare(String(b), undefined,
                                                        { sensitivity: "base" }));
  state.picked = new Set([...state.picked].filter((c) => state.companies.includes(c)));
  // The Fresh tab's own selection follows the same rule: names that left the
  // watchlist leave the set, so its count never promises a scan it cannot run.
  // Prune ONLY when we actually have a watchlist. A failed or empty /companies
  // response would otherwise wipe the selection silently, so the boxes you ticked
  // before a scan would clear themselves while it ran and you would lose track of
  // what was being scanned.
  if (state.companies.length) {
    state.freshPicked = new Set([...state.freshPicked].filter((c) => state.companies.includes(c)));
    saveFreshPicked();
  }
  renderPicker();
  renderSkipPicker();
  renderDiscoverLabel();
  renderCompanyOptions();   // keep the custom-role datalist in step
}

async function loadApps() {
  if (DEMO) return;
  try {
    const a = await fetch(api("/applications"), { headers: auth.header() }).then((r) => r.json());
    state.apps = (a.items || []).filter((r) => !String(r.pk || "").startsWith("meta#"));
    // The heatmap and the Fresh tab ride the same cadence, but only while on screen.
    if (state.tab === "activity") loadActivity();
    if (state.tab === "fresh") loadFresh();
    renderTabs(); refreshPane(); renderFooter(); scheduleFeed();
  } catch { /* transient — next poll wins */ }
}

// Poll /stats every ~3s: drives the running-state of the two buttons, the
// vitals and the scanning-now strip. When a run finishes, refresh the board
// once. The poll NEVER rebuilds the pane itself — its few in-pane concerns
// (the Fresh scan slab) are written in place, and anything bigger goes
// through refreshPane(), which repaints only on a genuine data change.
let _scanFootSig = null;
async function pollStats() {
  if (DEMO) return;
  try {
    const s = await fetch(api("/stats"), { headers: auth.header() }).then((r) => r.json());
    const was = !!(state.stats.discovering || state.stats.processing);
    const wasDisc = !!state.stats.discovering;
    const wasScanning = (state.stats.scanning || []).length;
    applyStats(s);
    renderDeck();
    // The Fresh tab's scan slab mirrors WHICH companies are busy. Written in
    // place when that set changes — the slab holds no input and no scroll, so
    // the rest of the pane need not be touched for it.
    const scanSig = (s.scanning || []).join(",");
    if (scanSig !== _scanFootSig) {
      _scanFootSig = scanSig;
      const foot = $("#fs-foot");
      if (foot) foot.innerHTML = freshScanFoot();
    }
    // A finished scan outlives its refusal note, so the note goes with it;
    // refreshPane picks the cleared note up (it is part of the pane signature).
    if (wasDisc !== !!s.discovering) {
      if (!s.discovering) state.freshNote = "";
      if (state.tab === "fresh") refreshPane();
    }
    // Same cadence as the rest of the poll, but only while the panel is open.
    const qpEl = $("#qpicker");
    if (qpEl && !qpEl.hidden) loadQueue();
    // The moment the last company finishes, so the strip can say so rather than
    // vanishing and leaving a four hour sweep with no visible outcome.
    if (wasScanning && !(s.scanning || []).length) noteScanFinished(wasScanning);
    // Only while it matters: during a run, and for the short window after one
    // ends when the results are still what you came back to look at. The strip
    // re-renders after the fetch too, so the receipt counts the fresh log.
    if ((s.scanning || []).length || _scanFinishedAt)
      loadScanLog().then(() => { renderScanResults(); renderScanNow(); });
    else renderScanResults();
    const is = !!(s.discovering || s.processing);
    if (was && !is) { loadApps(); loadQueue(); toast("Run finished — board updated."); }
    // A scan queued behind another one starts the moment ITS company frees.
    // Waiting on the global discovering flag held it until every company
    // everywhere fell silent — the same "one crawl blocks all" gate the per
    // company claims removed. The server refuses per company, so ask per company.
    if (state.pendingScan) {
      const busyNow = new Set((s.scanning || []).map((c) => String(c).toLowerCase()));
      if (!busyNow.has(String(state.pendingScan.co).toLowerCase())) {
        const { co, url } = state.pendingScan;
        state.pendingScan = null;
        renderPicker();
        toast(`Scanning ${co} with the new rules…`);
        runCompany(co, url, false, true);
      }
    }
  } catch { /* backend briefly away — keep last known state */ }
}

let _reloadTimer = null;
let _lastReload = 0;
const RELOAD_DEBOUNCE_MS = 1500;   // let a burst settle before rebuilding
const RELOAD_FLOOR_MS = 8000;      // and never rebuild more often than this

/* A full board rebuild is expensive and it BLOCKS the page while it runs. Two
   guards, because they stop different things: the debounce folds a burst of
   transitions into one rebuild, and the floor stops a steady drip of them from
   rebuilding continuously. Between rebuilds the board is not frozen — the live
   lines, the queue panel and the vitals each update on their own, far cheaper
   paths. */
function scheduleReload() {
  if (DEMO) return;
  clearTimeout(_reloadTimer);
  const wait = Math.max(RELOAD_DEBOUNCE_MS, RELOAD_FLOOR_MS - (Date.now() - _lastReload));
  _reloadTimer = setTimeout(() => {
    _lastReload = Date.now();
    loadApps();
    loadQueue();
  }, wait);
}

async function post(path, body) {
  try {
    const r = await fetch(api(path), {
      method: "POST",
      headers: { ...auth.header(), "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    // A 404/405 means the running daemon predates this route — the page has been
    // reloaded but the server has not been restarted. Saying so beats a generic
    // failure: it is the difference between "restart the daemon" and an hour
    // spent debugging a button that is fine.
    if (r.status === 404 || r.status === 405) {
      return { ok: false, error: "That action needs a daemon restart "
                               + "(./appliedin stop && ./appliedin start)." };
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok && !data.error) data.error = `The server returned ${r.status}.`;
    return data;
  } catch {
    toast("Request failed — is the backend running?");
    return null;
  }
}
/* Say so, permanently and in the header, when the numbers are invented.
   Demo mode only ever announced itself in a toast when you clicked something, so
   a page of synthetic figures was indistinguishable from a page of real ones —
   8678 jobs found reads exactly like a real total until you check it against the
   store. A number you might act on has to carry its own provenance. */
function markDemo() {
  const pill = $("#demo-pill");
  if (pill) pill.hidden = !DEMO;
  if (DEMO) document.title = "AppliedIn (sample data)";
}

function demoGuard() {
  if (DEMO) { toast("Demo mode — run the local backend to use actions."); return true; }
  return false;
}

// --- primary actions -------------------------------------------------------
// Starting a run is the end of the task a picker exists for, so close every open
// popover. Leaving one up covers the board you just told it to fill, and the run
// buttons live at module scope where the per-picker close helpers cannot reach.
function closeAllPickers() {
  document.querySelectorAll(".copicker:not([hidden])").forEach((el) => { el.hidden = true; });
  document.querySelectorAll("[aria-haspopup][aria-expanded='true']")
    .forEach((b) => b.setAttribute("aria-expanded", "false"));
  const co = document.getElementById("btn-companies");
  if (co) { co.setAttribute("aria-expanded", "false"); co.classList.remove("open"); }
}

// A run is already going. Offer to stop it and start the new one, because the
// usual reason for asking twice is that the parameters changed: the first run is
// scanning with the rules you have just replaced, so waiting for it is waiting
// for an answer you no longer want.
// Offer to stop whatever is in the way and start again. Retries ONCE: if the
// second attempt is still refused, something else is holding the guard and asking
// again would just reopen this dialog forever, which is what happened when the
// stop was scoped to discovery and a PROCESS run was the actual blocker.
// Changing a company's rules in Discover should make Discover act on them, so a
// scan follows the edit. Debounced rather than immediate: the fields commit on
// blur, so tabbing through four of them would otherwise launch four crawls of the
// same careers page. One scan, after the edits stop.
let _rescanTimer = null;
function scheduleRescan(co, url = "", waiting = false) {
  clearTimeout(_rescanTimer);
  if (waiting) {
    // Another company is already scanning. Discovery runs one at a time, so hold
    // this one and let the stats poll start it the moment that finishes. It is
    // remembered rather than retried, so editing rules while something else scans
    // is a thing you can just do.
    state.pendingScan = { co, url };
    renderPicker();
    toast(`${co}: rules saved. It scans as soon as the current one finishes.`);
    return;
  }
  // Editing four fields commits four times; one scan once the edits settle.
  _rescanTimer = setTimeout(() => {
    toast(`Scanning ${co} with the new rules…`);
    runCompany(co, url, false, true);      // silent: never prompts
  }, 2500);
}

async function offerRestart(label, retry, blockedBy = "discover", again = false) {
  if (again) {
    toast("Still busy — something else is running. Try again in a moment.");
    return;
  }
  const what = blockedBy === "process" ? "process" : "discover";
  const name = what === "process" ? "Processing" : "Discovery";
  if (!confirm(`${name} is already running.\n\nStop it and start ${label.toLowerCase()} with your current settings?`)) {
    toast("Left the current run going.");
    return;
  }
  if (!await stopRun(what, false)) return;
  setTimeout(() => retry(true), 800);   // let both guards clear first
}

async function runDiscover(again = false) {
  // Same reasoning as runFreshScan: `discovering` is true whenever ANY company is
  // being scanned, and scans are per company now, so bailing here made one running
  // crawl block every other company from being started. The server refuses only
  // the companies genuinely mid scan, so drop those and send the rest.
  if (demoGuard()) return;
  closeAllPickers();
  const busy = new Set((state.stats.scanning || []).map((c) => String(c).toLowerCase()));
  const wanted = discoverScope();
  const scope = Array.isArray(wanted)
    ? wanted.filter((c) => !busy.has(String(c).toLowerCase()))
    : wanted;
  if (Array.isArray(wanted) && wanted.length && !scope.length) {
    toast("Those companies are already being scanned.");
    return;
  }
  // An un-scoped run claims whatever is free — but when NOTHING is free the
  // server logs a skip and this side would still toast "Discovery started",
  // promising a run that never happened. Refuse honestly instead.
  if (Array.isArray(wanted) && !wanted.length && busy.size) {
    const everyCo = state.companies.filter((c) => !state.skipped.has(String(c).toLowerCase()));
    if (everyCo.length && everyCo.every((c) => busy.has(String(c).toLowerCase()))) {
      toast("Every company is already being scanned.");
      return;
    }
  }
  // The picker's window rides along per run: `hours` bounds this scan to roles
  // published within it, and is omitted for "Any age" so the payload — and the
  // behaviour — stay exactly what they were before the window existed.
  const hours = Number(state.scanHours) || 0;
  state.stats.discovering = true;   // optimistic; poll confirms
  renderDeck();
  const profile = ($("#cp-profile") || {}).value || "";
  const body = { companies: scope, profile_id: profile };
  if (hours) body.hours = hours;
  const d = await post("/actions/discover", body);
  const winNote = hours ? ` Only postings from ${scanWindowText(hours)} count.` : "";
  if (d && d.status === "already_running") offerRestart("the scan", () => runDiscover(true), d.blocked_by, again);
  else if (d && d.ok && profile) {
    const p = state.profiles.find((x) => x.id === profile);
    toast(`Discovering. Everything found will apply as ${p ? p.label : profile}.${winNote}`);
  } else if (d && d.ok) toast((scope.length
    ? `Discovery started. Scanning ${scope.length <= 3 ? scope.join(", ") : `${scope.length} companies`}.`
    : "Discovery started. Scanning the whole watchlist.") + winNote);
  else if (!d) { state.stats.discovering = false; renderDeck(); }
  pollStats();
}

/* The Fresh tab's Run. Same endpoint and payload shape as Discover, scoped by
   the tab's own selection and bounded by the tab's window — so what lands below
   is exactly what was asked for. A refusal (every company asked for is mid scan
   already) is said in the panel, next to the button that was pressed, not only
   in a toast that may already have faded. */
async function runFreshScan() {
  // NOT gated on state.stats.discovering. That flag is true whenever any company
  // anywhere is being scanned, so with Apple mid crawl this returned instantly and
  // ticking Adobe did nothing at all — while the button, which had already been
  // fixed to gate per company, still read "Scan 1 company". A control that looks
  // live and silently does nothing is worse than one that is visibly disabled.
  // The per company filtering below is the real guard.
  if (demoGuard()) return;
  // Drop the ones already being scanned rather than sending them. The server
  // refuses a company that is mid scan, and sending a mixed list would have the
  // whole press read as a refusal when most of it was startable.
  const busy = new Set((state.stats.scanning || []).map((c) => String(c).toLowerCase()));
  const all = [...state.freshPicked].filter((c) => state.companies.includes(c));
  const companies = all.filter((c) => !busy.has(c.toLowerCase()));
  if (!all.length) { toast("Pick at least one company first."); return; }
  if (!companies.length) { toast("Those companies are already being scanned."); return; }
  if (companies.length < all.length) {
    toast(`Starting ${companies.length}. The rest are already scanning.`);
  }
  const hours = Number(state.freshHours) || 0;
  state.freshNote = "";
  state.stats.discovering = true;   // optimistic; poll confirms
  renderDeck(); renderPane();
  const d = await post("/actions/discover", { companies, profile_id: "", hours });
  if (d && d.status === "already_running") {
    const who = Array.isArray(d.companies) && d.companies.length ? d.companies : companies;
    state.freshNote = (who.length <= 3
      ? `${who.join(", ")} ${who.length === 1 ? "is" : "are"}`
      : `Those ${who.length} companies are`)
      + " already being scanned. Wait for that scan to finish, or press Stop scan"
      + " in the top bar, then run again.";
    toast("Already scanning those companies.");
    renderPane();
  } else if (d && d.ok) {
    toast(`Scanning ${companies.length <= 3 ? companies.join(", ")
      : `${companies.length} companies`} for roles from ${scanWindowText(hours)}.`
      + ` Expect ${freshEta(companies.length)}.`);
  } else {
    if (d && d.error) toast(d.error);
    state.stats.discovering = false;
    renderDeck(); renderPane();
  }
  pollStats();
}

async function runCompany(name, careersUrl, again = false, silent = false) {
  if (demoGuard()) return;
  closeAllPickers();
  const d = await post("/actions/run-company", { name, careers_url: careersUrl || "" });
  if (d && d.status === "already_running") {
    // A scan the OWNER asked for may interrupt to ask. One this scheduled itself
    // after a rules edit must not: editing four fields would mean four dialogs,
    // and the answer to all of them is the same. It waits and tries again.
    if (silent) scheduleRescan(name, careersUrl, true);
    else offerRestart("the scan", (a) => runCompany(name, careersUrl, a), d.blocked_by, again);
  }
  else if (d && d.ok) toast(`▶ ${name}: discover → score → tailor started. Tailored jobs will land on the board.`);
  else if (d) toast(d.error || "Could not start the run.");
  pollStats();
}

async function runProcess(again = false) {
  if (demoGuard() || state.stats.processing) return;
  closeAllPickers();
  const n = state.stats.found_waiting ?? 0;
  const scope = discoverScope();  // same picked companies as Discover
  state.stats.processing = true;    // optimistic; poll confirms
  renderDeck();
  const d = await post("/actions/process", { companies: scope });
  if (d && d.status === "already_running") offerRestart("processing", (a) => runProcess(a), d.blocked_by, again);
  else if (d && d.ok) toast(scope.length
    ? `Processing ${scope.length <= 3 ? scope.join(", ") : `${scope.length} companies`} only — score · tailor · apply.`
    : n ? `Processing ${n} waiting job${n === 1 ? "" : "s"} — score · tailor · apply.`
        : "Processing run started.");
  else if (!d) { state.stats.processing = false; renderDeck(); }
  pollStats();
}

// Optimistically flip a card to a working state so a click is acknowledged in the
// same frame. The server's own status/event overwrites this as soon as it lands;
// if the request fails, the next loadApps() puts the real status back.
/* A job the owner just sent to be scored and tailored. "tailoring" is a real
   pipeline status that the board already files under Ready to apply, so the card
   leaves Found the instant it is clicked and cannot be started twice. The server
   overwrites this on its next poll, so a failed start corrects itself. */
function markTailoring(pk) {
  const row = state.apps.find((a) => a.pk === pk);
  if (row) row.status = "tailoring";
  state.activity[pk] = { detail: "scoring and tailoring…", at: Date.now() / 1000,
                         kind: "running" };
  renderPane();
  scheduleLive(pk);
}

function markBusy(pk, detail) {
  const row = state.apps.find((a) => a.pk === pk);
  if (row) row.status = "submitting";
  state.activity[pk] = { detail, at: Date.now() / 1000, kind: "running" };
  renderPane();        // move the card into its working state
  scheduleLive(pk);    // and show the live line, same path the SSE feed uses
}

function paneAction(act, pk, el) {
  if (demoGuard()) return;
  if (act === "answer") {
    const t = $(`#pane textarea[data-answer-for="${CSS.escape(pk)}"]`);
    const answer = (t?.value || "").trim() || "approved";
    // Reload the queue as soon as the approve lands, so the card LEAVES "Ready to
    // apply" and reappears under Queued with its position. Without that refresh the
    // row sits exactly where it was until the next poll and the click reads as dead.
    markBusy(pk, "queued…");
    post(`/actions/resume/${encodeURIComponent(pk)}`, { answer })
      .then(() => loadQueue());
    toast("Queued — it starts when this company is free.");
  } else if (act === "queue-apply") {
    markBusy(pk, "queued…");
    post(`/actions/queue-apply/${encodeURIComponent(pk)}`).then(() => loadQueue());
    toast("Queued — it applies when a browser lane is free.");
  } else if (act === "rot-co") {
    // Ends at the queue, deliberately. Queueing is reversible and reviewable;
    // starting half an hour of browser sessions is the next button along.
    const co = (el && el.dataset.company) || "";
    if (demoGuard()) return;
    el.disabled = true;
    el.classList.add("is-busy");
    el.textContent = "↻ rotating…";
    post("/actions/rotate-and-approve", { company: co }).then((r) => {
      el.disabled = false;
      el.classList.remove("is-busy");
      el.textContent = "↻ Rotate & queue";
      toast(r && r.ok
        ? `${co}: ${r.queued} queued, applying as ${r.email} onward${
            r.tailoring ? ` · ${r.tailoring} tailoring first` : ""} — hit Process to run them.`
        : (r && r.error) || "Could not rotate that company.");
      loadQueue(); loadApps();
    });
  } else if (act === "drain-co") {
    // From the button's own dataset: paneAction is handed the element, not the
    // event, so there is no e.target here.
    const co = (el && el.dataset.company) || "";
    post("/actions/drain-company", { company: co }).then((r) => {
      toast(r && r.ok
        ? `Working through ${co} — ${r.queued} queued, one at a time.`
        : (r && r.error) || "Could not start it.");
      loadQueue();
    });
  } else if (act === "apply-now") {
    markBusy(pk, "starting…");
    post(`/actions/apply-now/${encodeURIComponent(pk)}`).then((r) => {
      toast(r && r.ok ? "Applying to this one now."
                      : (r && r.error) || "Could not start it.");
      loadQueue(); loadApps();
    });
  } else if (act === "qsel-bulk") {
    // Skip and Remove, over a tick list. Each job still goes through the SAME
    // endpoint one at a time — a bulk button is a convenience over the existing
    // action, never a second path that could disagree with it about what
    // skipping means.
    const pks = [...state.qPicked];
    if (!pks.length) return;
    const skip = el.dataset.mode === "skip";
    if (skip && !confirm(`Skip ${pks.length} job${pks.length === 1 ? "" : "s"}?`
        + `\n\nThey leave the queue and move to closed. Removing instead keeps`
        + ` them on the board under Ready to apply.`)) return;
    el.disabled = true;
    el.classList.add("is-busy");
    const url = (pk) => skip ? `/actions/skip/${encodeURIComponent(pk)}`
                             : `/actions/queue-remove/${encodeURIComponent(pk)}`;
    Promise.all(pks.map((pk) => post(url(pk)).catch(() => null))).then((rs) => {
      const ok = rs.filter((r) => r && (r.ok || r.note)).length;
      state.qPicked.clear();
      toast(`${ok} of ${pks.length} ${skip ? "skipped" : "taken out of the queue"}`
          + (ok < pks.length ? " — the rest could not be, and are still listed." : "."));
      loadQueue(); loadApps();
    });
  } else if (act === "queue-remove") {
    post(`/actions/queue-remove/${encodeURIComponent(pk)}`).then((r) => {
      toast(r && r.ok ? "Out of the queue — still on the board under Ready to apply."
                      : (r && r.error) || "Could not remove it.");
      loadQueue(); loadApps();
    });
  } else if (act === "queue-skip") {
    // Confirmed because it is the one that closes the job, not just unqueues it.
    if (!confirm("Skip this job? It leaves the queue and moves to closed.")) return;
    post(`/actions/skip/${encodeURIComponent(pk)}`).then((r) => {
      toast(r && r.ok ? "Skipped and removed from the queue."
                      : (r && r.note) || "Couldn't skip it.");
      loadQueue(); loadApps();
    });
  } else if (act === "skip") {
    if (!confirm("Skip this job? It moves to closed.")) return;
    // The response is READ. It used to say "Skipped." unconditionally, so a
    // refusal — an application already sent, or one mid-submit — looked exactly
    // like a skip that worked, and the card said something else afterwards.
    post(`/actions/skip/${encodeURIComponent(pk)}`).then((r) => {
      toast(r && r.ok ? "Skipped." : (r && r.note) || "Couldn't skip it.");
      loadApps();
    });
  } else if (act === "retry") {
    post(`/actions/retry/${encodeURIComponent(pk)}`);
    toast("Retrying — re-running the pipeline for this job.");
  } else if (act === "mark-applied") {
    post(`/actions/mark-applied/${encodeURIComponent(pk)}`);
    toast("Marked applied — won't resubmit.");
  } else if (act === "reopen") {
    post(`/actions/reopen/${encodeURIComponent(pk)}`).then((d) => {
      toast(d && d.ok
        ? `Back in play (was ${d.was || "closed"}) — scoring it again now.`
        : (d && d.note) || "Couldn't reopen it.");
      loadApps();
    });
  }
  scheduleReload();
}

// Stops the SCAN only. An application already being filled is left alone: the
// crawl is disposable, the apply is a form half completed under a real name.
async function stopRun(what = "discover", ask = true) {
  if (demoGuard()) return false;
  if (ask && !confirm("Stop the scan in progress?\n\nAnything already found is kept, and an application being filled is not touched.")) return false;
  const d = await post("/actions/stop-run", { what });
  if (!d || !d.ok) { toast("Could not stop the run."); return false; }
  toast(d.sessions_killed ? `Stopped — ${d.sessions_killed} browser session(s) ended.` : "Stopped.");
  if (what !== "process") state.stats.discovering = false;
  if (what !== "discover") state.stats.processing = false;
  renderDeck();
  pollStats();
  return true;
}

/* The confirm has to state the number the SERVER will queue, not a smaller one.
   This counted only needs_human rows awaiting approval while /actions/approve-all
   also takes every TAILORED row, so the dialog offered 11 and the press queued
   328. On a board in auto mode that difference is 300 applications sent under a
   real name from a dialog that named a tenth of them. The rule below mirrors
   server.py's selection exactly; if one moves, the other has to move with it. */
const approveAllPicks = (a) =>
  a.status === "tailored"
  || (a.status === "needs_human"
      && (a.gate_reason === "approval"
          || String(a.gate_question || "").startsWith("Ready to apply")));

function approveAll() {
  if (demoGuard()) return;
  const picks = state.apps.filter(approveAllPicks);
  const n = picks.length;
  if (!n) { toast("Nothing is waiting for approval."); return; }
  // Gated is a waiting room: the worker only drains the queue in auto mode, so
  // promising that applications are running would be false in the default mode.
  const after = state.mode === "auto"
    ? "They will be applied for a few at a time — finish any CAPTCHA windows as they open."
    : "They go to the apply queue and wait. Nothing is submitted until you run them.";
  if (!confirm(`Approve ${n} job${n === 1 ? "" : "s"}?\n\n${after}`)) return;
  picks.forEach((a) => markBusy(a.pk, "queued…"));
  post("/actions/approve-all", { company: "__all__" }).then((d) => {
    loadQueue();
    if (d && d.ok) {
      toast(`Queued ${d.queued}${d.already_queued ? `, ${d.already_queued} already there` : ""}.`
        + (state.mode === "auto" ? " Applying now." : " Waiting for you to run them."));
    } else {
      toast((d && d.error) || "Approve all failed — nothing was queued. See the logs.");
    }
  });
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
  // A closed job that was never sent can go back in play. It is offered on the
  // skipped ones especially: a role skipped as a low score under preferences you
  // have since fixed had no way back, and "Run now" silently refused it.
  const canReopen = !["applied", "applied_manual", "submitting"].includes(r.status)
                    && !r.confirmation_id
                    && ["skipped", "failed", "error", "job_gone", "capped"].includes(r.status);
  const closed = r.closed_reason ? `
    <div class="section closed-box">
      <div class="section-t">why it ${canRetry ? "failed" : "closed"}</div>
      <div class="closed-why">${esc(r.closed_reason)}</div>
      ${canRetry || canReopen ? `<div class="drawer-actions">
        ${canRetry ? `<button class="btn btn-primary" data-act="retry" data-pk="${esc(r.pk)}">Retry</button>` : ""}
        ${canReopen ? `<button class="btn btn-ghost" data-act="reopen" data-pk="${esc(r.pk)}"
          title="Put it back in play and score it again from scratch, using your preferences as they are now. Nothing is submitted.">↺ Reopen &amp; re-score</button>` : ""}
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

  const failure = (r.fail_reason || "").trim();
  const failBlock = failure && ["failed", "uncertain"].includes(r.status)
    ? `<div class="section drawer-fail"><div class="section-t">what went wrong</div>
         <div class="fail-text">${esc(failure)}</div></div>`
    : "";

  const jdText = (r.jd_text || "").trim();
  const jdBlock = jdText
    ? `<details class="section jd-block"><summary class="section-t">the posting${
         r.jd_url ? ` · <a href="${esc(r.jd_url)}" target="_blank" rel="noopener">open ↗</a>` : ""
       }</summary><div class="jd-text">${esc(jdText)}</div></details>`
    : "";

  $("#drawer-body").innerHTML = `
    ${gate}
    ${failBlock}
    ${jdBlock}
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

    <div class="section"><div class="section-t">applying as</div>
      <div class="profpick">
        <select id="job-profile" data-job-profile="${esc(r.pk)}">
          <option value=""${r.profile_id ? "" : " selected"}>default${
            state.profileDefault ? " — " + esc((state.profiles.find(p => p.id === state.profileDefault) || {}).label || "") : ""}</option>
          ${aliasById(r.profile_id) ? `<option value="${esc(r.profile_id)}" selected>rotating · ${
            esc(aliasById(r.profile_id).email)}</option>` : ""}
          ${state.profiles.filter((p) => p.kind !== "rotating")
            .map((p) => `<option value="${esc(p.id)}"${p.id === r.profile_id ? " selected" : ""}>${esc(p.label)} · ${esc(p.email)}</option>`).join("")}
        </select>
        <div class="profpick-note">Changing this re-tailors the résumé so its
          contact details match what gets typed into the form.${
          aliasById(r.profile_id) ? ` This address was minted for ${esc(r.company || "this company")}
            and is counted against its limit; picking another one here leaves it
            counted, because the slot is what protects the address.` : ""}</div>
      </div></div>

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
    <h1>Alex Rivera</h1>
    <div class="r-contact">Backend Engineer · alex.rivera@example.com · github.com/your-handle</div>
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

// --- apply queue -----------------------------------------------------------
// The queue panel answers its two questions with structure, not prose:
// "why has mine not started" — either every lane is busy, or its company
// already has one running, and each waiting row says which — and "what gave
// up" — the dead list, each with its reason and how many tries it got.
async function loadQueue() {
  if (DEMO) return;
  try {
    const [q, d] = await Promise.all([
      fetch(api("/apply-queue"), { headers: auth.header() }).then((r) => r.json()),
      fetch(api("/apply-queue/dead"), { headers: auth.header() }).then((r) => r.json()),
    ]);
    state.queue = q || {};
    state.dead = (d && d.dead) || [];
    renderQueueBadge();
    renderQueuePanel();
    refreshPane();   // the pipeline stack shows the queue inline; a real change repaints
    loadVerify();
  } catch { /* backend briefly away — keep last known state */ }
}

/* A session is sitting in a browser waiting for a one-time code, and it can only
   wait about nine minutes. That makes this the one prompt that cannot live in a
   list to be noticed later — it goes at the top of the board, with the box to
   answer it right there. */
function sendVerifyCode(pk) {
  if (!pk) return;
  const box = $(`#verify-bar .vf-in[data-vf-pk="${CSS.escape(pk)}"]`);
  const code = (box?.value || "").trim();
  if (!code) { toast("Type the code first."); return; }
  post("/actions/verify-code", { pk, code }).then((r) => {
    toast(r && r.ok ? "Sent — the browser picks it up on its next look."
                    : (r && r.error) || "Could not send it.");
    if (box) box.value = "";
    loadVerify();
  });
}

async function loadVerify() {
  if (DEMO) return;
  try {
    const r = await fetch(api("/verify-pending"), { headers: auth.header() })
      .then((x) => x.json());
    state.verifying = (r && r.waiting) || [];
  } catch { return; }
  renderVerify();
}

function renderVerify() {
  const host = $("#verify-bar");
  if (!host) return;
  // Answered waits stay visible while the session has not collected the code:
  // "I typed it and nothing happened" is exactly the state worth showing.
  const rows = (state.verifying || []).filter((v) => !v.answered || v.unseen_s > 90);
  if (!rows.length) { host.hidden = true; host.innerHTML = ""; return; }
  host.innerHTML = rows.map((v) => {
    const left = Math.max(0, 9 - Math.floor(v.waiting_s / 60));
    // Say whether the session is still LOOKING. A code that is ready but never
    // collected is the failure that reads as "I entered it and nothing happened",
    // and it needs the opposite response to waiting: start the job again.
    const gone = v.unseen_s > 90;
    const state = v.answered
      ? (gone ? "The code is ready but the browser has stopped checking for it."
              : "Code sent. The browser picks it up on its next look.")
      : (gone ? `No sign of the browser for ${Math.round(v.unseen_s)}s. It may have given up.`
              : `is waiting for the verification code it was emailed. About ${left} min left.`);
    return `<div class="vf-row${gone ? " vf-stale" : ""}">
      <span class="vf-dot"></span>
      <span class="vf-msg">${v.answered || gone ? "" : `<b>${esc(v.company || "An application")}</b> `}${state}</span>
      <input class="vf-in" type="text" inputmode="numeric" autocomplete="one-time-code"
        placeholder="code" data-vf-pk="${esc(v.pk)}" />
      <button class="vf-go" data-act="verify-send" data-pk="${esc(v.pk)}">Send</button>
    </div>`;
  }).join("");
  host.hidden = false;
}

function renderQueueBadge() {
  const q = state.queue || {};
  const n = (q.total || 0) + (q.dlq || 0);
  const b = $("#q-count");
  if (!b) return;
  b.hidden = !n;
  b.textContent = String(n);
  b.classList.toggle("dead", (q.dlq || 0) > 0);   // red only when something gave up
  $("#btn-queue").classList.toggle("live", (q.running || []).length > 0);
}

// Queue keys arrive lowercased; the watchlist knows the proper casing.
function queueCoName(c) {
  const lc = String(c).toLowerCase();
  return state.companies.find((x) => x.toLowerCase() === lc)
    || ((state.apps.find((a) => (a.company || "").toLowerCase() === lc) || {}).company)
    || c;
}

function renderQueuePanel() {
  const box = $("#q-body");
  if (!box) return;
  const q = state.queue || {};
  const cc = Math.min(6, Math.max(1, q.concurrency || 3));
  const maxTries = q.max_attempts || 3;
  const running = q.running || [];
  const queued = q.queued || {};
  const total = q.total ?? Object.values(queued).reduce((a, b) => a + b, 0);

  // The limit drawn as six lane slots: lit up to the cap, pulsing where an
  // application is running right now. Clicking a slot sets the cap to it, so
  // the control and the live picture are the same object.
  const lanes = Array.from({ length: 6 }, (_, i) => {
    const k = i + 1;
    const cls = k <= running.length ? " busy" : k <= cc ? " open" : "";
    return `<button type="button" class="q-lane${cls}" data-cc="${k}"
      aria-pressed="${k === cc}" title="Allow ${k} at once">${k}</button>`;
  }).join("");
  const paused = !!state.stats.paused;
  const busyChip = running.length
    ? `<span class="q-chip mono">${running.length} of ${cc} busy</span>`
    : paused && total
      ? `<span class="q-chip mono warn">paused</span>` : "";
  const laneSec = `<div class="q-sec">
    <div class="q-sec-h">Runs at once${busyChip}</div>
    <div class="q-lanes" role="group" aria-label="How many applications run at once">${lanes}</div>
    <div class="q-note">${paused && total
      ? `Nothing is running because the pipeline is <b>paused</b>. Unpause in the
         deck and these ${total} start straight away.`
      : `Up to <b>${cc}</b> run at the same time, never two at the same company.
         A new limit starts with the next application.`}</div>
  </div>`;

  // One row per company: running ones first with what queues behind them, then
  // waiting ones, each carrying the exact reason it has not started.
  const cos = [...new Set([...running, ...Object.keys(queued).filter((c) => queued[c] > 0)])];
  cos.sort((a, b) => {
    const ra = running.includes(a), rb = running.includes(b);
    if (ra !== rb) return ra ? -1 : 1;
    return (queued[b] || 0) - (queued[a] || 0) || String(a).localeCompare(String(b));
  });
  let freeLanes = Math.max(0, cc - running.length);
  const rows = cos.map((c) => {
    const n = queued[c] || 0;
    const isRun = running.includes(c);
    const name = esc(queueCoName(c));
    let stateHtml, why = "";
    if (isRun) {
      stateHtml = `<span class="q-state live">applying now</span>`;
      if (n) why = `<span class="q-why"
        title="One ${name} application runs at a time. The rest wait behind it.">${n} behind it</span>`;
    } else {
      stateHtml = `<span class="q-state mono">${n} waiting</span>`;
      if (freeLanes > 0) {
        freeLanes -= 1;
        why = `<span class="q-why next" title="A lane is free. It starts next.">starts next</span>`;
      } else {
        why = cc === 1
          ? `<span class="q-why" title="Starts when the lane opens.">the lane is busy</span>`
          : `<span class="q-why" title="Starts when one of the ${cc} lanes opens.">all ${cc} lanes busy</span>`;
      }
    }
    return `<div class="q-co${isRun ? " running" : ""}">
      <span class="q-dot"></span>
      <span class="q-co-name">${name}</span>
      ${stateHtml}${why}
    </div>`;
  }).join("");
  const queueSec = `<div class="q-sec">
    <div class="q-sec-h">Queue${total ? `<span class="q-chip mono">${total} waiting</span>` : ""}</div>
    ${cos.length ? `<div class="q-cos">${rows}</div>`
      : `<div class="q-idle">Nothing is queued or running. The Queue button on a
          tailored card lines an application up here.</div>`}
  </div>`;

  const deadRows = state.dead.map((d) => {
    const tries = d.attempts ?? maxTries;
    const hist = (d.history || [])
      .map((h) => `try ${h.attempt}: ${h.reason} (${when(h.at)})`).join("\n");
    return `<div class="q-dead">
      <div class="q-dead-m">
        <div class="q-dead-t"><span class="q-dead-co">${esc(d.company || d.pk)}</span>
          <span class="q-dead-role">${esc(d.title || "")}</span></div>
        <div class="q-dead-why">${esc(d.reason || "no reason recorded")}</div>
      </div>
      <span class="q-tries mono" title="${esc(hist || `${tries} tries`)}">${tries}×</span>
      <button class="q-revive" type="button" data-revive="${esc(d.pk)}"
        title="Clear the failures and put it back in the queue">Revive</button>
    </div>`;
  }).join("");
  const deadSec = state.dead.length ? `<div class="q-sec">
    <div class="q-sec-h">Gave up<span class="q-chip mono bad">${state.dead.length}</span>
      ${state.dead.length > 1 ? `<button class="cp-lk q-reviveall" type="button"
        data-revive-all="1" title="Put every one of these back in the queue">Revive all</button>` : ""}</div>
    <div class="q-deads">${deadRows}</div>
  </div>` : "";

  const keepScroll = box.scrollTop;   // 3s refreshes must not jump the list
  box.innerHTML = laneSec + queueSec + deadSec;
  box.scrollTop = keepScroll;
  const foot = $("#q-foot");
  if (foot) {
    // Stop lives here as well as in the deck. This panel is where you look when
    // you care about applications, and a control you have to go hunting for is
    // one you will not find while the thing you want stopped is running.
    foot.innerHTML = `<span class="q-foot-t">Environment failures, like a browser
      conflict or a timeout, retry with backoff, up to ${maxTries} tries. A
      failure about the application itself never retries.</span>`
      + (running.length
         ? `<button class="cp-lk cp-add-btn q-stop" id="q-stop" type="button"
              title="Stop the ${running.length} application(s) being filled now. A part filled form is left open in your browser."
              >Stop applying</button>`
         : "");
  }
}

async function setConcurrency(v) {
  if (demoGuard()) return;
  if (v === (state.queue || {}).concurrency) return;
  const d = await post("/actions/apply-concurrency", { value: v });
  if (d && d.ok) {
    state.queue = { ...(state.queue || {}), concurrency: d.concurrency };
    renderQueuePanel();
    toast(`Limit set to ${d.concurrency}. It takes effect with the next application.`);
  } else if (d) toast(d.error || "Could not change the limit.");
}

async function reviveDead(pk) {
  if (demoGuard()) return;
  const d = await post("/actions/apply-revive", pk ? { pk } : {});
  if (d && d.ok) {
    toast(d.revived
      ? `Revived ${d.revived === 1 ? "it" : d.revived}. Back in the queue.`
      : "Nothing to revive.");
    loadQueue();
    scheduleReload();
  } else if (d) toast(d.error || "Could not revive.");
}

// --- wiring ----------------------------------------------------------------
// Cap an open popover to the room actually below its button. Without this a
// tall one (job preferences) hangs past the bottom of the window and its Save
// button is unreachable — scrolling over the popover scrolls the list inside it,
// not the page. Call on open, and again on resize while it is open.
const fitPopover = (el) => {
  if (!el || el.hidden) return;
  el.style.removeProperty("--pop-max");           // measure unclamped
  const top = el.getBoundingClientRect().top;
  el.style.setProperty("--pop-max", `${Math.max(220, window.innerHeight - top - 12)}px`);
};
window.addEventListener("resize", () => {
  document.querySelectorAll(".copicker:not([hidden])").forEach(fitPopover);
});

function wire() {
  $("#btn-discover").addEventListener("click", runDiscover);
  $("#btn-stop").addEventListener("click", () => stopRun("discover"));
  $("#qpicker").addEventListener("click", (e) => {
    if (e.target.closest("#q-stop")) stopApplying();
  });
  $("#btn-stop-apply").addEventListener("click", stopApplying);
  async function stopApplying() {
    if (demoGuard()) return;
    // Spelled out rather than a generic "are you sure": the cost is a form left
    // part filled, and if it had already submitted the confirmation goes unread.
    if (!confirm("Stop the applications in progress?\n\n"
                 + "A form being filled is left open in your browser, unsubmitted. "
                 + "If one had already gone through, check the portal, because its "
                 + "confirmation will not be recorded.")) return;
    const d = await post("/actions/stop-run", { what: "apply" });
    if (!d || !d.ok) { toast("Could not stop the applications."); return; }
    toast(d.sessions_killed
      ? `Stopped ${d.sessions_killed} application(s). Check your browser tabs.`
      : "Nothing was in progress.");
    loadQueue && loadQueue();
    pollStats();
  }
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
    if (show) {
      fitPopover(picker);
      // The per-company pane offers an identity dropdown, and profiles are only
      // fetched when the Profiles panel is opened — so without this the dropdown
      // is empty until you happen to have visited that panel first.
      if (!state.profiles.length) loadProfiles().then(renderPicker);
      renderPicker();
      $("#cp-search").focus();
    }
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
  // The scan window chips. The choice is remembered like the theme is, so "I
  // only ever want fresh postings" survives a reload without becoming a saved
  // preference on any company.
  $("#cp-window").addEventListener("click", (e) => {
    const b = e.target.closest("[data-scan-w]");
    if (!b) return;
    state.scanHours = Number(b.dataset.scanW) || 0;
    localStorage.setItem("appliedin.scanw", String(state.scanHours));
    renderScanWindow(); renderPickerState(); renderDiscoverLabel();
  });
  // Add a company to the watchlist from the picker; the finder resolves its
  // ATS on the first discovery. The new company starts picked so "add → run
  // on just this one" is two clicks.
  const addCompany = async () => {
    if (demoGuard()) return;
    const name = $("#cp-add-name").value.trim();
    if (!name) { toast("Type a company name above first, or pick one in the list and use Scan now."); return; }
    const d = await post("/actions/watchlist",
                         { name, careers_url: $("#cp-add-url").value.trim() });
    if (d && d.ok) {
      $("#cp-add-name").value = ""; $("#cp-add-url").value = "";
      await loadCompanies();
      state.picked.add(name);
      renderPicker(); renderDiscoverLabel();
      closePicker();          // the company is added and selected — nothing left here
      toast(`${name} added to the watchlist and selected. Hit Discover to scan it.`);
    } else if (d) toast(d.error || "Could not add the company.");
  };
  $("#cp-add-btn").addEventListener("click", addCompany);
  $("#cp-add-run").addEventListener("click", async () => {
    if (demoGuard()) return;
    const name = $("#cp-add-name").value.trim();
    if (!name) { toast("Type a company name above first, or pick one in the list and use Scan now."); return; }
    const url = $("#cp-add-url").value.trim();
    $("#cp-add-name").value = ""; $("#cp-add-url").value = "";
    await runCompany(name, url);   // endpoint adds it to the watchlist if new
    await loadCompanies();
  });
  const commitOneFilter = async (inp) => {
    const name = inp.dataset.filterCo, titles = inp.value.trim();
    const was = (state.filters[name.toLowerCase()] || []).join(", ");
    if (titles === was) return;              // nothing typed — don't toast at them
    const d = await post("/actions/company-filter", { name, titles });
    if (d && d.ok) {
      state.filters = d.filters || {};
      toast(titles
        ? `${name}: only titles with "${titles}"${d.reconciled ? ` — ${d.reconciled} re-sorted` : ""}.`
        : `${name}: every title counts again.`);
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

  // Open a company's preference pane. One click, no mode to remember.
  $("#cp-list").addEventListener("click", (e) => {
    if (e.target.closest(".cp-skip") || e.target.tagName === "INPUT") return;
    // The row is the target, not the little chip on it. Clicking a company name
    // and getting nothing but a ticked checkbox was the whole confusion.
    const row = e.target.closest(".cp-item");
    const chip = row && row.querySelector("[data-detail]");
    if (!chip) return;
    e.preventDefault();
    const co = chip.dataset.detail;
    state.detailCo = state.detailCo === co ? null : co;
    renderPicker();
  });

  // Save on blur or Enter. The field was pre-filled with the DEFAULT when this
  // company had no opinion, so "unchanged" must mean "still shares the default"
  // and store nothing. Writing an override that merely copies today's default
  // would quietly detach the company: a later change to your defaults would move
  // every other company and leave this one behind, with nothing on screen saying
  // why. Typing the default back in is therefore also how you re-share it.
  const commitCpref = async (el) => {
    const co = el.dataset.co, f = CPREF_FIELDS.find((x) => x.k === el.dataset.cpref);
    if (!co || !f) return;
    const raw = el.value.trim();
    // "Apply as" carries both kinds of answer. A rotating choice is not a
    // per-company override — there is no one address to store — so it binds the
    // company instead, and picking anything else unbinds it.
    if (f.prof) {
      const bound = (state.rotation || []).some((r) => r.company === co.toLowerCase());
      if (raw.startsWith("rot:")) {
        const d = await post("/actions/rotation", { company: co, profile_id: raw.slice(4) });
        if (!d || !d.ok) { toast((d && d.error) || "Couldn't set that up."); return; }
        toast(`${co}: a new address every ${d.limit} applications.`);
        await loadRotation();
        renderPicker();
        return;
      }
      if (bound) {
        await post("/actions/rotation", { company: co, profile_id: "" });
        await loadRotation();
        toast(`${co} no longer rotates. Addresses already used are kept.`);
      }
    }
    const over = state.cprefs[co.toLowerCase()] || {};
    const shown = cpAsText(over[f.k] !== undefined ? over[f.k] : (state.prefs || {})[f.k], f);
    if (raw === shown) return;                                  // untouched
    const matchesDefault = raw === cpAsText((state.prefs || {})[f.k], f);
    const val = matchesDefault ? null : (f.bool ? raw === "yes" : raw);
    const d = await post("/actions/company-prefs",
                         { name: co, overrides: { [f.k]: val } });
    if (d && d.ok) {
      state.cprefs = d.prefs || {};
      const moved = d.rescreened
        ? ` ${d.rescreened} job${d.rescreened === 1 ? "" : "s"} re-screened.` : "";
      toast((matchesDefault
        ? `${co}: ${f.label.toLowerCase()} shared with everything else again.`
        : `${co}: ${f.label.toLowerCase()} now applies to ${co} only.`) + moved);
      renderPicker();
      if (d.rescreened) loadApps();
      scheduleRescan(co);          // the rules changed; go find what they match
    }
  };
  $("#cp-detail").addEventListener("keydown", (e) => {
    if (!e.target.classList.contains("cp-dt-in")) return;
    if (e.key === "Enter" && e.target.tagName !== "TEXTAREA") { e.preventDefault(); e.target.blur(); }
    if (e.key === "Escape") { state.detailCo = null; renderPicker(); }
  });
  $("#cp-detail").addEventListener("focusout", (e) => {
    if (e.target.classList.contains("cp-dt-in")) commitCpref(e.target);
  });
  $("#cp-detail").addEventListener("change", (e) => {
    if (e.target.tagName === "SELECT" && e.target.classList.contains("cp-dt-in")) commitCpref(e.target);
  });
  $("#cp-detail").addEventListener("click", async (e) => {
    // Rotate & approve: re-point what is in flight, then queue it. One press,
    // because doing the two halves separately queues jobs under the address
    // being retired.
    const rotgo = e.target.closest("#cp-dt-rotgo");
    if (rotgo) {
      if (demoGuard()) return;
      const company = rotgo.dataset.co;
      rotgo.disabled = true;
      rotgo.classList.add("is-busy");
      rotgo.textContent = "rotating…";
      const d = await post("/actions/rotate-and-approve", { company });
      rotgo.disabled = false;
      rotgo.classList.remove("is-busy");
      rotgo.textContent = "Rotate & queue";
      if (!d || !d.ok) { toast((d && d.error) || "Couldn't rotate that company."); return; }
      toast(`${company}: ${d.queued} queued as ${d.email}`
          + (d.tailoring ? ` · ${d.tailoring} tailoring now, queued when ready` : "")
          + (d.left ? ` · ${d.left} left for the next address` : "")
          + (d.blocked ? ` · ${d.blocked} still need a real answer` : ""));
      loadRotation(); loadApps();
      return;
    }
    if (e.target.closest("#cp-dt-scan")) {
      clearTimeout(_rescanTimer);        // scanning now; do not scan twice
      const co = state.detailCo;
      if (!co) return;
      // A field still focused has not been committed yet, and scanning with the
      // rules you just typed but did not blur is the worst kind of surprise:
      // it looks like the edit was ignored.
      const open = document.activeElement;
      if (open && open.classList && open.classList.contains("cp-dt-in")) {
        await commitCpref(open);
      }
      runCompany(co);
      return;
    }
    if (!e.target.closest("#cp-dt-reset")) return;
    const co = state.detailCo;
    if (!co) return;
    const d = await post("/actions/company-prefs", { name: co, overrides: {} });
    if (d && d.ok) {
      state.cprefs = d.prefs || {};
      toast(`${co}: shares your preferences again.`
            + (d.rescreened ? ` ${d.rescreened} job(s) re-screened.` : ""));
      renderPicker();
      if (d.rescreened) loadApps();
      scheduleRescan(co);
    }
  });

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
    // Cached data renders at once; the fetch refreshes it behind the paint.
    if (state.tab === "activity") loadActivity();
    if (state.tab === "fresh") loadFresh();
    renderTabs();
    renderPane();
  });

  const rp = $("#rolepicker"), rpBtn = $("#btn-role");
  const closeRp = () => { if (!rp.hidden) { rp.hidden = true; rpBtn.setAttribute("aria-expanded", "false"); } };
  const runRole = async () => {
    if (demoGuard()) return;
    const url = $("#rp-url").value.trim();
    if (!url) { toast("Paste the job URL first."); return; }
    // Naming the company matters beyond tidiness: it is the key the queue, the
    // per-company preferences and the board filter all work from. Guessed from
    // the URL when left blank, which is right for the big boards and wrong for
    // a vendor host that names the software rather than the employer.
    const company = chosenCompany();
    const d = await post("/actions/apply-role", { url, company });
    if (d && d.ok) {
      $("#rp-url").value = "";
      if ($("#rp-url-company")) $("#rp-url-company").value = "";
      if ($("#rp-url-newco")) { $("#rp-url-newco").value = ""; $("#rp-url-newco").hidden = true; }
      closeRp();
      toast("▶ Tailoring your résumé to that role — it'll land in Tailored when ready.");
      pollStats();
    } else if (d) toast(d.error || "Couldn't start — check the URL.");
  };
  // Tailor from PASTED text: a recruiter's message with the role in it, where
  // there is no posting to fetch and nothing to apply to at the end.
  const runRoleText = async () => {
    if (demoGuard()) return;
    const text = $("#rp-text").value.trim();
    if (text.length < 80) {
      toast("Paste more of the message — a line or two is not enough to tailor against.");
      return;
    }
    const d = await post("/actions/tailor-text", {
      text,
      company: $("#rp-company").value.trim(),
      title: $("#rp-title").value.trim(),
    });
    if (d && d.ok) {
      $("#rp-text").value = ""; $("#rp-company").value = ""; $("#rp-title").value = "";
      closeRp();
      toast(`▶ Tailoring a résumé for ${d.title} @ ${d.company} — it'll land in Tailored.`);
      pollStats();
    } else if (d) toast(d.error || "Couldn't start.");
  };

  const showRpTab = (which) => {
    const url = which === "url";
    $("#rp-pane-url").hidden = !url;
    $("#rp-pane-text").hidden = url;
    $("#rp-tab-url").classList.toggle("on", url);
    $("#rp-tab-text").classList.toggle("on", !url);
    fitPopover(rp);                       // the text pane is taller than the link one
    (url ? $("#rp-url") : $("#rp-text")).focus();
  };
  $("#rp-tab-url").addEventListener("click", () => showRpTab("url"));
  $("#rp-tab-text").addEventListener("click", () => showRpTab("text"));
  $("#rp-go-text").addEventListener("click", runRoleText);
  $("#rp-text").addEventListener("keydown", (e) => {
    // Enter belongs to the textarea; the shortcut has to be deliberate.
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); runRoleText(); }
  });

  rpBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const show = rp.hidden;
    rp.hidden = !show;
    rpBtn.setAttribute("aria-expanded", String(show));
    if (show) { fitPopover(rp); $("#rp-url").focus(); }
  });
  rp.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", (e) => { if (!rp.hidden && !e.target.closest(".rolemgr")) closeRp(); });
  $("#rp-url-company")?.addEventListener("change", toggleNewCompany);
  $("#rp-go").addEventListener("click", runRole);
  $("#rp-url").addEventListener("keydown", (e) => { if (e.key === "Enter") runRole(); });

  // --- profiles: which identity an application goes out under ---------------
  const pm = $("#profpicker"), pmBtn = $("#btn-profiles");
  const closePm = () => {
    if (!pm.hidden) { pm.hidden = true; pmBtn.setAttribute("aria-expanded", "false"); }
  };
  async function loadProfiles() {
    try {
      const r = await fetch(api("/profiles"), { headers: auth.header() });
      const d = (await r.json()) || {};
      state.profiles = d.profiles || [];
      state.profileDefault = d.default || "";
    } catch { state.profiles = []; }
    renderProfiles();
    renderProfilePickers();
    loadRotation();
  }
  function renderProfiles() {
    const list = $("#pr-list");
    const n = state.profiles.length;
    $("#prof-count").hidden = !n;
    $("#prof-count").textContent = String(n);
    if (!n) {
      list.innerHTML = `<div class="cp-none">No profiles yet — applications use
        whatever is in your facts.</div>`;
      return;
    }
    list.innerHTML = state.profiles.map((p) => {
      // A rotating profile is a template, not an identity: its own address never
      // reaches a form, so the two buttons that would SEND under it — "use for
      // all" and "default" — are the wrong offer and are not shown.
      const rot = p.kind === "rotating";
      return `
      <div class="pr-row${p.id === state.profileDefault ? " is-default" : ""}${rot ? " is-rot" : ""}">
        <div class="pr-main">
          <div class="pr-label">${esc(p.label)}${p.id === state.profileDefault
            ? '<span class="pr-tag">DEFAULT</span>' : ""}${rot
            ? `<span class="pr-tag rot">ROTATING · ${p.limit || 5} each</span>` : ""}</div>
          <div class="pr-meta" title="${esc(p.email)}${p.phone ? " · " + esc(p.phone) : ""}">${
            rot ? "base " : ""}${esc(p.email)}</div>
        </div>
        ${rot ? "" : `<button class="pr-act use" data-prof-all="${esc(p.id)}"
          title="Re-render every tailored résumé with this profile's details, ready to apply. No model is called.">use for all</button>`}
        ${rot || p.id === state.profileDefault ? "" :
          `<button class="pr-act" data-prof-default="${esc(p.id)}" title="Make default">default</button>`}
        <button class="pr-act del" data-prof-del="${esc(p.id)}" title="Remove">✕</button>
      </div>`;
    }).join("");
    renderRotation();
  }

  // --- rotation: which companies get an alias per application ---------------
  // The count shown is derived from the rows themselves, so it is what actually
  // happened rather than a number something remembered to update.
  async function loadRotation() {
    try {
      const r = await fetch(api("/rotation"), { headers: auth.header() });
      state.rotation = ((await r.json()) || {}).companies || [];
    } catch { state.rotation = []; }
    renderRotation();
  }
  function renderRotation() {
    const box = $("#pr-rot"), list = $("#pr-rot-list");
    const rows = state.rotation || [];
    const rotators = (state.profiles || []).filter((p) => p.kind === "rotating");
    // The section appears as soon as a rotating profile exists, not only once
    // some company already uses one — otherwise the control that turns the first
    // one on is hidden behind having turned one on.
    if (box) box.hidden = !rotators.length;
    const sel = $("#pr-rot-prof");
    if (sel) {
      sel.innerHTML = rotators
        .map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("");
      sel.hidden = rotators.length < 2;      // no choice to make with one
    }
    const dl = $("#pr-rot-cos");
    if (dl) {
      // Every watchlist company, as suggestions. The field stays free text, so a
      // company that is not on the watchlist can still be bound.
      const bound = new Set(rows.map((r) => r.company));
      dl.innerHTML = (state.companies || [])
        .map((c) => (c && c.name) || c).filter(Boolean)
        .filter((n) => !bound.has(String(n).toLowerCase()))
        .map((n) => `<option value="${esc(n)}"></option>`).join("");
    }
    if (!rotators.length) return;
    // The top-level control belongs to the same data, so it is kept in step here
    // rather than in a second place that could disagree with this one.
    const all = $("#btn-rotate-all");
    if (all) {
      all.hidden = !rows.length;
      const n = $("#rot-count");
      if (n) { n.hidden = !rows.length; n.textContent = String(rows.length); }
    }
    if (!list) return;
    if (!rows.length) {
      list.innerHTML = `<div class="cp-none">No company rotates yet. Name one below
        and every application to it gets its own address.</div>`;
      return;
    }
    list.innerHTML = rows.map((r) => `
      <div class="prr-row">
        <div class="pr-main">
          <div class="pr-label">${esc(r.company)}</div>
          <div class="pr-meta" title="${esc(r.style)} · ${r.minted} minted so far">${
            r.email ? esc(r.email) + ` · ${r.used}/${r.limit}`
                    : "first address on the next application"}</div>
        </div>
        <button class="pr-act" data-rot-retire="${esc(r.company)}"
          title="Retire this address so the next application starts on a fresh one. Use it when the board refused under its own cap while this address was still below the limit.">retire</button>
        <button class="pr-act del" data-rot-unbind="${esc(r.company)}"
          title="Stop rotating for this company. Addresses already used are kept.">✕</button>
      </div>`).join("");
  }
  function renderProfilePickers() {
    const sel = $("#cp-profile");
    if (!sel) return;
    const cur = sel.value;
    // Rotating profiles are absent by design: picking one would mean applying
    // under the base address, which is the one thing rotation exists to prevent.
    sel.innerHTML = `<option value="">default profile</option>` +
      state.profiles.filter((p) => p.kind !== "rotating")
        .map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("");
    sel.value = cur;
  }
  async function saveProfiles(profiles, def) {
    const d = await post("/profiles", { profiles, default: def });
    if (d && d.ok) { state.profiles = d.profiles; state.profileDefault = d.default;
                     renderProfiles(); renderProfilePickers(); }
    else toast((d && d.error) || "Couldn't save profiles.");
  }
  pmBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const show = pm.hidden;
    pm.hidden = !show;
    pmBtn.setAttribute("aria-expanded", String(show));
    if (show) { fitPopover(pm); loadProfiles(); }
  });
  pm.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", (e) => {
    if (!pm.hidden && !e.target.closest(".profmgr")) closePm();
  });
  loadProfiles();          // boot: the board needs these to label its cards

  $("#pr-add").addEventListener("click", () => {
    if (demoGuard()) return;
    const email = $("#pr-email").value.trim();
    if (!email) { toast("An email address is required."); return; }
    const label = $("#pr-label").value.trim() || email.split("@")[0];
    const rotating = ($("#pr-rotating") || {}).checked;
    const row = { id: label.toLowerCase().replace(/[^a-z0-9]+/g, "-"),
                  label, email, phone: $("#pr-phone").value.trim() };
    if (rotating) {
      row.kind = "rotating";
      row.limit = Number($("#pr-limit").value) || 5;
      row.style = ($("#pr-style") || {}).value || "plus";
    }
    const next = [...state.profiles, row];
    $("#pr-label").value = $("#pr-email").value = $("#pr-phone").value = "";
    if ($("#pr-rotating")) $("#pr-rotating").checked = false;
    if ($("#pr-add-form")) {           // fold it away again — the list is the point
      $("#pr-add-form").hidden = true;
      $("#pr-new").textContent = "＋ new";
      $("#pr-new").setAttribute("aria-expanded", "false");
    }
    // A rotating profile must never become the default — its base address is
    // exactly the one being kept off every form.
    const fallback = next.find((p) => p.kind !== "rotating");
    saveProfiles(next, state.profileDefault || (fallback || {}).id || "");
  });

  // Every rotating company, one press. It confirms first: it moves work for
  // several employers at once, and the count is the only honest way to say how
  // much before it happens.
  $("#btn-rotate-all")?.addEventListener("click", async () => {
    if (demoGuard()) return;
    const cos = (state.rotation || []).map((r) => r.company);
    if (!cos.length) return;
    if (!confirm(`Rotate and queue ${cos.length} compan${cos.length === 1 ? "y" : "ies"}`
               + ` — ${cos.join(", ")}?\n\nEvery un-sent job at these employers is`
               + ` re-pointed at that employer's rotating address, and each one with`
               + ` a résumé goes into the apply queue.\n\nNothing is applied: the`
               + ` queue is where you press Process.`)) return;
    // It re-points and queues across several employers, which takes seconds, and
    // a button that just sits there reads as a button that did nothing.
    const btn = $("#btn-rotate-all");
    const label = btn.firstChild;
    btn.disabled = true;
    btn.classList.add("is-busy");
    if (label) label.textContent = "↻ Rotating…";
    toast(`Rotating ${cos.length} compan${cos.length === 1 ? "y" : "ies"}…`);
    const d = await post("/actions/rotate-and-approve", { company: "__all__" });
    btn.disabled = false;
    btn.classList.remove("is-busy");
    if (label) label.textContent = "↻ Rotate & queue all";
    if (!d || !d.ok) { toast((d && d.error) || "Couldn't rotate."); return; }
    toast(`${d.queued} queued across ${d.companies} compan${
      d.companies === 1 ? "y" : "ies"}${d.tailoring ? ` · ${d.tailoring} tailoring first` : ""}`
      + " — hit Process on a company to run them.");
    loadRotation(); loadQueue(); loadApps();
  });

  // Bind ANY company from here — the datalist suggests the watchlist, but the
  // field is free text, so a company you have not tracked yet can be set up
  // before its first job is even discovered.
  const bindRotation = async () => {
    if (demoGuard()) return;
    const input = $("#pr-rot-co");
    const company = (input.value || "").trim();
    if (!company) { toast("Which company should rotate?"); return; }
    const profile = $("#pr-rot-prof").value
      || ((state.profiles || []).find((p) => p.kind === "rotating") || {}).id;
    if (!profile) { toast("Add a rotating profile first."); return; }
    const d = await post("/actions/rotation", {
      company, profile_id: profile, limit: Number($("#pr-rot-limit").value) || 0,
    });
    if (!d || !d.ok) { toast((d && d.error) || "Couldn't set that up."); return; }
    input.value = "";
    await loadRotation();
    toast(`${company}: a new address every ${d.limit} applications.`);
    renderPane();       // the board's company markers follow the binding
  };
  $("#pr-rot-bind")?.addEventListener("click", bindRotation);
  $("#pr-rot-co")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); bindRotation(); }
  });

  // The add form is folded away by default. With nine profiles in the list, a
  // permanently open four-field form is most of the panel, and reading which
  // address a job goes out under is what this panel is opened for.
  $("#pr-new")?.addEventListener("click", () => {
    const form = $("#pr-add-form");
    form.hidden = !form.hidden;
    $("#pr-new").setAttribute("aria-expanded", String(!form.hidden));
    $("#pr-new").textContent = form.hidden ? "＋ new" : "close";
    if (!form.hidden) $("#pr-label").focus();
  });

  pm.addEventListener("click", (e) => {
    const mk = e.target.closest("[data-prof-default]");
    if (mk) { saveProfiles(state.profiles, mk.dataset.profDefault); return; }
    const all = e.target.closest("[data-prof-all]");
    if (all) {
      if (demoGuard()) return;
      const p = state.profiles.find((x) => x.id === all.dataset.profAll) || {};
      if (!confirm(`Re-render every tailored résumé with ${p.label}'s details `
                 + `(${p.email})?\n\nThe tailoring itself is kept — only the contact `
                 + `line changes, so no model is called and nothing is re-written.`)) return;
      all.textContent = "re-rendering…";
      post("/actions/apply-profile-to-all", { profile_id: all.dataset.profAll })
        .then((d) => {
          all.textContent = "use for all";
          if (d && d.ok) {
            toast(`${d.rerendered} résumé(s) now go out as ${p.label}`
                + (d.left_alone ? ` · ${d.left_alone} already applied, left alone` : ""));
            reload();
          } else toast((d && d.error) || "Couldn't re-render.");
        });
      return;
    }
    const retire = e.target.closest("[data-rot-retire]");
    if (retire) {
      if (demoGuard()) return;
      const co = retire.dataset.rotRetire;
      if (!confirm(`Retire the address ${co} is applying under?\n\nThe next `
                 + `application there mints a fresh one. Nothing already sent `
                 + `changes, and the retired address stays on record.`)) return;
      post("/actions/rotation-retire", { company: co }).then((d) => {
        toast(d && d.ok ? `${co} will mint a new address next time.`
                        : "Nothing to retire there.");
        loadRotation();
      });
      return;
    }
    const unbind = e.target.closest("[data-rot-unbind]");
    if (unbind) {
      if (demoGuard()) return;
      const co = unbind.dataset.rotUnbind;
      post("/actions/rotation", { company: co, profile_id: "" }).then(() => {
        toast(`${co} no longer rotates. Addresses already used are kept.`);
        loadRotation();
      });
      return;
    }
    const del = e.target.closest("[data-prof-del]");
    if (del) {
      const keep = state.profiles.filter((p) => p.id !== del.dataset.profDel);
      saveProfiles(keep, keep.some((p) => p.id === state.profileDefault)
        ? state.profileDefault : (keep[0] || {}).id || "");
    }
  });

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
      state.prefs = p;                 // the per-company pane shows these as its
      renderDetail();                  // placeholders, i.e. what a blank inherits
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
      // Saving is the end of the task, so get out of the way. Leaving the panel
      // open over the board invites a second save of the same values.
      closePf();
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
    if (show) { fitPopover(pf); loadPrefs(); $("#pf-titles").focus(); }
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
    if (show) { fitPopover(sp); renderSkipPicker(); $("#sp-search").focus(); }
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

  // --- apply queue: who runs, who waits and why, what gave up ---
  const qp = $("#qpicker"), qBtn = $("#btn-queue");
  const closeQp = () => {
    if (!qp.hidden) { qp.hidden = true; qBtn.setAttribute("aria-expanded", "false"); }
  };
  qBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const show = qp.hidden;
    qp.hidden = !show;
    qBtn.setAttribute("aria-expanded", String(show));
    if (show) { renderQueuePanel(); fitPopover(qp); loadQueue(); }
  });
  qp.addEventListener("click", (e) => {
    e.stopPropagation();
    const lane = e.target.closest("[data-cc]");
    if (lane) { setConcurrency(Number(lane.dataset.cc)); return; }
    if (e.target.closest("[data-revive-all]")) { reviveDead(""); return; }
    const rv = e.target.closest("[data-revive]");
    if (rv) reviveDead(rv.dataset.revive);
  });
  document.addEventListener("click", (e) => {
    if (!qp.hidden && !e.target.closest(".qmgr")) closeQp();
  });

  $("#verify-bar").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act='verify-send']");
    if (btn) sendVerifyCode(btn.dataset.pk);
  });
  $("#verify-bar").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const box = e.target.closest(".vf-in");
    if (box) { e.preventDefault(); sendVerifyCode(box.dataset.vfPk); }
  });
  restoreFreshPicked();
  markDemo();
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
    if (state.tab === "activity") loadActivity();
    if (state.tab === "fresh") loadFresh();
    toast("Refreshed.");
  });

  // A press in flight (pointerdown seen, click not yet delivered) must never
  // have the pane rebuilt under it — that is how a click dies. refreshPane()
  // checks the hold; the release is queued a tick after pointerup so the
  // browser has dispatched the click (and any change event) before a deferred
  // repaint is allowed to run.
  $("#pane").addEventListener("pointerdown", () => { _pressAt = Date.now(); }, true);
  const releasePress = () => {
    if (!_pressAt) return;
    _pressAt = 0;
    if (_paneDirty) { _paneDirty = false; refreshPane(); }
  };
  window.addEventListener("pointerup", () => setTimeout(releasePress, 0));
  window.addEventListener("pointercancel", () => setTimeout(releasePress, 0));

  $("#pane").addEventListener("click", (e) => {
    if (e.target.closest("a")) return;                 // links behave as links
    if (e.target.closest("[data-clear-filters]")) {
      state.coFilter = ""; state.query = ""; state.logKind = "all";
      $("#co-filter").value = ""; $("#co-filter").classList.remove("on");
      $("#search").value = "";
      // The Fresh tab's own facets clear with the rest. Unticking the company
      // facet is part of it: the ticks and the scan scope are one control, and
      // "show me everything" untargets the next scan by the same gesture.
      if (state.tab === "fresh") {
        state.freshStatus = "";
        state.freshPicked.clear();
        saveFreshPicked();
      }
      renderPane();
      return;
    }
    // Pipeline stack: folds, company rollups, paging, and the jump to the
    // Needs you tab. All of them re-render only; none of them post anything.
    if (e.target.closest("[data-goto-needs]")) {
      state.tab = "needs";
      renderTabs(); renderPane();
      return;
    }
    if (e.target.closest("[data-goto-fresh]")) {
      state.freshHours = FRESH_BOARD_HOURS;   // the chip's claim IS the 2 day window
      state.tab = "fresh";
      loadFresh();
      renderTabs(); renderPane();
      return;
    }
    // The Fresh tab's window facet: a lens over data already in hand, so the
    // switch is a pure re-render — no fetch, no flash.
    const fw = e.target.closest("[data-fresh-w]");
    if (fw) {
      state.freshHours = Number(fw.dataset.freshW);
      localStorage.setItem("appliedin.freshw", String(state.freshHours));
      renderPane();
      return;
    }
    // The status facet: single select, and a second press on the selected row
    // clears it — the same gesture "Any status" performs explicitly.
    const fst = e.target.closest("[data-fresh-st]");
    if (fst) {
      const v = fst.dataset.freshSt || "";
      state.freshStatus = state.freshStatus === v ? "" : v;
      renderPane();
      return;
    }
    // The company facet's bulk controls and the Run itself. Ticks are data for
    // the counts, so refresh those in place; the list is rebuilt too (its
    // checkboxes must flip), which is safe because the click that got here
    // landed on a button OUTSIDE the list.
    if (e.target.closest("[data-fs-all]")) {
      freshScanCos().forEach((c) => state.freshPicked.add(c));
      saveFreshPicked();
      freshRefresh(true);
      return;
    }
    if (e.target.closest("[data-fs-none]")) {
      state.freshPicked.clear();
      saveFreshPicked();
      freshRefresh(true);
      return;
    }
    if (e.target.closest("[data-fs-run]")) { runFreshScan(); return; }
    const secFold = e.target.closest("[data-sec-fold]");
    if (secFold) {
      const k = secFold.dataset.secFold;
      state.secOpen[k] = !state.secOpen[k];
      renderPane();
      return;
    }
    const coFold = e.target.closest("[data-co-fold]");
    if (coFold) {
      const co = coFold.dataset.coFold;
      state.openCos.has(co) ? state.openCos.delete(co) : state.openCos.add(co);
      renderPane();
      return;
    }
    const qcoFold = e.target.closest("[data-qco-fold]");
    if (qcoFold) {
      const co = qcoFold.dataset.qcoFold;
      state.openQCos.has(co) ? state.openQCos.delete(co) : state.openQCos.add(co);
      renderPane();
      return;
    }
    const reveal = e.target.closest("[data-more]");
    if (reveal) {
      state.page[reveal.dataset.more] = Number(reveal.dataset.next);
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
    if (e.target.closest("[data-see-applied]")) {
      // Straight to the full record rather than paging a lane: the table carries
      // the resume, the posting and the screenshot for every row. renderTabs
      // owns the active state, so switching here goes through it rather than
      // reaching for the buttons directly.
      state.tab = "apps";
      state.filter = "applied";
      delete state.page.appsTable;
      renderTabs();
      renderPane();
      return;
    }
    const res = e.target.closest("[data-resume]");
    if (res) { openResume(res.dataset.resume); return; }
    if (e.target.closest("[data-approve-all]")) { approveAll(); return; }
    const runAllBtn = e.target.closest("[data-run-all]");
    if (runAllBtn) { e.stopPropagation(); runProcess(); return; }
    const runNow = e.target.closest('[data-act="run-now"]');
    if (runNow) {
      e.stopPropagation();
      if (!demoGuard()) {
        const pk = runNow.dataset.pk;
        // Move the card OUT of Found straight away. Scoring and tailoring take a
        // minute or more, and until this the row sat in Found with its button
        // still inviting a second click — which starts the whole job again.
        markTailoring(pk);
        post(`/actions/run-job/${encodeURIComponent(pk)}`);
        toast("Scoring and tailoring — it moves to Ready to apply when done.");
        pollStats();
      }
      return;
    }
    // Bulk selection in the queue. Ticks re-render the pane rather than mutating
    // the row in place, because the section head has to show the count and the
    // actions that only exist while something is ticked.
    // Turning rotation on or off for the company the board is filtered to.
    const rotOn = e.target.closest("[data-rot-on]");
    if (rotOn) {
      if (demoGuard()) return;
      const company = rotOn.dataset.rotOn;
      rotOn.disabled = true;
      rotOn.classList.add("is-busy");
      post("/actions/rotation", { company, profile_id: rotOn.dataset.profile })
        .then(async (d) => {
          if (!d || !d.ok) { toast((d && d.error) || "Couldn't set that up."); return; }
          await loadRotation();
          toast(`${company}: a new address every ${d.limit} applications. `
              + `Rotate & queue moves its un-sent work over.`);
          renderPane();
        });
      return;
    }
    const rotOff = e.target.closest("[data-rot-off]");
    if (rotOff) {
      if (demoGuard()) return;
      const company = rotOff.dataset.rotOff;
      post("/actions/rotation", { company, profile_id: "" }).then(async () => {
        await loadRotation();
        toast(`${company} no longer rotates. Addresses already used are kept.`);
        renderPane();
      });
      return;
    }
    const tick = e.target.closest("[data-qsel]");
    if (tick) {
      const pk = tick.dataset.qsel;
      state.qPicked.has(pk) ? state.qPicked.delete(pk) : state.qPicked.add(pk);
      qselPaint(tick.closest(".un-row"), state.qPicked.has(pk));
      return;
    }
    if (e.target.closest("[data-qsel-all]")) {
      document.querySelectorAll("[data-qsel]").forEach((cb) => {
        state.qPicked.add(cb.dataset.qsel);
        cb.checked = true;
        qselPaint(cb.closest(".un-row"), true);
      });
      return;
    }
    if (e.target.closest("[data-qsel-none]")) {
      state.qPicked.clear();
      document.querySelectorAll("[data-qsel]").forEach((cb) => {
        cb.checked = false;
        qselPaint(cb.closest(".un-row"), false);
      });
      return;
    }
    const bulk = e.target.closest("[data-qsel-skip], [data-qsel-remove]");
    if (bulk) {
      bulk.dataset.mode = bulk.hasAttribute("data-qsel-skip") ? "skip" : "remove";
      paneAction("qsel-bulk", "", bulk);
      return;
    }
    const act = e.target.closest("[data-act]");
    if (act) { paneAction(act.dataset.act, act.dataset.pk, act); return; }
    const pco = e.target.closest("[data-passed-co]");
    if (pco) {
      const k = pco.dataset.passedCo;
      state.passedOpen.has(k) ? state.passedOpen.delete(k) : state.passedOpen.add(k);
      renderPane();
      return;
    }
    const day = e.target.closest("[data-day]");
    if (day) {
      const k = day.dataset.day;
      state.dayShut.has(k) ? state.dayShut.delete(k) : state.dayShut.add(k);
      renderPane();
      return;
    }
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

  // Fresh rail: both search boxes repaint the DATA regions in place and never
  // their own element, so focus and caret simply survive — nothing to save,
  // nothing to hand back. The results search mirrors the workbar search: one
  // query, two places to type it.
  $("#pane").addEventListener("input", (e) => {
    if (e.target.id === "fr-search") {
      state.query = e.target.value;
      const g = $("#search");
      if (g) g.value = state.query;
      freshRefresh(true);   // the company counts follow the query too
      return;
    }
    if (e.target.id !== "fs-search") return;
    state.freshCoQuery = e.target.value;
    const fl = $("#fs-list");
    if (fl) fl.innerHTML = freshScanList();
  });
  // A tick both narrows the view and arms the scan. It refreshes counts and
  // results IN PLACE and leaves the checkbox list's DOM alone — the box just
  // clicked keeps its focus, its scroll and its next click, however fast they
  // come. This used to rebuild the whole pane and then hunt for the checkbox
  // to hand focus back to, which is how mid rebuild clicks were being eaten.
  $("#pane").addEventListener("change", (e) => {
    // The per-company cap. Employers do not share a number — your notes have
    // Ramp at 2 and Coinbase at 3 — and until now the only way to say so was to
    // edit the ledger by hand.
    const lim = e.target.closest("[data-rot-limit]");
    if (lim) {
      const company = lim.dataset.rotLimit;
      const limit = Math.max(1, Number(lim.value) || 5);
      post("/actions/rotation", { company, profile_id: rotProfileFor(company), limit })
        .then(async (d) => {
          if (!d || !d.ok) { toast((d && d.error) || "Couldn't save that."); return; }
          await loadRotation();
          toast(`${company}: a new address every ${d.limit} applications.`);
          renderPane();
        });
      return;
    }
    const cb = e.target.closest("input[data-fresh-co]");
    if (!cb) return;
    if (cb.checked) state.freshPicked.add(cb.value); else state.freshPicked.delete(cb.value);
    saveFreshPicked();
    freshRefresh();
  });

  // Heatmap tooltip: one floating tip, delegated on the pane so it survives
  // every re-render. Fixed positioning keeps it clear of the scroll clip.
  $("#pane").addEventListener("mouseover", (e) => {
    const tip = $("#hm-tip");
    if (!tip) return;
    const c = e.target.closest(".hm-c[data-hd]");
    if (!c) { tip.hidden = true; return; }
    const f = Number(c.dataset.hf) || 0, a = Number(c.dataset.ha) || 0;
    tip.innerHTML = `<b>${esc(c.dataset.hd)}</b><br>${
      f || a ? `${f} found · ${a} applied` : "no activity"}`;
    tip.hidden = false;
    const r = c.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    tip.style.left = Math.max(8, Math.min(window.innerWidth - tr.width - 8,
      r.left + r.width / 2 - tr.width / 2)) + "px";
    tip.style.top = (r.top - tr.height - 8 >= 4 ? r.top - tr.height - 8 : r.bottom + 8) + "px";
  });
  $("#pane").addEventListener("mouseleave", () => {
    const tip = $("#hm-tip");
    if (tip) tip.hidden = true;
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

  // Lane-level location filter (Tailored). Clicking the active pill clears it.
  document.addEventListener("click", (e) => {
    const pill = e.target.closest("[data-loc]");
    if (pill) {
      const key = pill.dataset.loc;
      state.locFilter = state.locFilter === key ? "" : key;
      renderPane();
      return;
    }
    const head = e.target.closest("[data-bucket]");
    if (head) {
      const key = head.dataset.bucket;
      state.collapsed = state.collapsed || new Set();
      state.collapsed.has(key) ? state.collapsed.delete(key) : state.collapsed.add(key);
      renderPane();
    }
  });

  // Choosing an identity for one job — and re-tailoring so the PDF follows.
  $("#drawer").addEventListener("change", async (e) => {
    const sel = e.target.closest("[data-job-profile]");
    if (!sel || demoGuard()) return;
    const pk = sel.dataset.jobProfile;
    const d = await post(`/actions/job-profile/${encodeURIComponent(pk)}`,
                         { profile_id: sel.value });
    if (!d || !d.ok) { toast((d && d.error) || "Couldn't set the profile."); return; }
    toast(d.retailoring
      ? "Profile set — re-tailoring so the résumé carries that address."
      : "Profile set for this application.");
    reload();
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
    else if (!qp.hidden) closeQp();
    else closeMenu();
  });
}

// --- boot ------------------------------------------------------------------
async function boot() {
  const savedTheme = localStorage.getItem("appliedin.theme");
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;
  const savedW = Number(localStorage.getItem("appliedin.freshw"));
  if (FRESH_WINDOWS.some(([h]) => h === savedW)) state.freshHours = savedW;
  const savedScan = Number(localStorage.getItem("appliedin.scanw"));
  if (SCAN_WINDOWS.some(([h]) => h === savedScan)) state.scanHours = savedScan;
  wire();
  renderScanWindow();   // the chips are static HTML; sync them to the restore
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
  loadQueue();   // the workbar badge needs a first read; after this it only
                 // refreshes on events, or every 3s while the panel is open
  if (!DEMO) {
    // The finish receipt survives a reload: the server's scan log knows a run
    // just ended even when this page was not open to watch it happen.
    loadScanLog().then(() => { adoptFinishedRun(); renderScanNow(); renderScanResults(); });
    connectLive();                                      // live activity (SSE)
    setInterval(pollStats, 3000);                       // button/vitals state
    setInterval(() => loadApps().catch(() => {}), 30000); // slow safety refresh
  } else {
    setFeedStatus("demo");
  }
}

boot();
