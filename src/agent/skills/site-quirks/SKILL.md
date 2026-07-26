---
name: site-quirks
description: Site-specific rules for filling and submitting job applications on a particular applicant-tracking system or employer portal, covering field controls that look ordinary but are not, submit wording, required-field order, and signals that falsely look like failure. Use when applying to a job posting, when an apply attempt stalls or is rejected, or when recording what was learned about a site so the next attempt succeeds.
---

# Site quirks

Rules for sites where the generic apply logic is not enough. Each file holds one
site's quirks and is injected into the apply task for that site only.

## Available sites

### By ATS — applies to every posting on that system

| ATS | File | Matches |
|---|---|---|
| Ashby | [ashby.md](ashby.md) | `ashbyhq.com` |
| Greenhouse | [greenhouse.md](greenhouse.md) | `greenhouse.io`, `boards.greenhouse.io`, `job-boards.greenhouse.io` |
| Workday | [workday.md](workday.md) | `myworkdayjobs.com`, `myworkdaysite.com` |

### By company — applies to that employer only

| Company | File | Why it needs one |
|---|---|---|
| Airbnb | [companies/airbnb.md](companies/airbnb.md) | embedded form, combobox-heavy |
| Anthropic | [companies/anthropic.md](companies/anthropic.md) | combobox-heavy |
| Cerebras | [companies/cerebras.md](companies/cerebras.md) | sanctions question |
| Databricks | [companies/databricks.md](companies/databricks.md) | embedded form, off-site redirect |
| Datadog | [companies/datadog.md](companies/datadog.md) | embedded form, combobox-heavy |
| Figma | [companies/figma.md](companies/figma.md) | combobox-heavy |
| Google DeepMind | [companies/google-deepmind.md](companies/google-deepmind.md) | combobox-heavy |
| Nebius | [companies/nebius.md](companies/nebius.md) | embedded form, combobox-heavy |
| Notion | [companies/notion.md](companies/notion.md) | sanctions question |
| OpenAI | [companies/openai.md](companies/openai.md) | sanctions question |
| Replit | [companies/replit.md](companies/replit.md) | sanctions question |
| Rivian | [companies/rivian.md](companies/rivian.md) | sanctions question |
| Scale AI | [companies/scale-ai.md](companies/scale-ai.md) | combobox-heavy |
| Snowflake | [companies/snowflake.md](companies/snowflake.md) | sanctions question |
| Stripe | [companies/stripe.md](companies/stripe.md) | embedded form, combobox-heavy |
| Uber | [companies/uber.md](companies/uber.md) | off-site redirect |
| Vercel | [companies/vercel.md](companies/vercel.md) | combobox-heavy |

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

## Uploading a file, on any board

Point the upload tool at the form's `input[type=file]` directly. It sets the file
on the input; no dialog is involved.

Never click "Attach", "Upload a file", "Choose file", or a drop zone to reach it.
Those open the operating system's file chooser, which is not part of the page —
it cannot be seen or driven, and the browser stops responding to anything else
until it is dismissed. The input is usually hidden behind that button rather than
missing, so target it even when it is invisible.

