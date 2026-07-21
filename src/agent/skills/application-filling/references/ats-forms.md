# ATS form quirks (reference)

Per-ATS behaviour the scripted apply engine handles, and what a gate means when it
doesn't. Consult when a form doesn't behave as expected.

## Ashby (jobs.ashbyhq.com)
- The form is behind an **"Application" tab** — click it before reading fields.
- Two uploaders: an optional **"Autofill from resume"** box and the **required
  Résumé** field. Upload the tailored PDF to the required one only.
- **Location** is an autocomplete. It renders suggestions as `<button>` elements
  (NOT `[role=option]`), so a plain option-list picker finds nothing. Type the
  **city token** ("Seattle"), wait for the async list, and click the qualified
  suggestion ("Seattle, Washington, United States") — a more-qualified option is
  the same place and IS correct. Never commit a different city ("Settle").
- A combobox stores its selected value **off the visible input** — verifying via
  `input.value` reads empty even when it's set. Trust the form's submit-time
  validation, not the DOM read, or a set field falsely gates.
- **Cloudflare Turnstile** ("You need to enable JavaScript…" token field) is
  usually present. It is a real CAPTCHA → gate to the human; never solve it.
- Some questions are conditional (appear after another answer); the form is
  re-read after fills, and the ReAct submit loop catches anything missed.

## Greenhouse (boards.greenhouse.io)
- Standard fields + an **EEO block** (gender, race, veteran, disability) near the
  bottom — resolve from the answer bank; don't skip.
- Location is often a **Google-places autocomplete** — same city-token → pick-
  suggestion handling as Ashby.
- Custom free-text questions are drafted by the writer if unbanked, else gate.

## Lever (jobs.lever.co)
- Single-page form. "Additional information" is free-text — writer-drafted or gate.
- Résumé upload triggers auto-parse; the pipeline fills fields itself rather than
  trusting the parse.

## Workday (myworkdayjobs.com)
- Multi-page, per-tenant. Expect an **account wall**, a résumé-parse-and-correct
  screen, and tenant questionnaires. An account/2FA wall → `no_account` gate.

## SmartRecruiters
- Consent / GDPR checkboxes are approved facts (checkbox picks), not essays.

## General
- A required field with no approved answer and no writer-eligible prose → gate
  (`unknown_field`). Never guess past a "required" marker.
- The submit loop reads the form's OWN error messages to know what to fix; it
  resubmits up to a few times, then gates on whatever the form still flags.
- Success = confirmation text ("application submitted", "thank you") OR a redirect
  to a confirmation URL. If the page closes before that, the result is `uncertain`
  (verify on the portal) — the engine never resubmits on its own.
