---
name: Oracle
match_hosts: [careers.oracle.com, oraclecloud.com, eeho.fa.us2.oraclecloud.com]
success_phrases:
  - "thank you for applying"
  - "your application has been submitted"
  - "application submitted"
  - "we have received your application"
---

- APPLY ON ORACLE'S CANDIDATE SITE, NEVER ON THE POSTING PAGE.
  `careers.oracle.com/en/sites/jobsearch/job/<id>/` embeds "LinkedIn Embedded
  Content". An ad blocker replaces that frame with a page belonging to its OWN
  extension, and Chrome then refuses to let any other extension act on the tab:
  "Cannot access a chrome-extension:// URL of different extension".
- That failure is deceptive, so recognise it rather than trusting it. Page reads
  keep working and show the posting with its Apply Now button, while every click,
  screenshot and JavaScript call fails. It looks like the posting cannot be filled
  when in fact the tab is poisoned. Recreating the tab, the tab group or the whole
  session does NOT help — the embed is re-injected on every load.
- The identical application is served from Oracle's own candidate site with no
  social embed, and everything works normally there:
  `https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/jobsearch/job/<id>/apply/email`
  `direct_board_url()` already rewrites to this, so you should land there. Do not
  navigate back to careers.oracle.com to "check" the posting first — that
  re-poisons the tab and the apply cannot recover.
- The flow opens on "You don't need to have an account": an email field, a terms
  and conditions checkbox, and NEXT. No signup is required, so do not create an
  account. Tick the terms box, it is required.
- The owner may already be signed in on careers.oracle.com. That session does not
  carry into the apply flow and does not need to.
- Ignore the "Are You Still With Us?" session-timeout dialog and the "Oracle
  Assistant" chat widget on the posting page. Neither is part of the application.
- After NEXT you may land on a "Confirm Your Identity" screen asking for a 6-digit
  code emailed to the address you entered. If this Chrome profile has applied
  through this flow before with the same email, the screen can resolve on its own
  within a couple seconds with no code entered — Oracle recognizes the existing
  candidate session/cookie. Don't panic and don't try to fetch the code yourself;
  just wait ~2s and re-check the URL/screenshot before assuming you're stuck. If
  it does NOT clear and still shows the code boxes, that's a genuine blocker
  (report needs_owner) since the code goes to the candidate's inbox, not one this
  agent can read.
- Returning-profile prefill can be stale: the résumé, education, and experience
  entries on section 2 may auto-populate from whatever was attached on a PREVIOUS
  application with this email, not the résumé tailored for this job. Check the
  attached filename — if it doesn't match the résumé path given for this task,
  click REMOVE and upload the correct file via the file input (ref inside the
  "Drop Resume Here" application/dropzone, not the dropzone div itself).
- Section 3 ("More About You") also prefills from the prior profile, including
  Earliest Available Date — verify it against the approved answer and correct the
  Month/Day dropdowns if stale (they're comboboxes: click to open, then click the
  option; typing via form_input alone doesn't commit the value, and Day can show
  a filtered suggestion list that needs an explicit click).
- The final page of section 3 has an E-Signature "Full Name" text field right
  above SUBMIT — required, not optional; type the full legal name before
  submitting.
- On success you land on the candidate's My Applications / profile page (not a
  dedicated "thank you" page), with a transient toast reading "Thank you for your
  job application." and the role listed under Active Job Applications with a
  status like "Under Consideration" and an Applied-on date.
