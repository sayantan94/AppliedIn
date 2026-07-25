/* AppliedIn — assisted apply, content script.
 *
 * This is a DRIVER, not the product. It knows how to read a form out of a page
 * and how to put values into it; every decision — which fact answers which
 * field, what an essay should say, which site has quirks — comes from the
 * AppliedIn server. Nothing is invented here and no key ever reaches the page.
 *
 * Why an extension at all: the pipeline drives a headless browser, and employers
 * increasingly answer that with a CAPTCHA. Running in the owner's own Chrome —
 * their session, their history, their profile — looks like what it is, a person
 * applying. When a challenge does appear the person is already there to clear it.
 * This never touches a CAPTCHA, and it never presses Submit.
 */

const API = "http://127.0.0.1:8787";

// ── reading the form ────────────────────────────────────────────────────────
const ntrim = (s) => (s || "").replace(/\s+/g, " ").trim();

// Cookie banners carry checkbox-shaped controls that look like form fields.
// Answering them corrupts the application and changes the owner's privacy
// settings, so they are invisible to everything below.
const COOKIE_SEL =
  '#onetrust-consent-sdk, #onetrust-banner-sdk, #onetrust-pc-sdk, .ot-sdk-container,' +
  ' .optanon-alert-box-wrapper, #CybotCookiebotDialog, .cc-window, .cookie-banner,' +
  ' [id*="cookie" i], [class*="cookie-banner" i], [aria-label*="cookie" i]';
const inCookieBanner = (el) => !!(el.closest && el.closest(COOKIE_SEL));

function labelOf(el) {
  const bound = el.labels && el.labels[0] && ntrim(el.labels[0].textContent);
  if (bound) return bound;
  const aria = el.getAttribute("aria-label");
  if (aria) return ntrim(aria);
  // A field wrapper's own unbound <label> — how Ashby and Greenhouse title fields.
  for (let w = el.parentElement, i = 0; i < 4 && w; i++, w = w.parentElement) {
    const own = [...w.children].find((c) => c.tagName === "LABEL");
    if (own) {
      const t = ntrim(own.textContent);
      if (t && t.length < 90) return t;
    }
  }
  const fs = el.closest("fieldset");
  const lg = fs && fs.querySelector("legend");
  if (lg) return ntrim(lg.textContent);
  return ntrim(el.placeholder || el.name || el.id || "");
}

function isCombo(el) {
  return (
    el.getAttribute("role") === "combobox" ||
    !!el.getAttribute("aria-autocomplete") ||
    !!el.getAttribute("aria-controls") ||
    /start typing|select\.{0,3}$/i.test(el.placeholder || "")
  );
}

/* The frame that actually holds the application. Employers embed the ATS form in
 * an iframe, leaving the top-level document with only nav and cookie controls —
 * so a driver that works on the top document silently does nothing. Each frame
 * runs its own copy of this script; this decides whether THIS one is the form. */
function formScore() {
  const q = (s) => document.querySelectorAll(s).length;
  return q("input[type=file]") * 100 + q("input[type=email]") * 25 +
         q("input[type=text], input[type=tel], textarea, select");
}

function readForm() {
  const out = [];
  const seen = new Set();
  const els = [...document.querySelectorAll("input, textarea, select")].filter(
    (el) => el.type !== "hidden" && !inCookieBanner(el)
  );
  // Radio/checkbox groups are ONE question with options, not N fields.
  const groups = new Map();
  for (const el of els) {
    const label = labelOf(el);
    if (!label) continue;
    if (el.type === "radio" || el.type === "checkbox") {
      const fs = el.closest("fieldset") || el.parentElement?.parentElement;
      const legend = fs && fs.querySelector("legend");
      const q = legend ? ntrim(legend.textContent) : label;
      if (!groups.has(q)) groups.set(q, { label: q, type: "choice-group", options: [], required: false, value: "" });
      const g = groups.get(q);
      g.options.push(label);
      g.required = g.required || el.required || /\*/.test(q);
      if (el.checked) g.value = label;
      continue;
    }
    if (seen.has(label)) continue;
    seen.add(label);
    out.push({
      label,
      type: el.tagName === "TEXTAREA" ? "textarea" : el.tagName === "SELECT" ? "select" : el.type || "text",
      combo: isCombo(el),
      required: el.required || el.getAttribute("aria-required") === "true" || /\*\s*$/.test(label),
      value: el.type === "file" ? (el.files && el.files[0] ? el.files[0].name : "") : el.value || "",
      options: el.tagName === "SELECT" ? [...el.options].map((o) => ntrim(o.textContent)).filter(Boolean) : undefined,
    });
  }
  return out.concat([...groups.values()]);
}

