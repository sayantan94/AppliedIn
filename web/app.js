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
  paused: false, filter: "all", query: "", route: "live", events: [], liveSeen: 0 };

// --- helpers ---------------------------------------------------------------
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
  const closeLists = () => { if (ul) { out.push("</ul>"); ul = false; } if (ol) { out.push("</ol>"); ol = false; } };
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
  live: {
    title: "Logs",
    desc: "raw input & output at every pipeline stage, grouped by job — live",
    render() {
      if (!state.events.length) {
        return `<div class="panel"><div class="empty">
          Waiting for activity — every stage's raw input and output streams here,
          grouped by job, as the daemon runs.</div></div>`;
      }
      return `<div class="runs">${groupRuns(state.events).map(runBlock).join("")}</div>`;
    },
  },

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
              gate: ${esc(STATUS_LABEL(r.gate_reason || "review"))} · <span style="color:var(--muted-fg)">${esc(r.pk)}</span>
              ${r.jd_url ? ` · <a href="${esc(r.jd_url)}" target="_blank" rel="noopener" style="color:var(--muted-fg)">view posting ↗</a>` : ""}
            </div></div>
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
  const jobUrl = r.jd_url
    ? `<a class="card-joburl" href="${esc(r.jd_url)}" target="_blank" rel="noopener"
         onclick="event.stopPropagation()">view posting ↗</a>`
    : "";
  return `<div class="card" data-pk="${esc(r.pk)}">
    <div class="card-top"><span class="card-co">${esc(r.company)}</span>${scoreHtml(r.match_score)}</div>
    <div class="card-role">${esc(r.title)}</div>
    <div class="card-meta"><span>${esc(r.resume_version || "—")}</span>
      ${r.gate_reason ? `<span class="dotsep">${esc(STATUS_LABEL(r.gate_reason))}</span>` : ""}
      <span class="dotsep">${ago(r.updated_at)}</span></div>
    ${jobUrl ? `<div class="card-links">${jobUrl}</div>` : ""}
  </div>`;
}
function qaHtml(x) {
  return `<div class="qa"><div class="qa-q">${esc(x.q)}</div>
    <div class="qa-a">${esc(x.a)}</div><div class="qa-src">via ${esc(x.source)}</div></div>`;
}
function gatePrompt(r) {
  if (r.gate_question) {
    return `<div class="md" style="font-size:13px;color:var(--fg)">${md(r.gate_question)}</div>`;
  }
  if (r.gate_reason === "no_account") return "Auto-signup was blocked. Reply once the account exists.";
  if (r.gate_reason === "captcha") return "A CAPTCHA blocked the bot — apply manually, then /done.";
  return "Open to review — approve to continue.";
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

  const gate = r.status === "needs_human" ? `
    <div class="section gate-box">
      <div class="section-t">⛔ blocked — needs you${r.gate_reason ? " · " + esc(STATUS_LABEL(r.gate_reason)) : ""}</div>
      <div class="gate-q md">${md(r.gate_question || "The pipeline is paused at this step — approve to continue.")}</div>
      <textarea id="gate-answer" class="gate-input" rows="2"
        placeholder="Type your answer, or leave blank to approve &amp; continue…"></textarea>
      <div class="drawer-actions">
        <button class="btn btn-primary" data-act="answer" data-pk="${esc(r.pk)}">Approve &amp; continue</button>
        <button class="btn" data-act="skip" data-pk="${esc(r.pk)}">Skip</button>
      </div>
    </div>` : "";

  const closed = r.closed_reason ? `
    <div class="section closed-box">
      <div class="section-t">why it closed</div>
      <div class="closed-why">${esc(r.closed_reason)}</div>
    </div>` : "";

  const diff = r.has_diff ? `
    <div class="section"><div class="section-t">what the tailor changed</div>
      <div id="diff-body" class="diff-body"><span class="muted">loading…</span></div></div>` : "";

  $("#drawer-body").innerHTML = `
    ${gate}
    ${closed}
    <div class="section"><div class="section-t">status</div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        ${tagHtml(r.status)}${scoreHtml(r.match_score)}
        ${r.gate_reason ? `<span class="mono" style="font-size:12px;color:var(--faint)">gate: ${esc(STATUS_LABEL(r.gate_reason))}</span>` : ""}
      </div></div>
    ${diff}

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
  `;
  $("#drawer-title").textContent = r.company;
  $("#drawer-sub").textContent = r.title;
  $("#scrim").hidden = false;
  $("#drawer").hidden = false;
  requestAnimationFrame(() => {
    $("#scrim").classList.add("show");
    $("#drawer").classList.add("show");
  });
  if (r.has_diff) loadDiff(r.pk);
}

