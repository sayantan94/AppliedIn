---
name: Ashby
match_hosts: [ashbyhq.com]
success_phrases:
  - "your application has been submitted"
  - "your application was successfully submitted"
  - "application success"
---

- Every field is a `._fieldEntry` wrapper whose own `<label>` is the field title.
  The label is NOT bound to the input, so a field's real name is the wrapper's
  label — never the nearest section heading.
- **Location** is a combobox with placeholder "Start typing…", not a text box.
  Type 3–4 characters, wait for the listbox, then click the matching option. Typing
  the full string and moving on leaves it empty and the form will not submit.
- The résumé field is required. The submit button stays disabled until the upload
  finishes — wait for the filename to appear before clicking it.
- After a successful submit the page keeps its hCaptcha iframe in the DOM. That is
  NOT a CAPTCHA challenge and NOT a failure: if the page shows the success wording,
  the application is in. Do not re-submit.
- The confirmation replaces the form with a short "Application Success" panel.
  Read it as the confirmation rather than looking for an email.
- A job page is **not** the application. The fields sit behind an "Application"
  tab and reading the posting itself finds none at all — click the tab, or append
  `/application` to the job URL, before treating an empty form as a failure.
- Some boards put an "Autofill from resume" uploader above the real Résumé field.
  It is a PARSER: giving it the résumé re-populates the form and overwrites
  answers already typed. Attach only to the field labelled Resume/CV.
- A Yes/No question is **two visible `<button>`s plus a hidden checkbox** that
  carries the state — not a radio group. Clicking the input does nothing because
  it is `display:none`. Click the visible button whose text is the answer; the
  hidden input follows.

