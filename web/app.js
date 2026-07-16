// AppliedIn — Control SPA
// Vanilla ES modules, no build step. Multi-view (hash router) + detail drawer.
// Login is bypassed for now (auth module kept for later, see auth.js).

import { auth } from "./auth.js";

const CONFIG = window.APPLIEDIN_CONFIG || {};
const DEMO = CONFIG.demo === true || new URLSearchParams(location.search).has("demo");
const LOGIN_DISABLED = true; // "for now remove login page and show the rest"

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = { apps: [], stats: {}, companies: [], bank: { global: [], companies: {} },
  paused: false, filter: "all", query: "", route: "pipeline" };

// --- helpers ---------------------------------------------------------------
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
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
const STATUS_LABEL = (s) => s.replace(/_/g, " ");
const tagClass = (s) =>
  ["applied", "applied_manual"].includes(s) ? "solid strong"
  : s === "needs_human" ? "strong"
  : s === "found" ? "dashed"
  : ["skipped", "job_gone", "error", "capped"].includes(s) ? "" : "solid";

const LANES = [
  { key: "discovered", title: "Discovered", match: ["found"] },
  { key: "progress", title: "Tailoring & applying", match: ["tailored", "submitting"] },
  { key: "review", title: "Waiting for you", match: ["needs_human"] },
  { key: "applied", title: "Applied", match: ["applied", "applied_manual"] },
  { key: "closed", title: "Closed", match: ["skipped", "job_gone", "error", "capped"] },
];
const FILTERS = [
  ["all", "all", () => true],
  ["review", "needs you", (r) => r.status === "needs_human"],
  ["progress", "in pipeline", (r) => ["found", "tailored", "submitting", "capped"].includes(r.status)],
  ["applied", "applied", (r) => ["applied", "applied_manual"].includes(r.status)],
  ["closed", "closed", (r) => ["skipped", "job_gone", "error"].includes(r.status)],
];

function scoreHtml(n) {
  if (n == null) return '<span class="score">—</span>';
  return `<span class="score"><span class="score-bar"><span style="width:${n * 10}%"></span></span>${n}</span>`;
}
function tagHtml(s) {
  return `<span class="tag ${tagClass(s)}">${esc(STATUS_LABEL(s))}</span>`;
}