function loadDiff(pk) {
  fetch(api(`/actions/diff/${encodeURIComponent(pk)}`), { headers: auth.header() })
    .then((r) => r.json())
    .then((d) => {
      const el = $("#diff-body");
      if (!el) return;
      const changes = (d.changes || []).filter((c) => c.before?.length || c.after?.length);
      if (!changes.length) { el.innerHTML = `<span class="muted">${esc(d.note || "no bullet changes")}</span>`; return; }
      el.innerHTML = changes.map((c) => `
        <div class="diff-item">
          ${(c.before || []).map((b) => `<div class="diff-line del">− ${esc(b)}</div>`).join("")}
          ${(c.after || []).map((a) => `<div class="diff-line add">+ ${esc(a)}</div>`).join("")}
        </div>`).join("");
    })
    .catch(() => { const el = $("#diff-body"); if (el) el.innerHTML = `<span class="muted">couldn't load diff</span>`; });
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
  // Real PDF whenever we have a resume_url (http presign OR same-origin
  // /artifact/…). Only demo mode falls back to the placeholder résumé.
  const real = !!r.resume_url && !DEMO;
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
  const view = VIEWS[state.route] || VIEWS.live;
  $("#view-title").textContent = view.title;
  $("#view-desc").textContent = view.desc;
  $("#view").innerHTML = view.render();
  $("#foot-count").textContent = `${state.apps.length} applications`;
  renderNav();
}
function goto(route) {
  state.route = VIEWS[route] ? route : "live";
  if (state.route === "live") { state.liveSeen = 0; updateLiveBadge(); }
  closeDrawer();
  render();
}

