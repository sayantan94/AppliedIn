---
name: Databricks
match_companies: [databricks]
allow_domains: [job-boards.greenhouse.io, boards.greenhouse.io]
success_phrases:
  - "thank you for applying"
  - "your application has been submitted"
---

- The job page hosts the application in a **Greenhouse iframe**. The page itself
  has no form fields and no file input, so reading or filling the top-level
  document finds nothing to do. Work inside the frame whose URL contains
  `greenhouse.io`, or go straight to the embed (below).
- Fastest route: take the `gh_jid` value from the job URL's query string and open
  `https://job-boards.greenhouse.io/embed/job_app?for=databricks&token=<gh_jid>`.
  That serves the same form standalone — no iframe, résumé field present. The long
  `validityToken` on the embedded copy is not required.
- Do not use `job-boards.greenhouse.io/databricks/jobs/<id>`; it redirects back to
  the Databricks page and shows no form.
- The résumé row offers Attach / Dropbox / Google Drive. Use **Attach** and set the
  file input directly.
- **Submitting requires a CAPTCHA.** Clicking "Submit application" reveals a
  reCAPTCHA plus an 8-box "Security code — confirm you're a human" field, and the
  application does NOT go through until a person completes it. Nothing on the page
  says this before the click, so a run that fills the form perfectly still ends
  with no confirmation. Treat this as a human handoff (BLOCKED: captcha), not a
  failure to fill — never attempt to solve it.

