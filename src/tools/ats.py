"""URL-type skills — per-ATS (applicant-tracking-system) form handlers.

When a job URL belongs to a known ATS, we can extract and understand its form
far more reliably than with a generic DOM scrape, because each ATS renders its
fields with a consistent, recognizable structure. This module is the registry:

  detect_ats(url)            -> "ashby" | "greenhouse" | "lever" | "workday" | ""
  await extract_fields(page, ats) -> [field, ...]  (None → caller uses the generic scraper)

A `field` is the SAME shape the generic `_READ_FORM_JS` produces, so the rest of
the apply pipeline (fill / combobox / choices) is unchanged:
  {label, type, required, value, combo?, options?}

Add a new URL-type skill by writing a detector branch + an extractor JS here —
nothing else in the apply pipeline needs to change.
"""

from __future__ import annotations

from urllib.parse import urlparse

from core.logging import get_logger

log = get_logger(__name__)


def detect_ats(url: str) -> str:
    """The ATS this job URL belongs to (''=unknown → generic handling)."""
    host = (urlparse(url or "").hostname or "").lower()
    if "ashbyhq.com" in host:
        return "ashby"
    if "greenhouse.io" in host or "greenhouse" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "myworkdayjobs" in host or "myworkdaysite" in host:
        return "workday"
    return ""


# --- Ashby -------------------------------------------------------------------
# Ashby renders every field as a `._fieldEntry…` wrapper inside
# `.ashby-application-form`, each carrying its OWN unbound <label> as the field
# title (NOT tied to the input by id). Location is a `role=combobox` input with
# placeholder "Start typing…". A generic scraper mislabels these (it climbs to
# the section heading for short labels) — reading the wrapper's own label is
# what makes Ashby reliable. Labels here MUST match _FILL_JS's labelOf so fills
# resolve; both take the field wrapper's own <label>.
_ASHBY_FIELDS_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const clean = t => norm(t).replace(/\s*\*\s*$/, '').trim();   // drop trailing "*"
  const wrappers = [...document.querySelectorAll('[class*="_fieldEntry"]')];
  const out = [];
  const seen = new Set();
  for (const fe of wrappers) {
    const lblEl = [...fe.children].find(c => c.tagName === 'LABEL')
               || fe.querySelector('label');
    const rawLabel = lblEl ? lblEl.textContent : '';
    const label = clean(rawLabel);
    if (!label || seen.has(label)) continue;

    // A PRIMARY text/combo/select control wins — its radios (e.g. a phone
    // country-code widget) are auxiliary, not the field's answer.
    const ctrl = fe.querySelector(
      'input:not([type=hidden]):not([type=radio]):not([type=checkbox]), textarea, select, [role=combobox]');

    // No primary control but radio/checkbox options → a real choice GROUP.
    if (!ctrl) {
      const opts = [...fe.querySelectorAll('input[type=radio], input[type=checkbox]')];
      if (!opts.length) continue;
      const options = [...fe.querySelectorAll('label')]
        .map(l => norm(l.textContent))
        .filter(t => t && t !== label && t.length < 80);
      const checked = opts.find(o => o.checked);
      out.push({
        label, type: 'choice-group',
        required: /\*/.test(norm(rawLabel)),
        options,
        value: checked ? norm((checked.closest('label') || {}).textContent || '') : '',
      });
      seen.add(label);
      continue;
    }
    const isCombo = ctrl.getAttribute('role') === 'combobox'
                 || ctrl.getAttribute('aria-autocomplete')
                 || /start typing/i.test(ctrl.placeholder || '');
    const type = ctrl.tagName === 'TEXTAREA' ? 'textarea'
               : ctrl.tagName === 'SELECT' ? 'select'
               : isCombo ? 'text'
               : (ctrl.type || 'text');
    out.push({
      label, type, combo: !!isCombo,
      required: /\*/.test(norm(rawLabel)) || !!ctrl.required
                || ctrl.getAttribute('aria-required') === 'true',
      value: ctrl.type === 'checkbox' ? (ctrl.checked ? 'checked' : '')
           : (ctrl.type === 'file' ? (ctrl.files && ctrl.files.length ? ctrl.files[0].name : '')
                                   : (ctrl.value || '')),
    });
    seen.add(label);
  }
  return out;
}
"""


async def extract_fields(page, ats: str) -> list | None:  # noqa: ANN001
    """ATS-specific full-form extraction, or None if we don't have a skill for
    this ATS (caller falls back to the generic scraper)."""
    if ats == "ashby":
        try:
            fields = await page.evaluate(_ASHBY_FIELDS_JS)
            # Only trust it if it actually found a real form.
            if fields and len([f for f in fields if f.get("type") != "file"]) >= 2:
                return fields
        except Exception as exc:  # noqa: BLE001
            log.warning("ashby extract failed (%s) — using generic scraper", exc)
    return None
