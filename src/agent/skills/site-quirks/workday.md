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
