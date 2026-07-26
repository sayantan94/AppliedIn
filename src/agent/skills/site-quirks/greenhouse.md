---
name: Greenhouse
match_hosts: [greenhouse.io, boards.greenhouse.io, job-boards.greenhouse.io]
success_phrases:
  - "thank you for applying"
  - "your application has been submitted"
---

- Labels are usually bound to inputs, so the generic reader is reliable here — do
  not second-guess a field whose label already reads sensibly.
- Custom questions live below the standard name/email/résumé block and are often
  required even when they look optional. Answer every one before submitting.
- "Attach, Dropbox, or manually enter" offers three résumé modes. Use **Attach**
  and set the file input directly; the manual-entry mode discards formatting.
- Demographic / EEOC questions at the bottom are voluntary. Select the decline
  option rather than leaving them blank when the form refuses to submit.
- Some Greenhouse boards gate the SUBMIT behind a reCAPTCHA / "security code"
  challenge that only appears after the button is clicked. If that appears, the
  application is not in — stop and hand the window to the person. Filling was not
  the problem, so do not re-fill or re-submit.
- The résumé control is a **button row** — "Attach", "Dropbox", "Google Drive",
  "Enter manually" — not a visible file input. Click **Attach** first; the real
  `input[type=file]` only becomes reachable after that. Uploading before clicking
  it silently does nothing.
- After the upload the filename appears next to the control with a small remove
  (×) beside it. That is the confirmation the attachment took. If the button row
  is still showing, it did not.
- Greenhouse is frequently EMBEDDED in the company's own careers page (an iframe,
  or a `gh_jid=` parameter on their URL). The form lives inside that embed, so a
  page that appears to have no fields usually has them one frame down.

