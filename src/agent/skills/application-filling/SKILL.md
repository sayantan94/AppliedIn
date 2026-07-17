---
name: application-filling
description: Fills and submits a job application using only human-approved answers, delegating the browser work to a browser agent and pausing for the human on any unapproved field, account wall, or CAPTCHA. Use when applying to a job, filling an application form, or completing a job portal. Never guesses answers or solves CAPTCHAs.
---

# Application filling

The actual form-filling is done by a browser agent (browser-use). You orchestrate
it: kick it off with the approved answers, then turn anything it can't resolve
into a human gate. You do not drive the browser yourself.

## Instructions

### Step 1: Apply
Call `apply_to_job()`. It hands the browser agent this job's URL plus every
human-approved answer for the company (global facts + company answers + the saved
portal login) and tells it to fill and submit using ONLY those answers. It
returns one of:

- `{"status": "applied", "confirmation": ...}` — submitted. Report the
  confirmation and you're done.
- `{"status": "gate", "reason": ..., "question": ...}` — it hit something it
  couldn't resolve from approved data (a required field with no answer, an
  account wall, or a CAPTCHA).
- `{"status": "unknown", "detail": ...}` — it finished without a clear result.

### Step 2: Gate the unknowns to the human
On `status: "gate"`, call `ask_human(question)` with the returned `question` and
STOP. Do not retry the application. The human's reply is saved as an approved
fact, and the run resumes from here — on the next attempt that field (or the now
account/login) is approved and won't gate again.

On `status: "unknown"`, call `ask_human` with the `detail` so the human can
decide, and stop.

## Rules
- Never fabricate an answer. The only inputs the browser agent is allowed to type
  come from approved facts or a human reply — the gate exists for everything else.
- Never solve a CAPTCHA and never create an account yourself. Both come back as a
  gate; hand them to the human.
- One unresolved required field gates the whole application. That is deliberate.

## Reference
`references/ats-forms.md` documents per-ATS quirks (Greenhouse EEO block, Workday
account creation, Ashby conditional fields, …). It's background for interpreting a
gate `reason` and knowing what the human will need to do — the browser agent
handles the navigation itself.
