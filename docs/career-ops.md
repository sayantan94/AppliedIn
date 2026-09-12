# Career Ops search in AppliedIn

Open **Find jobs → Career Ops search** in the dashboard, or `http://127.0.0.1:8787/#career-ops`.
Enter an optional interest and press Enter or **Search jobs**. Leaving it blank
uses your saved role, location and keyword preferences. Broad search discovers
employers across the web; it is not restricted to companies on your watchlist.

Search activity streams the actual web queries and posting checks as they finish.
Expand or collapse **Search activity** to follow the run; completed activity is
saved with the search receipt. Interrupted connections fall back to polling.

Select results and choose **Prepare selected**, then **View pipeline**. This is
the same scoring, tailoring and final approval queue used by native discovery.
The company filter follows the prepared jobs. Importing a role never approves an
application, even with global auto-apply enabled.

**Advanced settings** contains company-feed scans, source selection, saved search
interests and separate six-hour schedules for feed scans and web searches.
Schedules repeat after the first completed search while the daemon is running
and unpaused. Manual searches work while paused. Automatic preparation is opt-in,
uses existing global/per-company preferences, saved posting-age windows and title
filters, and respects per-company `max_new_per_run`, up to 50 jobs per scan.

The integration has two distinct discovery paths:

- Career Ops' public HTTP providers: Greenhouse, Ashby, Lever, Workable,
  SmartRecruiters, Recruitee and Oracle Recruiting Cloud. Oracle uses its direct
  candidate-site feed. Its upstream provider caps a scan at 5,000 newest postings.
- Career Ops' broad-search workflow runs through your Claude Code subscription
  login, using its WebSearch and WebFetch tools. It requests at most four tool
  calls and returns up to 15 leads per search. This uses your Claude allowance.
  OpenAI API access is not used for search and is never a fallback.
  Unknown company feeds fall back to scoped web search instead of a disabled button.

Only URLs returned by the search tool are admitted. Known aggregator links are
excluded. Greenhouse, Ashby and Lever leads are checked against live employer
APIs; confirmed missing roles are removed. Temporary failures and other websites
remain visibly unverified. Search snippets never become job descriptions:
unverified hits must be read by the existing posting reader before tailoring.
Web search requires Claude Code signed in with a subscribed Claude account
(`claude auth login`). Public company feeds require no model login. Search uses
no Chrome session, résumé sharing or application submission. Résumé preparation and applying retain their
existing requirements.

## Automatic local setup

Run `./appliedin start`. Both **start** and **setup** check Git, Node.js 18+ and
Career Ops. Missing system tools are installed through Homebrew when available;
otherwise startup names the tool to install. No npm packages or browser
installation are needed.

The first run downloads the pinned providers into
`.local/integrations/career-ops`. Later starts check the installation locally,
without downloading it again. `APPLIEDIN_LOCAL_DIR` is respected from the
environment or `.env`. Downloads are staged so a failed install can be retried;
existing local edits are preserved and reported rather than overwritten.

Wait for any active applications to finish before restarting. The provider
contract is pinned to `da8c6f9193ac3d7a48a583f815b7d0feab742b81`; updating it requires
verifying the bridge and its provider outputs before changing `REVISION` in
`src/discovery/career_ops_setup.py`.

Results, dismissed jobs, source choices and receipts are private local data in
`.local/career-ops-board.json`. Already-handled URLs and existing tracking rows
are checked before a role enters the pipeline. A partial scan keeps completed
company results and reports failed sources. Posted dates come from the employer;
missing dates are shown as unavailable. Previously found jobs remain on the
board, with their last-seen timestamp; rediscovery does not prove a job remained
open between scans. Unverified web leads must be read before preparation; application checks still run at submission.

Upstream: [Career Ops](https://github.com/career-ops-hq/career-ops), MIT licensed.
The checkout keeps the upstream license. AppliedIn's bridge is in
`scripts/integrations/career-ops.mjs`; local routes and board persistence are in
`src/discovery/career_ops*.py`.

Verified source corrections live in `config/career_ops_sources.yaml`. These replace
entire upstream entries, so an obsolete API URL cannot override a corrected
careers URL. Restart after editing this source catalog.
