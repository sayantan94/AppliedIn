// Sample data for demo mode — lets the whole dashboard render without a backend.
const now = Date.now();
const min = (m) => new Date(now - m * 60000).toISOString();

export const paused = false;

// Canonical EEO / fact fields most forms ask — reused across the demo rows.
const eeo = [
  { label: "Are you authorized to work in the US?", value: "Yes", type: "text", confidence: "high" },
  { label: "Do you now or in the future require sponsorship?", value: "Yes, H-1B", type: "text", confidence: "high" },
  { label: "Gender", value: "Decline to self-identify", type: "select", confidence: "high" },
  { label: "Veteran status", value: "Not a protected veteran", type: "select", confidence: "high" },
  { label: "Consent to data processing", value: true, type: "checkbox", confidence: "high" },
];

function tl(events) {
  return events.map(([label, m, done]) => ({ label, at: min(m), done: done !== false }));
}

export const applications = [
  {
    pk: "stripe#a1", company: "Stripe", title: "Staff Backend Engineer, Payments",
    status: "applied", match_score: 9, gate_reason: null, ats: "greenhouse", mode: "auto",
    resume_version: "v4-payments", resume_url: "#", jd_url: "#", screenshot_url: "#",
    confirmation_id: "GH-8842190", submitted_at: min(14), updated_at: min(14),
    jd_excerpt: "We're looking for a staff engineer to own reliability of the payments ledger…",
    fields: [
      { label: "Full name", value: "Sayantan Bhowmik", type: "text", confidence: "high" },
      { label: "Email", value: "sayantan.bhowmik94@gmail.com", type: "text", confidence: "high" },
      { label: "Notice period", value: "30 days", type: "text", confidence: "high" },
      { label: "Desired salary", value: "$240,000", type: "text", confidence: "high" },
      ...eeo,
    ],
    timeline: tl([["Discovered", 320], ["Tailored · score 9", 210], ["Submitted", 14]]),
  },
  {
    pk: "ramp#b2", company: "Ramp", title: "Senior Software Engineer, Platform",
    status: "needs_human", match_score: 8, gate_reason: "low_confidence", ats: "ashby", mode: "gated",
    resume_version: "v4-platform", resume_url: "#", jd_url: "#", screenshot_url: null,
    confirmation_id: null, submitted_at: null, updated_at: min(41),
    jd_excerpt: "You'll work on the core platform team building primitives other teams rely on…",
    fields: [
      { label: "Full name", value: "Sayantan Bhowmik", type: "text", confidence: "high" },
      { label: "Why do you want to work at Ramp?", value: "(awaiting your answer)", type: "text", confidence: "low" },
      { label: "Notice period", value: "30 days", type: "text", confidence: "high" },
      ...eeo,
    ],
    timeline: tl([["Discovered", 90], ["Tailored · score 8", 70], ["Gated: low confidence", 41, false]]),
  },
  {
    pk: "linear#c3", company: "Linear", title: "Product Engineer",
    status: "submitting", match_score: 8, gate_reason: null, ats: "greenhouse", mode: "auto",
    resume_version: "v3-product", resume_url: "#", jd_url: "#", screenshot_url: null,
    confirmation_id: null, submitted_at: null, updated_at: min(3),
    jd_excerpt: "Product engineers at Linear own features end to end…",
    fields: [
      { label: "Full name", value: "Sayantan Bhowmik", type: "text", confidence: "high" },
      { label: "Portfolio / GitHub", value: "github.com/sayantan", type: "text", confidence: "high" },
      ...eeo,
    ],
    timeline: tl([["Discovered", 40], ["Tailored · score 8", 20], ["Submitting…", 3, false]]),
  },
  {
    pk: "vercel#d4", company: "Vercel", title: "Backend Engineer, Infrastructure",
    status: "tailored", match_score: 7, gate_reason: null, ats: "lever", mode: "gated",
    resume_version: "v4-infra", resume_url: "#", jd_url: "#", screenshot_url: null,
    confirmation_id: null, submitted_at: null, updated_at: min(8),
    jd_excerpt: "Own the infrastructure that powers millions of deployments…",
    fields: [{ label: "Full name", value: "Sayantan Bhowmik", type: "text", confidence: "high" }, ...eeo],
    timeline: tl([["Discovered", 22], ["Tailored · score 7", 8, false]]),
  },
  {
    pk: "figma#f6", company: "Figma", title: "Senior Engineer, Multiplayer",
    status: "found", match_score: null, gate_reason: null, ats: "greenhouse", mode: "gated",
    resume_version: null, resume_url: null, jd_url: "#", screenshot_url: null,
    confirmation_id: null, submitted_at: null, updated_at: min(5),
    jd_excerpt: "Help build the real-time collaboration engine behind Figma…",
    fields: [], timeline: tl([["Discovered", 5, false]]),
  },
  {
    pk: "notion#e5", company: "Notion", title: "Software Engineer, Data",
    status: "needs_human", match_score: 7, gate_reason: "no_account", ats: "workday", mode: "gated",
    resume_version: "v3-data", resume_url: "#", jd_url: "#", screenshot_url: "#",
    confirmation_id: null, submitted_at: null, updated_at: min(62),
    jd_excerpt: "Build the data platform powering Notion's analytics…",
    fields: [{ label: "Full name", value: "Sayantan Bhowmik", type: "text", confidence: "high" }, ...eeo],
    timeline: tl([["Discovered", 140], ["Tailored · score 7", 120], ["Gated: no account", 62, false]]),
  },
  {
    pk: "airbnb#g7", company: "Airbnb", title: "Staff Engineer, Payments Platform",
    status: "applied", match_score: 8, gate_reason: null, ats: "greenhouse", mode: "auto",
    resume_version: "v4-payments", resume_url: "#", jd_url: "#", screenshot_url: "#",
    confirmation_id: "GH-7710233", submitted_at: min(120), updated_at: min(120),
    jd_excerpt: "Lead the payments platform serving millions of hosts and guests…",
    fields: [{ label: "Full name", value: "Sayantan Bhowmik", type: "text", confidence: "high" },
      { label: "Desired salary", value: "$250,000", type: "text", confidence: "high" }, ...eeo],
    timeline: tl([["Discovered", 400], ["Tailored · score 8", 300], ["Submitted", 120]]),
  },
  {
    pk: "openai#i9", company: "OpenAI", title: "Member of Technical Staff, Applied",
    status: "applied_manual", match_score: 9, gate_reason: "captcha", ats: "ashby", mode: "gated",
    resume_version: "v4-applied", resume_url: "#", jd_url: "#", screenshot_url: null,
    confirmation_id: "manual", submitted_at: min(200), updated_at: min(200),
    jd_excerpt: "Ship applied AI products used by hundreds of millions…",
    fields: [{ label: "Full name", value: "Sayantan Bhowmik", type: "text", confidence: "high" }, ...eeo],
    timeline: tl([["Discovered", 520], ["Tailored · score 9", 420], ["CAPTCHA — applied manually", 200]]),
  },
  {
    pk: "databricks#h8", company: "Databricks", title: "Distributed Systems Engineer",
    status: "capped", match_score: 9, gate_reason: null, ats: "greenhouse", mode: "auto",
    resume_version: "v4-dist", resume_url: "#", jd_url: "#", screenshot_url: null,
    confirmation_id: null, submitted_at: null, updated_at: min(30),
    jd_excerpt: "Work on the distributed query engine at petabyte scale…",
    fields: [{ label: "Full name", value: "Sayantan Bhowmik", type: "text", confidence: "high" }, ...eeo],
    timeline: tl([["Discovered", 60], ["Tailored · score 9", 40], ["Daily cap reached — parked", 30, false]]),
  },
  {
    pk: "brex#j10", company: "Brex", title: "Backend Engineer II",
    status: "skipped", match_score: 5, gate_reason: null, ats: "greenhouse", mode: "gated",
    resume_version: null, resume_url: null, jd_url: "#", screenshot_url: null,
    confirmation_id: null, submitted_at: null, updated_at: min(310),
    jd_excerpt: "Mid-level backend role on the corporate card team…",
    fields: [], timeline: tl([["Discovered", 330], ["Skipped · score 5 < 7", 310]]),
  },
  {
    pk: "plaid#k11", company: "Plaid", title: "Senior Backend Engineer",
    status: "job_gone", match_score: 7, gate_reason: null, ats: "lever", mode: "gated",
    resume_version: "v3-data", resume_url: null, jd_url: "#", screenshot_url: null,
    confirmation_id: null, submitted_at: null, updated_at: min(400),
    jd_excerpt: "", fields: [], timeline: tl([["Discovered", 430], ["Posting closed on arrival", 400]]),
  },
];

