---
name: Workday
match_hosts: [myworkdayjobs.com, myworkdaysite.com]
---

- Workday is multi-page and requires an ACCOUNT before the form appears. If there
  is no saved session, stop and report that a login is needed — do not create one.
- Each page must be completed and "Save and Continue" clicked before the next
  page's fields exist. Never look for a single submit button on page one.
- Fields are custom widgets, not plain inputs: dropdowns are buttons that open a
  listbox, and dates are three separate segments. Click, wait for the popup, then
  choose — typing into them silently does nothing.
- The résumé upload usually triggers an auto-parse that OVERWRITES fields you
  already filled. Upload the résumé FIRST, let the parse settle, then correct the
  fields it got wrong.

## Starting: three ways in, and one of them is a trap

Clicking Apply opens "Start Your Application" with three buttons:

- **Autofill with Resume** — parses the résumé you attach and populates the
  experience pages from it. This is the one to use: the résumé given for THIS
  task is tailored to THIS posting, so the experience it writes is the right
  emphasis for the role.
- **Apply Manually** — a blank form. Slower, and it means typing what the parser
  would have written anyway.
- **Use My Last Application** — DO NOT USE. It copies the previous application,
  which means the résumé tailored for a DIFFERENT job, and its answers. It looks
  like a shortcut and it silently submits the wrong document.

## The pages, in order

My Information, My Experience, Application Questions, Voluntary Disclosures,
Self Identify, Review. Each needs "Save and Continue"; the next page's fields do
not exist until you do.

## My Experience: the page that matters

The parser fills this and gets much of it slightly wrong. Read every entry back
before continuing, because this page, not the résumé attachment, is what a
recruiter's search actually queries.

**Work experience.** One entry per role: Job Title, Company, Location, "I
currently work here", From and To dates, and Role Description.

- Dates are separate month and year segments, not a text field. Typing a date
  does nothing; set each segment.
- The parser commonly merges two roles at one employer into one entry, or loses
  the earliest role entirely. Count the entries against the résumé.
- Role Description is free text and is usually left empty by the parser. Fill it
  from the résumé's bullets for that role, in the résumé's own words. Do not
  invent achievements to fill space, and do not write a description for a role the
  résumé does not describe.
- "I currently work here" must be ticked for the present role, or the To date is
  required and any date entered there is a false statement about employment.

**Education.** Degree, institution, field of study, and dates. The parser
frequently gets the degree TYPE wrong (a Master of Science becomes a Bachelor's,
or the field is dropped). Correct it against the résumé exactly: a wrong degree is
not a formatting error, it is a false claim, and it is the field most likely to be
verified.

**Skills.** A token field: type a skill, press Enter, it becomes a chip. This is
what recruiter searches match on, so it is worth filling properly rather than
accepting whatever the parser guessed.


- Add every skill the SKILLS THE RÉSUMÉ EVIDENCES list in your task supports.
  Work through that list rather than improvising: it is the set the owner can
  defend in an interview.
- Prefer the exact terms the posting uses when the résumé evidences them, since
  that is what the search is looking for.
- Remove anything the parser invented that is not in the list. A skill is a claim
  about a person.
- Do not get stuck in the skill for long time, add the skills tha are best fit and move on to next step.

**Résumé.** Even with autofill, confirm the attached filename matches the résumé
given for this task. A profile that already has an older résumé will show that one
here, and the tailored version is the whole point of the run.
