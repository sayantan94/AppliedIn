# Working on AppliedIn

For anyone changing this code, human or agent. The [README](README.md) covers
installing and using it; this covers where things live, how to extend them, and
the failures that are expensive to rediscover.

**Asked to set it up and start the server? Go straight to [Setup](#setup-set-this-repo-up-and-start-the-server).**
It is scripted end to end; the only things you need from the human are an API key,
a résumé and a company or two.

The one rule that outranks the rest: **an application is sent under a real
person's name and cannot be recalled.** A missed application costs an
opportunity. A wrong one costs credibility. When a change trades safety for
throughput, it is the wrong change.

---

## Setup: "set this repo up and start the server"

This is the most common thing you will be asked, and it is fully scripted, so
follow it rather than improvising. Work through the steps in order and verify each
one before the next. Do not run them all at once and hope.

Steps 3 to 5 need things only the human has: an API key, their résumé, and which
companies they care about. **Ask for those. Never invent them**, and never write a
key into a file you have not read first.

There is exactly one entry point, `./appliedin`. If you find yourself reaching for
another script, you are looking at stale instructions.

### 1. Install

```bash
./appliedin setup
```

Installs uv, Python 3.12, the dependencies, Redis (queues and runtime flags) and
Tectonic (renders the résumé PDF). Idempotent, so re-running is safe.

`./appliedin` is the only entry point. `start` runs this for you when the
dependencies have moved, so this step is really just "get it wrong early rather
than at launch".

Verify: `.venv` exists and `redis-cli ping` answers `PONG`.

### 2. Confirm what actually submits applications

```bash
command -v claude
```

Applications are filled in by a `claude --chrome` subprocess driving the human's
own Chrome. That needs the Claude Code CLI and a Claude **subscription**; it
refuses API-key auth. Discovery, scoring and tailoring all work without it, so
this is a warning rather than a blocker. Say which of the two situations they are
in rather than letting them find out at the first apply.

### 3. The API key

Setup copies `.env.example` to `.env`. It needs one line filled in:

```
OPENAI_API_KEY=sk-...
```

That is the key that gates startup, because orchestration (discovery, scoring,
tailoring, the writer) runs on OpenAI by default.

`ANTHROPIC_API_KEY` is **not** what makes applying work, which is the mistake to
head off: applying goes through the Claude CLI's own subscription session, never
an API key. The commented line in `.env.example` is only for pointing an
individual stage at an Anthropic model through LiteLLM, which is optional.

**Ask the human for the key. Never invent one, and never paste one into a file
you did not read first.**

### 4. The résumé

`resume/base.tex`, in LaTeX, because tailoring edits the source and Tectonic
renders the PDF from it. Without it, tailoring has nothing to work from and the
apply has nothing to attach.

Ask them to save it there. If they have a PDF or Word file, say plainly that it
has to be LaTeX and offer to help convert it; do not fabricate a résumé.

Verify: `.venv/bin/python -c "from tools.render import render_pdf; render_pdf(open('resume/base.tex').read())"`
renders without raising.

### 5. What to look for, and what counts as a match

- `config/watchlist.yaml` — the companies. Tracked in git, so treat it as public.
  Each entry needs a `discovery` mode; see [Adding a company](#adding-a-company).
- `config/preferences.yaml` — titles, seniority, locations, the score bar. Also
  tracked, also public.
- `.local/facts.md` — the answers used to fill forms, seeded from
  `facts.seed.example.yaml`. This one is private and gitignored.

These can be edited later in the dashboard, so a first run does not need them
perfect. It does need at least one company, or discovery has nothing to scan.

### 6. Start it

```bash
./appliedin start
```

Installs anything missing, brings up Redis, checks the config, then launches the
daemon in the background. Dashboard on `http://127.0.0.1:8787`.

Verify it is really up before reporting success:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/
```

Expect `200`. Then tell them the first run does nothing on its own: discovery is
on a six hour schedule and does not sweep at boot, so they should press **Discover**
scoped to one company to see the pipeline work end to end.

### When it does not work

| symptom | cause |
|---|---|
| `paste your key into .env` and they already did | the key must be `OPENAI_API_KEY`. `ANTHROPIC_API_KEY` is not used anywhere. |
| dashboard loads, nothing ever happens | check the deck for **paused**. That flag lives in Redis and survives restarts. |
| discovery runs, finds nothing | expected if the watchlist is empty, or the company needs `discovery: browser`. |
| tailoring cannot render a PDF | Tectonic missing, or a LaTeX error in `resume/base.tex`. Render it by hand for the real message. |
| apply does nothing | no `claude` on PATH, or it is authenticated with an API key rather than a subscription. |

## Layout

```
src/
  agent/
    graph.py         the ADK pipeline: score, tailor, critic loop, gates
    run.py           run_job / resume_job / _apply_direct, the apply control path
    skills/          markdown instructions handed to the agents
      site-quirks/   per site learnings (apply AND discovery)
  discovery/
    handler.py       feed discovery, per company preference overrides
    crawler.py       career pages with no feed: fetch, then escalate to Chrome
    chrome_crawl.py  the browser finder and its prompt
    relevance.py     the screen that decides what is worth tailoring
    watchlist.py     watchlist.yaml + preferences.yaml loaders
  core/
    flags.py         runtime settings in Redis, incl. per company preferences
    models.py        Status, DiscoveryMode, JobRecord
    stores.py        the storage factory. Never construct storage directly
  tools/
    claude_chrome.py THE apply engine: runs `claude --chrome` as a subprocess
    browser_apply.py apply() dispatch and the duplicate guard
    company_skills.py loads site-quirks for a URL or company
web/                 the dashboard: app.js, styles.css, dashboard.html
tests/               pytest, offline, no network
```

`src/tools/claude_chrome.py` is where an application actually happens. Read it
before changing anything about applying.

---

## Running it during development

```bash
./appliedin start | stop | status | logs
./appliedin start --no-discover      # dashboard + queue worker, crawler off
.venv/bin/python -m daemon           # foreground, log straight to your terminal
.venv/bin/python -m pytest -q        # tests, offline, seconds
```

`./appliedin` is the only entry point: `start` is background and writes to
`.local/daemon.log`. Run the module directly when you want it in the foreground
with the log where you are already looking, which is usually what you want while
changing code.

**The daemon does not hot reload.** Every change to `src/` needs a restart, and
the browser caches `app.js` and `styles.css`, so a UI change needs a hard reload
too. More than one confusing session has come from testing stale code.

## Adding a job preference

Preferences exist twice: globally in `config/preferences.yaml`, and per company
as overrides in Redis. A company inherits every field it does not override.

To add a field end to end:

1. **`src/discovery/watchlist.py`** add it to `Preferences` with a default.
2. **`src/discovery/relevance.py`** use it. A field nothing reads is a lie in the
   interface.
3. **`src/core/flags.py`** add its name to `COMPANY_PREF_FIELDS` if it can vary
   per company. `effective_prefs()` merges automatically once it is listed.
4. **`src/server.py`** in `company_prefs()`, add it to the list coercion or the
   integer coercion if it is not a plain string.
5. **`web/app.js`** add it to `CPREF_FIELDS` with the right flag: `list`, `num`,
   `bool`, `area`, or `prof`.

**The part that is easy to get wrong.** The per company pane pre-fills each field
with the shared value, so "unchanged" must mean "still shares it" and store
nothing. Writing an override that merely copies today's default silently detaches
the company: a later change to the global value moves every other company and
leaves that one behind, with nothing on screen explaining why. `commitCpref` in
`app.js` compares against the default and clears the override when they match.
Keep that property.

**Known gap.** Changing a company's preferences does not re-screen its existing
backlog. The older `/actions/company-filter` endpoint does reconcile; the newer
preferences endpoint does not. Worth fixing.

---

## Adding a site quirk

`src/agent/skills/site-quirks/<name>.md`, loaded by `tools/company_skills.py` and
injected into both the apply prompt and the discovery crawl prompt.

```markdown
---
name: Acme
match_hosts: [acme.com, jobs.acme.com]   # matched against the HOST only
match_companies: [Acme]                  # or by watchlist name
success_phrases:
  - "thank you for applying"
---

- One learning per bullet, in the imperative, with the reason.
```

`match_hosts` is compared against the hostname, so a path fragment such as
`google.com/about/careers` never matches. Use the host and add `match_companies`.

Write these from the live page, not from memory. `site-quirks/oracle.md` and
`apple.md` were both written after reading the real thing, and both contain a
detail that would never have been guessed.

---

## Adding a company

`config/watchlist.yaml`. Pick the discovery mode deliberately:

| mode      | when |
|-----------|------|
| `feed`    | a real ATS feed exists (Greenhouse, Ashby, Lever, Workday). Always prefer this: fast, free, complete. |
| `crawl`   | a custom careers page that a plain HTTP fetch can read. |
| `browser` | the listing is built in the browser. Always read in Chrome, skip the fetch. |

Choose `browser` when a page renders its results client side. The older rule
escalated to Chrome only when nothing relevant was found, which never fires on
these pages: a short window of the listing usually contains a match or two, so a
truncated page looks exactly like a complete one. Apple was returning twenty of
six hundred postings on every rescan for this reason.

---

## Debugging

**The habit that matters: get evidence before forming a theory.** The Oracle
apply failure in this repo was diagnosed by theory three times and by evidence
once. Only the evidence was right, and the fix took ten minutes once the raw
report was in the log.

When the browser agent reports a failure:

1. **Read the session's own words.** `claude_chrome.py` logs the full report and
   session output on a conflict. A one line verdict names a symptom; the report
   names the cause.
2. **Reproduce outside the daemon.** Run `claude --chrome -p "..."` by hand with
   the same flags. If it fails there too, the daemon is not involved.
3. **Bisect the page.** Try a different URL on the same domain. That is what
   isolated the Oracle bug: `example.com` worked, the posting failed, and Oracle's
   own 404 page worked, which ruled out both the domain and the extension and left
   only the page.

**"The page can be read but not clicked."** Reads succeeding while every click,
screenshot and JavaScript call fails with `Cannot access a chrome-extension:// URL
of different extension` means a browser extension has injected a frame into the
page. An ad blocker replacing a social embed does this. Chrome then refuses to let
any other extension act on that tab. It is not a broken posting and not a second
Claude session; the fix is to apply on a URL without the embed. This is handled by
`direct_board_url()` and detected by `_is_browser_conflict()`.

**Nothing happens when I click Apply.** Check the daemon log before assuming a
dead button. Reading the posting is its own browser subprocess and can take a
minute; status and events are emitted before it starts so the interface is not
silent, but the work is real and slow.

**Discovery found nothing new.** The seen ledger only blocks postings already
processed, so genuinely new ones always pass. If nothing new appears, the fetch
probably never saw them: check whether the company should be `discovery: browser`.

---

## Invariants

These are enforced in code, not requested in prompts, because a rule that lives
only in a prompt is a rule nobody is enforcing. Do not move them into a prompt.

- **Never declare a protected characteristic.** `guard_value()` in
  `claude_chrome.py` intercepts every value the model writes. Disability, veteran
  status, race and gender may be declined but never affirmed. Negatives must still
  go through, or a required field is left blank rather than correctly declined.
- **Never apply twice.** Checked in `browser_apply.apply()` and again immediately
  before submit.
- **Never record an application the page did not confirm.** An unconfirmed submit
  is `uncertain`, never `applied`.
- **Sanctions and restricted country questions take the safe answer.** Getting one
  wrong is a false statement, not a bad application.
- **A browser fault is not a job failure.** Nothing was filled and nothing was
  submitted, so the job is re-queued rather than burned.
- **Never submit a résumé built from an edited base.** Tailored copies record
  their `resume_seed`; a mismatch re-tailors first.

---

## Tests

```bash
.venv/bin/python -m pytest -q
```

Offline by design. No test may reach the network or drive a browser; inject a fake
extractor or stub the subprocess instead.

Write the test so it says **why**, not just what. `tests/tools/test_browser_conflict.py`
pins that sessions run concurrently and explains that a semaphore was tried,
disproved and removed, so it cannot come back without new evidence. That is worth
more than an assertion on its own.

---

## Conventions

- Comments explain **why**, not what. Most non obvious code here exists because
  something failed in a specific way; say what that was.
- Match the surrounding style rather than introducing a new one.
- Do not commit `.local/`, `resume/base.tex` or anything under `output/`. They
  hold real personal data and are already gitignored: saved logins, the answer
  bank, tailored résumés and application screenshots all live in `.local/`.
  `config/watchlist.yaml` and `config/preferences.yaml` ARE tracked, so treat
  them as public.
- The dashboard has no build step. Plain ES modules and plain CSS, using the
  tokens already in `web/styles.css`.
