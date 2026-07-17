---
name: job-discovery
description: Finds new job postings across a watchlist of companies and enqueues the ones matching the candidate's preferences. Use when discovering, finding, or polling for new jobs to apply to. Handles both ATS feeds and custom career pages.
---

# Job discovery

Find every new, matching job across the watchlist and enqueue it for the
application pipeline. Be exhaustive — a missed posting is a missed opportunity.

## Instructions

### Step 1: Get the watchlist
Call `list_companies`. It returns each company with its careers URL and whether
it's a feed or a crawl target.

### Step 2: Discover each company
Call `discover_company(name)` for **every** company in the list — do not skip
any. It resolves the company's ATS from the careers URL, fetches the feed (or
crawls a custom page), filters to the candidate's preferences, dedups against
what's already been seen, and enqueues the new matches. It returns how many new
jobs it enqueued.

### Step 3: Report
Sum the enqueued counts and list any company that returned an error, so the
watchlist can be fixed.

## Rules
- Discovery is idempotent: a posting already seen is skipped (deterministic
  dedup). Running twice never double-enqueues — safe to run on a schedule.
- Only postings passing the stage-1 preference filter are enqueued; the deeper
  LLM match-score happens later, in the pipeline's scorer.
- If a company errors (unreachable page, changed ATS), report it and move on —
  one bad company must not stop discovery.