// --- data ------------------------------------------------------------------
async function load() {
  if (DEMO) {
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
    state.companies = apps.companies || [];
    state.bank = apps.bank || { global: [], companies: {} };
    state.paused = !!stats.paused;
    $("#foot-env").textContent = CONFIG.env || "local";
  }
  $("#foot-updated").textContent = "updated " + new Date().toLocaleTimeString();
  renderPause();
  render();
}
const api = (p) => (CONFIG.apiUrl || "").replace(/\/$/, "") + p;
function post(path, body) {
  return fetch(api(path), {
    method: "POST",
    headers: { ...auth.header(), "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  }).catch(() => toast("request failed"));
}

// Raw logs, grouped by job → chronological stages. One `run` block per job.
const STAGE_LABEL = { scorer: "score", tailor: "tailor", critic: "critique",
  applier: "apply", tailor_critique: "tailor", appliedin_pipeline: "pipeline" };

function groupRuns(events) {
  const order = [];               // pks, most-recently-active first
  const byPk = new Map();
  for (const e of events) {       // events arrive newest-first
    const pk = e.pk || "—";
    if (!byPk.has(pk)) { byPk.set(pk, []); order.push(pk); }
    byPk.get(pk).unshift(e);      // chronological within a job
  }
  return order.map((pk) => {
    const lines = byPk.get(pk);
    const app = state.apps.find((a) => a.pk === pk) || {};
    const meta = lines.find((e) => e.url) || {};
    const named = lines.find((e) => (e.detail || "").includes(" @ "));
    return {
      pk,
      company: app.company || (named ? named.detail.split(" @ ")[1] : ""),
      title: app.title || (named ? named.detail.split(" @ ")[0] : pk),
      url: app.jd_url || meta.url || "",
      status: app.status || runStatus(lines),
      lines,
    };
  });
}
function runStatus(lines) {
  for (let i = lines.length - 1; i >= 0; i--) {
    const k = lines[i].kind;
    if (k === "applied") return "applied";
    if (k === "gate") return "needs_human";
    if (k === "error") return "error";
  }
  return "submitting";
}

function runBlock(r) {
  return `<div class="run" data-pk="${esc(r.pk)}">
    <div class="run-head">
      <span class="run-co">${esc(r.company || "—")}</span>
      <span class="run-role">${esc(r.title)}</span>
      ${tagHtml(r.status)}
    </div>
    ${r.url ? `<a class="run-url" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.url)}</a>` : ""}
    <div class="run-log">${r.lines.map(logLine).join("")}</div>
  </div>`;
}

function logLine(e) {
  const t = e.at ? new Date(e.at).toLocaleTimeString("en-GB") : "";
  const stage = STAGE_LABEL[e.agent] || e.agent || "";
  if (e.kind === "step") {  // entering a stage → a subtle separator
    return `<div class="log-stage">${esc(STAGE_LABEL[e.detail] || e.detail || stage)}</div>`;
  }
  let mark = "·", raw = e.detail || "";
  if (e.kind === "response") { mark = "model"; raw = e.detail; }
  else if (e.kind === "action") { mark = "→ call"; raw = `${e.detail}(${e.input ?? ""})`; }
  else if (e.kind === "result") { mark = "← ret"; raw = `${e.detail}  ${e.output ?? ""}`; }
  else if (e.kind === "gate") { mark = "gate"; }
  else if (e.kind === "applied") { mark = "done"; }
  else if (e.kind === "discovered") { mark = "found"; }
  else if (e.kind === "running") { mark = "start"; }
  else if (e.kind === "error") { mark = "error"; }
  const shot = e.screenshot
    ? `<a class="log-shot" href="${esc(e.screenshot)}" target="_blank" rel="noopener">
         <img src="${esc(e.screenshot)}" alt="screenshot" loading="lazy"></a>` : "";
  const rawHtml = (e.kind === "response" || e.kind === "gate") ? md(raw) : esc(raw);
  return `<div class="log-line k-${esc(e.kind)}">
    <span class="log-t">${t}</span>
    <span class="log-stg">${esc(stage)}</span>
    <span class="log-mark">${esc(mark)}</span>
    <span class="log-raw ${(e.kind === "response" || e.kind === "gate") ? "md" : ""}">${rawHtml}</span>
    ${shot}
  </div>`;
}

function connectLive() {
  if (DEMO) return;  // demo has no live stream
  let es;
  const open = () => {
    es = new EventSource(api("/events"));
    es.onmessage = (m) => {
      try {
        const e = JSON.parse(m.data);
        state.events.unshift(e);
        if (state.events.length > 300) state.events.pop();
        if (state.route === "live") render();
        else { state.liveSeen++; updateLiveBadge(); }
        if (["discovered", "applied", "gate", "running"].includes(e.kind)) scheduleReload();
      } catch { /* ignore */ }
    };
    es.onerror = () => { es.close(); setTimeout(open, 3000); };  // auto-reconnect
  };
  open();
}

let _reloadTimer = null;
function scheduleReload() {  // debounce board refreshes when the store changes
  clearTimeout(_reloadTimer);
  _reloadTimer = setTimeout(() => load().catch(() => {}), 800);
}
function updateLiveBadge() {
  const b = $("#nav-live-badge");
  if (state.route === "live" || state.liveSeen === 0) { b.hidden = true; return; }
  b.hidden = false; b.textContent = state.liveSeen > 99 ? "99+" : state.liveSeen;
}

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
    const pk = act.dataset.pk;
    if (act.dataset.act === "answer") {
      const answer = ($("#gate-answer")?.value || "").trim() || "approved";
      post(`/actions/resume/${encodeURIComponent(pk)}`, { answer });
      toast("sent — pipeline resuming");
    } else {
      post(`/actions/skip/${encodeURIComponent(pk)}`);
      toast("skipped");
    }
    closeDrawer();
    scheduleReload();
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
  $("#reset-btn").addEventListener("click", () => {
    if (!confirm("Reset the pipeline? Clears all tracked jobs, the queue, live logs, "
      + "and stored résumés/screenshots. Your facts and saved logins are kept.")) return;
    post("/actions/reset").then(() => {
      state.apps = []; state.events = []; state.liveSeen = 0;
      toast("pipeline reset"); load(); render();
    });
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
  if (!DEMO) {
    connectLive();                                   // live activity stream (SSE)
    setInterval(() => load().catch(() => {}), 30000); // periodic board refresh
  }
}

boot();
