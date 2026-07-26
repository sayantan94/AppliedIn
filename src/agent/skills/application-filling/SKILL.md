---
name: application-filling
description: Fills and submits a job application using only human-approved facts, driving the owner's real Chrome via a subprocess agent, with a writer for free-text answers. Pauses for the human only on a genuinely unknown field, an account wall, or a CAPTCHA. Never fabricates answers, never solves CAPTCHAs, never double-submits. Use when applying to a job or completing an application portal.
---

# Application filling

You orchestrate one call — `apply_to_job()` — and turn its result into either a
"done" or a human gate. The heavy lifting is the engine below; you do not drive
the browser field-by-field yourself.

## What `apply_to_job()` does internally

It hands the posting to a `claude --chrome` subprocess that acts in the owner's
own browser — a real profile with real history, which is what portals accept.
There is no headless driver and no second engine.

1. Refuse outright if tracking already marks this job applied (a duplicate under
   the owner's real name is worse than a missed application).
2. Rewrite the posting to the ATS board's direct URL where one exists — a
   cross-origin embed blocks the résumé upload.
3. Open the posting and reach the actual form (Ashby "Application" tab,
   Greenhouse/Lever "Apply" button).
4. Set the **tailored** résumé on the real résumé input, never the optional
   "autofill from resume" uploader, and never by clicking an "Attach" button
   (that opens an OS file chooser and freezes the browser).
5. Fill from approved facts only. A free-text question with no banked answer goes
   to a **writer** model grounded in the résumé + GitHub + JD.
6. Submit, read the FORM's own validation errors, fix the flagged fields,
   resubmit. The form is the source of truth, not a DOM read.
7. Stop at the real blocker: a required field with no answer, an account wall, or
   a CAPTCHA — filled form left open for the human.

Every value the agent writes passes `guard_value()` first, so the guarantees hold
whatever the model decides: self-identification questions (disability, veteran
status, race, gender) can be declined but never affirmed, sanctions and
restricted-country questions always take the safe answer, and placeholder text
never reaches a field. A refusal is enforced in code, not requested in a prompt.

## Instructions

### Step 1: Apply
Call `apply_to_job()`. It returns one of:

- `{"status": "applied", "confirmation": ...}` — a real submission was confirmed
  (confirmation text or a confirmation redirect). Report it; you're done.
- `{"status": "gate", "reason": ..., "question": ...}` — a genuine blocker:
  `unknown_field` (a required field with no approved answer), `no_account`
  (login/signup wall), or `captcha`.
- `{"status": "failed", "reason": ..., "detail": ...}` — a real block:
  `duplicate_application`, `application_limit`, `already_applied`, or a
  `guardrail` refusal. Close it out; do not retry.
- `{"status": "uncertain" | "unknown", "detail": ...}` — the submit could not be
  confirmed (e.g. the browser was closed, or the run ended with no confirmation
  on the page). It did NOT resubmit.

### Step 2: Route the result
- `gate` → call `ask_human(question)` and STOP. Do not retry. The reply is banked
  as an approved fact and the run resumes; that field won't gate again.
- `uncertain`/`unknown` → call `ask_human(detail)` so the human verifies on the
  portal whether it went through, and STOP. Never re-run — a resubmit could
  double-apply.

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
- `references/ats-forms.md` — per-ATS quirks (Ashby autocomplete widgets +
  Turnstile, Greenhouse EEO + city picker, Workday account walls) for
  interpreting a gate.
- The `site-quirks` skill carries the accumulated per-board learnings that the
  apply subprocess is given directly.
