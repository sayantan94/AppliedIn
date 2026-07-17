# ATS form navigation (reference)

Per-ATS quirks for Step 1 (reading the form) and Step 4 (fill & submit).
Consult when the form doesn't behave as expected.

## Greenhouse
- Standard fields: first/last name, email, phone, resume upload, LinkedIn.
- EEO block (gender, race, veteran, disability) is a separate section near the
  bottom — always present. These resolve from the answer bank; don't skip them.
- Custom questions vary per company; treat any free-text box as gated.

## Lever
- Single-page form. "Additional information" is a free-text box — gate it unless
  the company's answer bank already has it.
- Resume upload triggers auto-parse; verify the parsed fields, don't trust them.

## Ashby
- Multi-section. Some questions are conditional (appear after you answer another).
  Re-read the form after each answer before deciding you're done.

## Workday
- Multi-page, per-tenant. Expect account creation, a resume-parse-and-correct
  screen, and tenant-specific questionnaires.
- If it demands account creation or 2FA you can't complete, `ask_human` and stop.

## SmartRecruiters
- Consent/GDPR checkboxes are common — these are approved facts, not essays.

## General
- A required field with no approved answer ALWAYS gates. Never guess to get past
  a "required" marker.
- After submit, capture the confirmation number / "application received" text as
  the confirmation id.