export const companies = [
  { name: "Stripe", careers_url: "boards.greenhouse.io/stripe", ats: "greenhouse", discovery: "feed", mode: "auto", clean_approvals: 3, needed: 3, applied_count: 4 },
  { name: "Ramp", careers_url: "jobs.ashbyhq.com/ramp", ats: "ashby", discovery: "feed", mode: "gated", clean_approvals: 1, needed: 3, applied_count: 0 },
  { name: "Vercel", careers_url: "jobs.lever.co/vercel", ats: "lever", discovery: "feed", mode: "gated", clean_approvals: 2, needed: 3, applied_count: 1 },
  { name: "Notion", careers_url: "notion.wd1.myworkdayjobs.com", ats: "workday", discovery: "feed", mode: "gated", clean_approvals: 0, needed: 3, applied_count: 0 },
  { name: "Custom Co", careers_url: "careers.customco.com", ats: "custom", discovery: "crawl", mode: "gated", clean_approvals: 0, needed: 3, applied_count: 0 },
];

export const bank = {
  global: [
    { q: "Are you authorized to work in the US?", a: "Yes", source: "seed" },
    { q: "Do you now or in the future require sponsorship?", a: "Yes, H-1B in the future", source: "whatsapp_reply" },
    { q: "Notice period", a: "30 days", source: "seed" },
    { q: "Desired salary", a: "$240,000", source: "/fact" },
    { q: "Veteran status", a: "Not a protected veteran", source: "seed" },
  ],
  companies: {
    Ramp: [{ q: "Why do you want to work at Ramp?", a: "I've followed Ramp's finance-automation work closely…", source: "gate_approval" }],
    Stripe: [{ q: "What excites you about payments?", a: "Payments is where correctness and scale collide…", source: "gate_approval" }],
  },
};

export const stats = {
  daily_cap: 5,
  today_submitted: 3,
  queue_age_seconds: 142,
  paused,
  counts_by_status: applications.reduce((a, r) => ((a[r.status] = (a[r.status] || 0) + 1), a), {}),
};
