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
- **A cap belongs to ONE employer, never to the board.** Ashby is software that
  hundreds of companies use. Each configures its own limit, or none at all, and
  the great majority set none. So a refusal at Ramp says nothing whatsoever about
  Snowflake, OpenAI or Perplexity, and a cap you hit earlier today says nothing
  about the company in front of you now.
  Do not carry a limit across companies, across roles at a different company, or
  across sessions. Do not reason from "this happened on an Ashby board before" to
  "it will happen here". ALWAYS fill the form and submit. Only what THIS
  employer's board says, in response to THIS submission, counts.
- **The application-limit banner is a NOTICE, not a stop.** Some boards show "we
  have set up limits for applications across roles. Candidates may not apply more
  than 5 times in any 180 day span". Fill the form and submit anyway. That wording
  is a statement of policy, not a live counter: it never says how many have been
  used or how many are left, so it cannot tell you whether THIS submission is over
  the line — and the board, not this agent, is the one that decides. Treating it
  as a refusal abandons applications that would have gone through.
  Report `application_limit` only when the site actually REFUSES the submission,
  in words that say so after clicking Submit. Contrast Google, which shows a real
  counter ("you can submit up to M more"); at 0 remaining that one IS a stop,
  because it states the answer outright.
- A rejected submit can answer "your application submission was flagged as possible
  spam… please submit your application again" **and blank every field**. Clicking
  Submit again straight away sends an EMPTY application, which looks far more like
  a bot than the attempt that was flagged. Re-enter the answers, wait, then
  resubmit — and never resubmit more than twice.
- **Required is marked with an asterisk in the label, not with `required` or
  `aria-required`.** The location combobox and the arbitration acknowledgement
  both carry a red `*` and neither HTML attribute, so a form can look complete
  while two mandatory fields are empty. Trust the asterisk.
- An acknowledgement ("I acknowledge that I have opened, read, and understood the
  Arbitration Agreement", "I confirm I have read the above") is a **lone checkbox**
  with no Yes/No options — tick the box itself. Only do so when the owner has
  explicitly answered it; an arbitration clause waives their right to sue and is
  never affirmed on a guess.
- Race and ethnicity are often ONE dropdown whose options encode both, e.g.
  "Asian (Not Hispanic or Latino)". Pick the single option matching both of the
  owner's answers rather than looking for two separate questions.
- Submitting can NAVIGATE rather than swap the form for a panel. When it does, the
  confirmation is on the page you land on, not the one you clicked from — wait for
  it to settle and read it before concluding nothing happened. Reporting "no
  confirmation" without looking at the destination leaves the owner unsure whether
  they have applied, which is the one state worse than a clean failure.

