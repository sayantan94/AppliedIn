# Release notes

## v1.2.1 — 2026-11-10 — Safer applying and independent instances

- Never apply twice: a submission the page did not confirm is marked uncertain
  and is not retried. Adding a job you already applied to is refused.
- An application is not sent without its résumé PDF.
- Instances on different ports are fully separate, including live activity.
- The dashboard only accepts requests addressed to this machine.
- Reset clears the pipeline and keeps your settings. Stop ends only the run
  you chose.
- "Top N per run" now applies to every company, not only job-board feeds.
- **Add a job**: "Add a new company" is at the top of the list.
- "Applied today" shows today's count. New identity opens again.

## 2026-09-14 — Clearer application details and compact workflow

- Add a compact Find → Prepare → Apply guide to Career Ops search, with aligned
  application columns and clearer status badges.
- Show the résumé, match score, submission status, and application date together
  in role details. Keep requested dates separate from confirmed application dates.
- Wrap the Fresh jobs Scan button label within the sidebar at narrow widths.

Validation: 46 JavaScript tests passed. Browser checks covered light and dark
themes, compact application rows, role details, and Scan button sizing from
390px to 1440px wide.

## 2026-09-14 — Shared agent instructions and clearer job review

- Add **Agent instructions** to the top navigation. Edit or import a private
  steering file shared with all agent paths, including Claude and Codex search.
  Each instance keeps its own file; stale saves cannot overwrite newer edits.
- Keep the Career Ops sidebar layout, with compact job rows, a role details
  drawer, and selection actions that remain accessible while scrolling.
- Show the latest search activity with readable elapsed times, grouped repeated
  updates, and filters for matches and issues.
- Hide already-applied jobs from Matching jobs, including roles submitted through
  another pipeline view. Group Your applications by status using compact rows;
  completed applications remain available in collapsed history.
- Install the Career Ops scanner in the selected instance's directory at startup.
  Report scanner failures explicitly instead of presenting them as empty results.

## 2026-09-13 — Career Ops search and application progress

### Find jobs

- Search from one embedded form: choose Claude or Codex, job boards, roles,
  locations, interests, and posting dates. Extra preferences stay tucked away.
- Discover employers beyond your watchlist through Career Ops' company
  directories for Greenhouse, Lever, Ashby, Workday, and iCIMS.
- See search activity and matches as they arrive. Stop a search without losing
  completed results, or continue from the next batch of companies.
- View coverage, unreachable boards, and missing posting dates explicitly.
  Directory scans retain every matching result; a partial scan does not imply
  that every available job has been checked.
- Keep selections and preference drafts stable while results refresh. Search
  controls sit beside the job board on desktop and stack on smaller screens.

### Apply and follow progress

- **Apply selected** authorizes scoring, résumé tailoring, and final application
  for the checked roles. It also works while scheduled automation is paused.
- **Score & tailor selected** prepares résumés and stops for review.
- **Your applications** appears immediately after Apply selected. It shows
  queued, scoring, tailoring, browser application, and final outcomes without
  leaving the search page. Progress refreshes every six seconds and survives
  page reloads and search-filter changes.
- Successful applications show the application date. Roles needing attention
  show the reason and a link to review them in the existing pipeline.
- A lost connection or failed request remains visibly unconfirmed. Applications
  are only marked applied after employer confirmation; existing score gates,
  duplicate protection, résumé checks, and required-answer gates still apply.
- Selecting roles never approves other jobs at the same company.

### Setup and compatibility

- Startup checks and installs missing Career Ops scanner dependencies with npm
  lifecycle scripts disabled. No separate upstream UI server is required.
- Career Ops web search uses the selected client's subscription login, without
  silently falling back to API-key billing. This choice controls search;
  scoring, tailoring, and browser submission use AppliedIn's existing services.
- The default scan checks 150 companies per selected source. Continue a scan to
  check more. AI search supplements directory reads with up to 30 leads; source
  pagination limits and unreachable boards can still limit coverage.
- Restart AppliedIn after updating the Python code, once active applications
  finish, then hard-refresh the dashboard to load the updated UI.

Validation: 592 Python tests and 45 JavaScript tests passed. Isolated browser
checks covered exact selections, immediate feedback, progress after reload,
failed requests, and mobile layout, with application requests mocked.