// ── writing to the form ─────────────────────────────────────────────────────
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findControl(label) {
  const want = label.toLowerCase().replace(/\s+/g, " ").trim();
  let best = null;
  let cost = Infinity;
  for (const el of document.querySelectorAll("input, textarea, select")) {
    if (el.type === "hidden" || inCookieBanner(el)) continue;
    if (["radio", "checkbox", "file", "submit", "button"].includes(el.type)) continue;
    const l = labelOf(el).toLowerCase();
    if (!l) continue;
    let c = null;
    if (l === want) c = 0;
    else if (l.includes(want) || want.includes(l)) c = Math.abs(l.length - want.length) + 1;
    if (c !== null && c < cost) { best = el; cost = c; }
  }
  return best;
}

/* Type like a person: focus, real key events, then commit. Assigning .value and
 * firing a synthetic event produces untrusted events with no keystrokes, which
 * is one of the signals a spam filter looks at — and React ignores it outright. */
async function typeInto(el, value) {
  el.scrollIntoView({ block: "center", behavior: "instant" });
  el.focus();
  const setter = Object.getOwnPropertyDescriptor(
    el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, "value").set;
  setter.call(el, "");
  el.dispatchEvent(new Event("input", { bubbles: true }));
  for (const ch of String(value)) {
    setter.call(el, el.value + ch);
    el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ch, inputType: "insertText" }));
    if (String(value).length < 60) await sleep(8 + Math.random() * 18);
  }
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.blur();
}

/* A combobox ignores a typed value: it needs the option picked out of the popup
 * it opens. Type a prefix, wait for the list, click the best match. */
async function fillCombo(el, value) {
  el.scrollIntoView({ block: "center", behavior: "instant" });
  el.click();
  await typeInto(el, String(value).split(",")[0].trim());
  el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  for (let i = 0; i < 12; i++) {
    await sleep(250);
    const opts = [...document.querySelectorAll(
      '[role=option], [class*="option" i]:not([class*="options" i]), li[id*="option"]')]
      .filter((o) => o.offsetParent);
    if (!opts.length) continue;
    const want = String(value).toLowerCase();
    const hit = opts.find((o) => ntrim(o.textContent).toLowerCase().includes(want.split(",")[0].trim()))
      || opts[0];
    hit.click();
    await sleep(200);
    return true;
  }
  return false;
}

/* Match an option by MEANING, never by bare substring. "No" is inside "North
 * Korea", so a substring match once selected a sanctioned-country option for a
 * question whose answer was "No". Exact first, then whole-word. */
function optionMatches(optionLabel, want) {
  const l = optionLabel.toLowerCase().trim();
  const w = String(want).toLowerCase().trim();
  if (!w) return false;
  if (l === w) return true;
  return new RegExp(`\\b${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`).test(l);
}

async function pickChoice(question, option) {
  const qn = question.toLowerCase();
  const boxes = [...document.querySelectorAll("input[type=radio], input[type=checkbox]")]
    .filter((el) => !inCookieBanner(el));
  // Prefer an exact option label, so a longer correct option ("None of the
  // above") is never beaten by a shorter accidental hit.
  const scoped = boxes.filter((el) => {
    const fs = el.closest("fieldset");
    const lg = fs && fs.querySelector("legend");
    return lg ? ntrim(lg.textContent).toLowerCase().includes(qn.slice(0, 40)) : true;
  });
  const ordered = [...scoped].sort((a, b) => labelOf(b).length - labelOf(a).length);
  const hit = ordered.find((el) => labelOf(el).toLowerCase().trim() === String(option).toLowerCase().trim())
           || ordered.find((el) => optionMatches(labelOf(el), option));
  if (!hit) return false;
  if (!hit.checked) { hit.scrollIntoView({ block: "center" }); hit.click(); }
  return true;
}

/* Sanctions / export-control questions are answered negatively, always, in the
 * driver as well as on the server. The cost of one wrong tick here is a
 * self-reported disqualifier under the owner's name, so it does not rely on the
 * model having mapped the question correctly. */