// --- views -----------------------------------------------------------------
const VIEWS = {
  pipeline: {
    title: "Pipeline",
    desc: "discovered → applied → waiting for you",
    render() {
      const kpis = kpiStrip();
      const lanes = LANES.map((lane) => {
        const items = state.apps.filter((a) => lane.match.includes(a.status));
        const cards = items.map(cardHtml).join("") ||
          `<div class="empty" style="padding:24px">empty</div>`;
        return `<div class="lane" data-lane="${lane.key}">
          <div class="lane-head"><span class="lane-dot"></span>
            <span class="lane-title">${lane.title}</span>
            <span class="lane-count">${items.length}</span></div>
          <div class="lane-body">${cards}</div>
        </div>`;
      }).join("");
      return kpis + `<div class="lanes">${lanes}</div>`;
    },
  },

  applications: {
    title: "Applications",
    desc: "every posting the pipeline has touched",
    render() {
      const counts = {};
      for (const [key, , pred] of FILTERS) counts[key] = state.apps.filter(pred).length;
      const chips = FILTERS.map(([key, label]) =>
        `<button class="chip" data-filter="${key}" aria-selected="${state.filter === key}">
          ${label}<span class="n">${counts[key]}</span></button>`).join("");
      const pred = FILTERS.find(([k]) => k === state.filter)[2];
      const q = state.query.toLowerCase();
      const rows = state.apps.filter(pred)
        .filter((r) => !q || `${r.company} ${r.title}`.toLowerCase().includes(q))
        .sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at));
      const body = rows.map((r) => `<tr data-pk="${esc(r.pk)}">
        <td>${tagHtml(r.status)}</td>
        <td class="t-co">${esc(r.company)}</td>
        <td class="t-role" title="${esc(r.title)}">${esc(r.title)}</td>
        <td>${scoreHtml(r.match_score)}</td>
        <td class="mono" style="color:var(--muted-fg)">${esc(r.resume_version || "—")}</td>
        <td class="t-when">${ago(r.updated_at)}</td>
      </tr>`).join("");
      return `<div class="panel">
        <div class="panel-head"><div class="filters">${chips}</div>
          <span class="mono" style="color:var(--faint);font-size:12px">${rows.length} shown</span></div>
        <div class="table-wrap"><table class="data"><thead><tr>
          <th>status</th><th>company</th><th>role</th><th>match</th><th>resume</th><th>updated</th>
        </tr></thead><tbody>${body || ""}</tbody></table>
        ${rows.length ? "" : '<div class="empty">Nothing here.</div>'}</div></div>`;
    },
  },

  review: {
    title: "Needs you",
    desc: "gated applications awaiting your reply",
    render() {
      const items = state.apps.filter((a) => a.status === "needs_human");
      if (!items.length) return `<div class="panel"><div class="empty">
        Nothing waiting — the pipeline is running clean.</div></div>`;
      return items.map((r) => `<div class="panel" style="margin-bottom:14px">
        <div class="panel-head">
          <div><div class="t-co" style="font-size:15px">${esc(r.company)}
            <span style="color:var(--faint);font-weight:400"> — ${esc(r.title)}</span></div>
            <div class="mono" style="font-size:12px;color:var(--faint);margin-top:3px">
              gate: ${esc(STATUS_LABEL(r.gate_reason || "review"))}</div></div>
          <button class="btn" data-open="${esc(r.pk)}">Open</button>
        </div>
        <div style="padding:14px 16px">${gatePrompt(r)}</div>
      </div>`).join("");
    },
  },

  companies: {
    title: "Companies",
    desc: "watchlist · burn-in · discovery mode",
    render() {
      return `<div class="grid-cards">${state.companies.map((c) => {
        const burn = Array.from({ length: c.needed }, (_, i) =>
          `<i class="${i < c.clean_approvals ? "on" : ""}"></i>`).join("");
        return `<div class="co-card">
          <div class="co-head"><span class="co-name">${esc(c.name)}</span>
            <span class="tag ${c.mode === "auto" ? "solid strong" : ""}">${esc(c.mode)}</span></div>
          <div class="co-row"><span>ATS</span><b>${esc(c.ats)}</b></div>
          <div class="co-row"><span>discovery</span><b>${esc(c.discovery)}</b></div>
          <div class="co-row"><span>applied</span><b>${c.applied_count}</b></div>
          <div class="co-row"><span>burn-in</span><b>${c.clean_approvals}/${c.needed}</b></div>
          <div class="burn">${burn}</div>
        </div>`;
      }).join("")}</div>`;
    },
  },

  answers: {
    title: "Answer bank",
    desc: "facts reused across portals + per-company answers",
    render() {
      const g = state.bank.global.map(qaHtml).join("") || '<div class="empty">no global facts</div>';
      const perCo = Object.entries(state.bank.companies).map(([co, list]) =>
        `<div class="bank-group"><h3>${esc(co)}</h3>${list.map(qaHtml).join("")}</div>`).join("");
      return `<div class="bank-group"><h3>Global facts · reused everywhere</h3>${g}</div>${perCo}`;
    },
  },
};

function kpiStrip() {
  const s = state.stats;
  const c = s.counts_by_status || {};
  const cap = s.daily_cap ?? 5, used = s.today_submitted ?? 0;
  const segs = Array.from({ length: cap }, (_, i) => `<i class="${i < used ? "on" : ""}"></i>`).join("");
  const pipeline = ["found", "tailored", "submitting"].reduce((n, k) => n + (c[k] || 0), 0);
  const tiles = [
    { l: "today · submitted", v: `${used}<span class="unit"> / ${cap}</span>`, x: `<div class="segmeter">${segs}</div>` },
    { l: "needs you", v: c.needs_human || 0, sub: (c.needs_human ? "awaiting reply" : "all clear") },
    { l: "in pipeline", v: pipeline, sub: `${c.submitting || 0} submitting` },
    { l: "applied", v: (c.applied || 0) + (c.applied_manual || 0), sub: `${c.skipped || 0} skipped` },
  ];
  return `<div class="kpis">${tiles.map((t) => `<div class="tile">
    <div class="tile-label">${t.l}</div><div class="tile-value">${t.v}</div>
    ${t.sub ? `<div class="tile-sub">${esc(t.sub)}</div>` : ""}${t.x || ""}</div>`).join("")}</div>`;
}

