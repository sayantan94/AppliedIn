---
name: Microsoft
match_hosts: [careers.microsoft.com, apply.careers.microsoft.com]
match_companies: [Microsoft]
success_phrases:
  - "your application has been submitted"
  - "thank you for applying"
  - "application submitted"
  - "we have received your application"
---

Microsoft runs its own portal at `apply.careers.microsoft.com`, not Greenhouse or
Workday. Apply now leads to a wizard down the left: Application location(s),
Resume, Contact Information, Work Authorization, Self identification, Candidate
questions. Each step must be completed before the next.

The form AUTOSAVES ("Changes saved less than a minute ago"), so a half filled
application persists between visits. Do not assume a blank form.

## The résumé step, which is where this goes wrong

The field is NOT a plain file input. It is a dropdown of résumés already uploaded
to the account, with a Preview button beside it, and separately:

    [ Sayantan Bhowmik Resume.pdf        v ]  [ Preview ]
    OR  [ Upload new ]   [ Apply With LinkedIn ]

**Delete whatever résumé is already there, then upload the new one.** Every
résumé this pipeline produces is called "<Full name> Resume.pdf", so the dropdown
fills up with entries that all read the same and there is no way to tell the
current one from last week's. Removing the old one first leaves exactly one
option, which is then unambiguous. Concretely:

1. If the dropdown already has a résumé, click it to open the preview overlay,
   click **Delete**, and confirm. Repeat until none are left.
2. Close the overlay with the X at the top right if it is still open.
3. Set the résumé on **Upload new**, which IS the file input: its element type is
   `file`, so give it the file directly. Do not CLICK it — a click opens the
   operating system's file chooser, which is not part of the page, cannot be
   seen or controlled, and freezes the browser while it is open.
4. Confirm the dropdown now shows exactly one résumé and it is selected.

The preview overlay is the only place Delete exists, so opening it on purpose is
correct. What is not correct is landing there by accident during an upload: it
covers the form, an upload attempt made underneath it looks like it did nothing,
and everything after fails because the page beneath is unreachable. Open it to
delete, close it, then upload.

- Microsoft asks that the résumé redact age, date of birth, and dates of
  attendance or graduation. Do not edit the résumé to comply: it is the owner's
  document. If it carries education dates, say so in the report rather than
  altering it or skipping the field.

## The rest

- The location step usually has one option and is preselected. Confirm it rather
  than changing it, since it drives the work authorization questions that follow.
- Contact information is prefilled from the account. Check it against the approved
  facts instead of trusting it.
## Signing in

Do not assume there is a session. Clicking Apply now often opens a **Sign in**
modal offering Microsoft, LinkedIn, Google and Facebook, plus Create an account;
the top right of the page reads "Sign in" rather than an account name. This is
normal, not a fault, and it is not a reason to stop.

Take **Sign in using Google**, under the rule that governs every portal: only if
the Google account it is already signed in as matches the email this application
is going out under. If it names a different account, opens an account chooser, or
asks for a password, stop and report it. Never create an account, never switch
accounts, and never type a password.

If Google is not offered, or the account does not match, report that the posting
needs a signed-in session rather than working around it.