const COUNTRY_RX = /cuba|iran\b|north korea|syria|crimea|donetsk|luhansk|zaporizhzhia|kherson|\bbelarus\b|\brussia\b/i;
const SAFE_RX = /^(none of the above|not applicable|none of these apply|no)\b/i;

async function sanctionsSweep() {
  const boxes = [...document.querySelectorAll("input[type=checkbox], input[type=radio]")]
    .filter((b) => !inCookieBanner(b));
  const risky = boxes.filter((b) => COUNTRY_RX.test(labelOf(b)));
  if (!risky.length) return "";
  let scope = risky[0].parentElement;
  for (let i = 0; i < 6 && scope; i++, scope = scope.parentElement) {
    if (risky.every((r) => scope.contains(r))) break;
  }
  scope = scope || document.body;
  for (const r of risky) if (r.checked) r.click();
  const safe = boxes.filter((b) => scope.contains(b)).find((b) => SAFE_RX.test(labelOf(b)));
  if (safe && !safe.checked) { safe.scrollIntoView({ block: "center" }); safe.click(); return labelOf(safe); }
  return safe ? "already " + labelOf(safe) : "no safe option found";
}

async function attachResume(url, filename) {
  const inputs = [...document.querySelectorAll('input[type=file]')].filter((i) => !inCookieBanner(i));
  const describe = (i) => (i.name + " " + i.id + " " + (i.closest("div,label,fieldset")?.textContent || "")).toLowerCase();
  const candidates = inputs.filter((i) => !/cover letter|coverletter/.test(describe(i)));
  const target = candidates.find((i) => /resume|cv|résumé/.test(describe(i))) || candidates[0];
  if (!target) return 0;
  const blob = await (await fetch(API + url)).blob();
  const file = new File([blob], filename || "Resume.pdf", { type: "application/pdf" });
  const dt = new DataTransfer();
  dt.items.add(file);
  target.files = dt.files;
  target.dispatchEvent(new Event("change", { bubbles: true }));
  return 1;
}

// ── the run ─────────────────────────────────────────────────────────────────
async function run() {
  if (formScore() < 25) return { ok: false, error: "no application form in this frame" };

  const fields = readForm();
  const ctx = await (await fetch(`${API}/extension/context?url=${encodeURIComponent(location.href)}`)).json();

  const plan = await (await fetch(`${API}/extension/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: location.href, company: ctx.company, fields,
                           jd_text: document.body.innerText.slice(0, 6000) }),
  })).json();
  if (!plan.ok) return { ok: false, error: plan.error || "the server could not plan this form" };

  const byLabel = Object.fromEntries(fields.map((f) => [f.label, f]));
  let filled = 0;
  for (const [label, value] of Object.entries(plan.values || {})) {
    const f = byLabel[label] || {};
    try {
      if (f.type === "choice-group") { if (await pickChoice(label, value)) filled++; continue; }
      const el = findControl(label);
      if (!el) continue;
      if (f.combo || isCombo(el)) { if (await fillCombo(el, value)) filled++; continue; }
      if (el.tagName === "SELECT") {
        const opt = [...el.options].find((o) => ntrim(o.textContent).toLowerCase().includes(String(value).toLowerCase()));
        if (opt) { el.value = opt.value; el.dispatchEvent(new Event("change", { bubbles: true })); filled++; }
        continue;
      }
      await typeInto(el, value);
      filled++;
    } catch (e) { /* one stubborn field must not stop the rest */ }
  }

  const sanctions = await sanctionsSweep();
  let resume = 0;
  if (ctx.resume_url) { try { resume = await attachResume(ctx.resume_url, ctx.resume_name); } catch (e) {} }

  // What is still required and unanswered — the person finishes these.
  const remaining = readForm()
    .filter((f) => f.required && !f.value && f.type !== "file")
    .map((f) => f.label);

  return { ok: true, company: ctx.company, title: ctx.title, pk: ctx.pk,
           filled, resume, sanctions, essays: plan.essays || [],
           missing: plan.missing || [], remaining,
           siteRules: !!ctx.site_rules };
}

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  if (msg?.type === "APPLIEDIN_FILL") { run().then(respond).catch((e) => respond({ ok: false, error: String(e) })); return true; }
  if (msg?.type === "APPLIEDIN_PROBE") { respond({ score: formScore(), url: location.href }); return true; }
  return false;
});