function cardHtml(r) {
  return `<div class="card" data-pk="${esc(r.pk)}">
    <div class="card-top"><span class="card-co">${esc(r.company)}</span>${scoreHtml(r.match_score)}</div>
    <div class="card-role">${esc(r.title)}</div>
    <div class="card-meta"><span>${esc(r.resume_version || "—")}</span>
      ${r.gate_reason ? `<span class="dotsep">${esc(STATUS_LABEL(r.gate_reason))}</span>` : ""}
      <span class="dotsep">${ago(r.updated_at)}</span></div>
  </div>`;
}
function qaHtml(x) {
  return `<div class="qa"><div class="qa-q">${esc(x.q)}</div>
    <div class="qa-a">${esc(x.a)}</div><div class="qa-src">via ${esc(x.source)}</div></div>`;
}
function gatePrompt(r) {
  if (r.gate_reason === "low_confidence" || r.gate_reason === "unknown_field") {
    const f = (r.fields || []).find((x) => x.confidence === "low");
    return f ? `<div class="mono" style="font-size:13px">Q: ${esc(f.label)}<br>
      <span style="color:var(--faint)">draft:</span> ${esc(f.value)} — reply <b>ok</b> to approve or send your own.</div>`
      : "Awaiting your input.";
  }
  if (r.gate_reason === "no_account") return "Auto-signup was blocked. Reply once the account exists.";
  if (r.gate_reason === "captcha") return "A CAPTCHA blocked the bot — apply manually, then /done.";
  return "Open to review the captured form and approve.";
}

