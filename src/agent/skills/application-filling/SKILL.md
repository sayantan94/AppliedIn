---
name: application-filling
description: Fills and submits a job application using only human-approved facts, a deterministic Playwright pipeline for the mechanical work, a writer for free-text answers, and browser-use vision for stubborn widgets. Pauses for the human only on a genuinely unknown field, an account wall, or a CAPTCHA. Never fabricates answers, never solves CAPTCHAs, never double-submits. Use when applying to a job or completing an application portal.
---

# Application filling

You orchestrate one call — `apply_to_job()` — and turn its result into either a
"done" or a human gate. The heavy lifting is a deterministic engine (below); you
do not drive the browser field-by-field yourself.

## What `apply_to_job()` does internally

It runs the **scripted** apply engine (a Playwright pipeline), falling back to a
browser-use agent only for portals it can't parse:

1. Open the posting, reach the actual form (Ashby "Application" tab, Greenhouse/
   Lever "Apply" button).
2. Upload the **tailored** résumé onto the real résumé field (never the optional
   "autofill from resume" uploader).
3. ONE LLM call maps each field label → an approved fact key, `ESSAY`, or `SKIP`
   (values are substituted in code, so the model can't invent text).
4. Fill text/dropdowns, pick autocompletes (type the city → click the qualified
   suggestion, e.g. "Seattle" → "Seattle, Washington, United States"), set every
   radio/checkbox with real clicks.
5. For a free-text question with no banked answer, a **writer** model drafts one
   from the résumé + GitHub + JD (grounded, no fabrication).
6. **ReAct submit loop**: submit → read the FORM's own validation errors → fix the
   flagged fields (a short vision browser-use agent finishes stubborn autocompletes
   on the same page) → resubmit. The form is the source of truth, not a DOM read.
7. Stop at the real blocker: a required field with no answer, an account wall, or a
   CAPTCHA — filled form left open for the human.

## Instructions

### Step 1: Apply
Call `apply_to_job()`. It returns one of:

- `{"status": "applied", "confirmation": ...}` — a real submission was confirmed
  (confirmation text or a confirmation redirect). Report it; you're done.
- `{"status": "gate", "reason": ..., "question": ...}` — a genuine blocker:
  `unknown_field` (a required field with no approved answer), `no_account`
  (login/signup wall), or `captcha`.
- `{"status": "uncertain", "detail": ...}` — the submit could not be confirmed
  (e.g. the browser was closed before confirmation). It did NOT resubmit.

### Step 2: Route the result
- `gate` → call `ask_human(question)` and STOP. Do not retry. The reply is banked
  as an approved fact and the run resumes; that field won't gate again.
- `uncertain` → call `ask_human(detail)` so the human verifies on the portal
  whether it went through, and STOP. Never re-run — a resubmit could double-apply.

## Rules
- Never fabricate. Typed values come only from approved facts or a writer answer
  grounded in the real résumé; everything else gates.
- Never solve a CAPTCHA and never create an account — both gate to the human.
- Never re-run or resubmit an application that may already have gone through.
- Trust the form's validation over any DOM read (a React combobox stores its value
  off the visible input; reading it as "empty" would falsely gate a set field).
- A job whose posting states it does NOT sponsor work visas is closed as failed
  before applying (the candidate requires sponsorship) — it never reaches here.

## Reference
`references/ats-forms.md` — per-ATS quirks (Ashby autocomplete widgets + Turnstile,
Greenhouse EEO + city picker, Workday account walls) for interpreting a gate.
