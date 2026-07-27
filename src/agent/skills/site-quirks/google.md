---
name: Google
match_hosts: [careers.google.com, google.com]
match_companies: [Google]
success_phrases:
  - "application submitted"
  - "we have received your application"
  - "thanks for applying"
  - "your application was submitted"
---

- THERE IS A HARD CAP: "You've submitted N application(s) over the last 30 days.
  You can submit up to M more." Google allows **3 applications per 30 days**. Read
  that banner at the top of the flow BEFORE filling anything. If it says you have
  0 remaining, stop and report `application_limit` — do not spend a submission
  slot, and do not fill the form hoping it will go through.
- The application is a FOUR-STEP wizard, and the steps can already be complete
  from previous applications: 1 Careers profile, 2 Role Information,
  3 Voluntary self-identification, 4 Review & apply. A step with a blue tick is
  done; do not redo it. Only the numbered (unticked) steps need work.
- THE RÉSUMÉ IS NOT PART OF THIS APPLICATION. It lives on the persistent Google
  Careers **profile** (step 1) and is shared by every Google application. There is
  usually one attached already, e.g. "Sayantan_Resume_v2.pdf — Uploaded: last
  month". Check the filename and date against the résumé given for this task.
- To change it you MUST click **Edit** on the Careers profile first. The profile
  renders READ-ONLY by default: there is no file input in the DOM at all until
  edit mode opens, which is why "target the hidden file input directly" — correct
  on Greenhouse and Ashby — finds nothing here and the upload appears impossible.
  Order is: click Edit → the résumé control appears → replace the file → save.
- Because the résumé is profile-level, replacing it changes what every FUTURE
  Google application starts from. That is acceptable (the submitted application
  keeps the résumé as it was at submit time), but never remove the existing
  résumé without attaching the new one in the same pass — an empty profile
  résumé is worse than a slightly stale one.
- If the tailored résumé cannot be attached for any reason, do NOT submit with the
  old one silently. Report it, so the owner decides whether a generic résumé is
  acceptable for this role.
- Step 4 "Review & apply" is the only place that actually submits. Reaching step 4
  is not submitting; look for the explicit confirmation afterwards.