// --- detail drawer ---------------------------------------------------------
function openDrawer(pk) {
  const r = state.apps.find((a) => a.pk === pk);
  if (!r) return;
  const links = [["JD", r.jd_url], ["Resume PDF", r.resume_url], ["Screenshot", r.screenshot_url]]
    .map(([l, h]) => h ? `<a class="lk" href="${esc(h)}" target="_blank" rel="noopener">${l}</a>`
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

  $("#drawer-body").innerHTML = `
    <div class="section"><div class="section-t">status</div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        ${tagHtml(r.status)}${scoreHtml(r.match_score)}
        ${r.gate_reason ? `<span class="mono" style="font-size:12px;color:var(--faint)">gate: ${esc(STATUS_LABEL(r.gate_reason))}</span>` : ""}
      </div></div>

    <div class="section"><div class="section-t">metadata used to apply</div>
      <dl class="meta-grid">
        <dt>resume version</dt><dd class="mono">${esc(r.resume_version || "—")}</dd>
        <dt>match score</dt><dd>${r.match_score ?? "—"} / 10</dd>
        <dt>ATS</dt><dd>${esc(r.ats || "—")}</dd>
        <dt>mode</dt><dd>${esc(r.mode || "—")}</dd>
        <dt>confirmation</dt><dd class="mono">${esc(r.confirmation_id || "—")}</dd>
        <dt>submitted</dt><dd>${when(r.submitted_at)}</dd>
        <dt>job id</dt><dd class="mono">${esc(r.pk)}</dd>
      </dl>
      ${r.resume_version ? `<button class="btn" style="margin-top:14px" data-resume="${esc(r.pk)}">
        View résumé · ${esc(r.resume_version)}</button>` : ""}</div>

    <div class="section"><div class="section-t">form answers · ${(r.fields || []).length} fields</div>
      <div class="fields">${fields}</div></div>

    ${r.jd_excerpt ? `<div class="section"><div class="section-t">JD snapshot</div>
      <div style="color:var(--muted-fg);font-size:13px;border:1px solid var(--border);
        border-radius:var(--r-md);padding:13px 15px;max-height:160px;overflow:auto">${esc(r.jd_excerpt)}</div></div>` : ""}

    <div class="section"><div class="section-t">timeline</div><div class="timeline">${tl}</div></div>

    <div class="section"><div class="section-t">artifacts</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">${links}</div></div>

    ${r.status === "needs_human" ? `<div class="drawer-actions">
      <button class="btn btn-primary" data-act="approve" data-pk="${esc(r.pk)}">Approve & submit</button>
      <button class="btn" data-act="skip" data-pk="${esc(r.pk)}">Skip</button></div>` : ""}
  `;
  $("#drawer-title").textContent = r.company;
  $("#drawer-sub").textContent = r.title;
  $("#scrim").hidden = false;
  $("#drawer").hidden = false;
  requestAnimationFrame(() => {
    $("#scrim").classList.add("show");
    $("#drawer").classList.add("show");
  });
}
function closeDrawer() {
  $("#scrim").classList.remove("show");
  $("#drawer").classList.remove("show");
  setTimeout(() => { $("#scrim").hidden = true; $("#drawer").hidden = true; }, 260);
}

// --- résumé viewer ---------------------------------------------------------
function openResume(pk) {
  const r = state.apps.find((a) => a.pk === pk);
  if (!r) return;
  const real = r.resume_url && /^https?:/.test(r.resume_url);
  $("#rmodal-title").textContent = `${r.company} · ${r.resume_version || "résumé"}`;
  const dl = $("#rmodal-download");
  if (real) { dl.href = r.resume_url; dl.hidden = false; } else dl.hidden = true;
  $("#rmodal-body").innerHTML = real
    ? `<iframe src="${esc(r.resume_url)}#toolbar=1" title="résumé"></iframe>`
    : demoResume(r);
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

// --- render / route --------------------------------------------------------
function renderNav() {
  $$(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.route === state.route));
  const needs = state.apps.filter((a) => a.status === "needs_human").length;
  const badge = $("#nav-review-badge");
  badge.hidden = needs === 0;
  badge.textContent = needs;
}
function render() {
  const view = VIEWS[state.route] || VIEWS.pipeline;
  $("#view-title").textContent = view.title;
  $("#view-desc").textContent = view.desc;
  $("#view").innerHTML = view.render();
  $("#foot-count").textContent = `${state.apps.length} applications`;
  renderNav();
}
function goto(route) {
  state.route = VIEWS[route] ? route : "pipeline";
  closeDrawer();
  render();
}

// --- data ------------------------------------------------------------------
async function load() {
  if (DEMO || LOGIN_DISABLED) {
    const d = await import("./demo-data.js");
    state.apps = d.applications;
    state.stats = d.stats;
    state.companies = d.companies;
    state.bank = d.bank;
    state.paused = d.paused;
    $("#foot-env").textContent = "demo";
  } else {
    const [apps, stats] = await Promise.all([
      fetch(api("/applications"), { headers: auth.header() }).then((r) => r.json()),
      fetch(api("/stats"), { headers: auth.header() }).then((r) => r.json()),
    ]);
    state.apps = apps.items || [];
    state.stats = stats;
    state.paused = !!stats.paused;
    $("#foot-env").textContent = CONFIG.env || "live";
  }
  $("#foot-updated").textContent = "updated " + new Date().toLocaleTimeString();
  renderPause();
  render();
}
const api = (p) => (CONFIG.apiUrl || "").replace(/\/$/, "") + p;

function renderPause() {
  $("#pipeline-toggle").dataset.state = state.paused ? "paused" : "running";
  $("#pipeline-label").textContent = state.paused ? "paused" : "running";
}
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg; el.hidden = false;
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => { el.classList.remove("show"); setTimeout(() => (el.hidden = true), 220); }, 2400);
}

// --- events / boot ---------------------------------------------------------
function wire() {
  $("#nav").addEventListener("click", (e) => {
    const item = e.target.closest(".nav-item");
    if (!item) return;
    e.preventDefault();
    location.hash = "#/" + item.dataset.route;
  });
  window.addEventListener("hashchange", () => goto(location.hash.replace("#/", "") || "pipeline"));

  $("#view").addEventListener("click", (e) => {
    const card = e.target.closest("[data-pk]");
    const chip = e.target.closest("[data-filter]");
    const open = e.target.closest("[data-open]");
    if (chip) { state.filter = chip.dataset.filter; render(); return; }
    if (open) { openDrawer(open.dataset.open); return; }
    if (card) openDrawer(card.dataset.pk);
  });
  $("#drawer").addEventListener("click", (e) => {
    const resume = e.target.closest("[data-resume]");
    if (resume) { openResume(resume.dataset.resume); return; }
    const act = e.target.closest("[data-act]");
    if (!act) return;
    toast(act.dataset.act === "approve" ? "approved & re-queued" : "skipped");
    closeDrawer();
  });
  $("#scrim").addEventListener("click", closeDrawer);
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#rmodal-close").addEventListener("click", closeResume);
  $("#rmodal").addEventListener("click", (e) => { if (e.target.id === "rmodal") closeResume(); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#rmodal").hidden) closeResume();
    else closeDrawer();
  });

  $("#search").addEventListener("input", (e) => {
    state.query = e.target.value;
    if (state.route === "applications") render();
  });
  $("#pipeline-toggle").addEventListener("click", () => {
    state.paused = !state.paused; renderPause();
    toast(state.paused ? "pipeline paused" : "pipeline resumed");
  });
  $("#theme-btn").addEventListener("click", () => {
    const root = document.documentElement;
    const light = root.getAttribute("data-theme") === "light";
    root.setAttribute("data-theme", light ? "dark" : "light");
    localStorage.setItem("appliedin.theme", light ? "dark" : "light");
  });
}

function startClock() {
  const t = () => ($("#clock").textContent = new Date().toLocaleTimeString("en-GB"));
  t(); setInterval(t, 1000);
}

async function boot() {
  const saved = localStorage.getItem("appliedin.theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  wire();
  startClock();
  goto(location.hash.replace("#/", "") || "pipeline");
  try { await load(); } catch (e) { toast("could not load data"); console.error(e); }
  goto(location.hash.replace("#/", "") || "pipeline");
  if (!DEMO && !LOGIN_DISABLED) setInterval(() => load().catch(() => {}), 30000);
}

boot();
