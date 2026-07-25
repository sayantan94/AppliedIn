---
name: site-quirks
description: Site-specific rules for filling and submitting job applications on a particular applicant-tracking system or employer portal, covering field controls that look ordinary but are not, submit wording, required-field order, and signals that falsely look like failure. Use when applying to a job posting, when an apply attempt stalls or is rejected, or when recording what was learned about a site so the next attempt succeeds.
---

# Site quirks

Rules for sites where the generic apply logic is not enough. Each file holds one
site's quirks and is injected into the apply task for that site only.

## Available sites

| Site | File | Applies to |
|---|---|---|
| Ashby | [ashby.md](ashby.md) | any `ashbyhq.com` posting |
| Greenhouse | [greenhouse.md](greenhouse.md) | any `greenhouse.io` posting |
| Workday | [workday.md](workday.md) | any `myworkdayjobs.com` posting |
| Uber | [companies/uber.md](companies/uber.md) | Uber only |

A company file and an ATS file both apply when both match; the company file is
read last and is the final word.

## Adding a site

Create `<ats>.md`, or `companies/<company>.md` for a single employer. Both are
picked up on the next apply — no code change, no restart.

```markdown
---
name: Greenhouse
match_hosts: [greenhouse.io]      # hostname contains any of these
match_companies: []               # or match by company name
allow_domains: []                 # extra hosts this apply may follow
success_phrases: []               # wording that means "really submitted"
---

- Location is a combobox: type 3-4 characters, wait for the listbox, click the option.
```

Every key is optional except one way to match, and `companies/<company>.md` matches
on its filename alone — so the smallest useful skill is a file containing one line
of prose.

## Writing rules that work

Record what the site **does**, as an instruction. One rule per line.

Good: `The submit button is disabled until the résumé upload finishes — wait for
the filename to appear before clicking it.`

Bad: `This site is unreliable and often fails.`

Three things are worth recording, because each one silently ruins an application:

1. **A control that is not what it looks like** — a combobox rendered as a text
   box, a dropdown that is really a button opening a listbox.
2. **What a real confirmation says**, when the wording is unusual enough that the
   generic patterns miss it. Put it in `success_phrases`.
3. **A signal that falsely looks like failure** — a CAPTCHA iframe left in the DOM
   after a successful submit, a redirect to a listings page.

Delete a rule when it stops being true. These are read on every apply to the site,
so a stale rule costs tokens and misleads the agent.
