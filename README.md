# AppliedIn

Applying to jobs is mostly retyping. The same name, the same phone number, the
same "why are you interested in this role" — a hundred times, until you stop
applying to good roles because the form is tedious.

AppliedIn does the retyping. It finds roles worth your time, tailors your résumé
to each one, fills the employer's form from answers you approved, and stops for
you only where a person is genuinely required.

**It runs entirely on your own machine, and applies in your own browser.** Your
résumé, your answers and your history stay on your disk. Nothing is uploaded
anywhere except to the employer you are applying to.

> Published for learning — applications go out under **your** name, so read them
> before they are sent. See [the caveats](#️-for-learning-purposes-only).

![AppliedIn — the pipeline board: jobs flow found → tailored → applied, with a live feed of every agent step](docs/screenshots/01-pipeline.png)

*Jobs move **found → tailored → applied**. Every step streams to the live feed,
so you can always see what it did and why.*

## What it actually does

- **Finds roles** on your watchlist's job boards, screened against what you
  said you want — titles, seniority, locations, hard rules like "no security
  clearance". Ranked, so Seattle beats San Francisco if that's your preference.
- **Tailors your résumé** per job: rewords bullets toward the posting's own
  vocabulary, never inventing anything and never touching your job titles.
  Compiles to PDF, and shows you a diff of exactly what changed.
- **Fills the form** — name, contact, work authorisation, the awkward dropdowns,
  and drafts the free-text answers from your résumé and GitHub.
- **Stops when it should.** An unknown field, a login wall or a security check
  becomes a question for you, and your answer is remembered so it is never asked
  twice.

## Getting started

```bash
cp .env.example .env          # paste your key: OPENAI_API_KEY=sk-...
./setup.sh                    # venv, deps, Redis, Tectonic, Playwright
./appliedin start             # → http://127.0.0.1:8787
```

Two things to set up first:

1. **Your résumé** — put its LaTeX in `resume/base.tex`. This file is
   git-ignored, and the tailor only ever re-words it.
2. **What you want** — employers in `config/watchlist.yaml`, and your criteria in
   the dashboard's **Job preferences** panel (titles, keywords, locations, the
   score bar, and free-text rules).

Then press **Discover**, and **Process** when jobs appear.

```bash
./appliedin start | stop | status | logs   # daemon lifecycle
./appliedin discover | work | run          # one-shot passes, no server
./appliedin resume <pk> "<answer>"         # answer a gate from the CLI
```

## How much it applies is up to you

| Mode | What happens |
| --- | --- |
| **Gated** *(default)* | Tailors everything, applies to nothing until you press ▶ Apply. |
| **Auto** | Applies by itself to jobs scoring above your bar, up to a daily cap. |
| **Assisted** | Stops at tailored; the [browser extension](extension/) finishes each one in your own Chrome. |

**Assisted** is worth understanding. Employers increasingly challenge automated
browsers — one posting will go through untouched while the next demands a
security code. In your own browser, with your own session, that challenge usually
never appears; when it does, you are already there to clear it. The extension
opens each waiting application, fills it, and leaves you the check and Submit.

## Under the hood

Google ADK agents over a durable queue.

```mermaid
sequenceDiagram
    participant Cron as Daemon
    participant Disc as Discovery
    participant Q as Queue
    participant Pipe as Pipeline (ADK)
    participant Web as Browser
    participant You

    Cron->>Disc: discover (board feeds + crawl)
    Disc->>Q: enqueue new postings (deduped)
    Q->>Pipe: job
    Pipe->>Pipe: score — skip below your bar
    Pipe->>Pipe: tailor + critic → render PDF
    Pipe->>You: "ready to apply?"
    You->>Pipe: approve
    Pipe->>Web: fill + submit (approved answers only)
    alt security check, login wall, unknown field
        Web-->>You: hand off — you finish it
    end
    Pipe->>You: confirmation captured
```

### Models

One key, one model, every stage — all defaulting to **`openai/gpt-5-mini`**.
Each is a full LiteLLM string, so any stage can point at another provider without
touching code.

| Stage | What it does | Env var |
| --- | --- | --- |
| Relevance | first-pass title screen | `APPLIEDIN_RELEVANCE_MODEL` |
| Scorer | rate fit 0–10 against your résumé | `APPLIEDIN_SCORER_MODEL` |
| Tailor | reword the résumé for the posting | `APPLIEDIN_TAILOR_MODEL` |
| Critic | review the tailored résumé | `APPLIEDIN_CRITIC_MODEL` |
| Writer | draft free-text answers | `APPLIEDIN_WRITER_MODEL` |
| Field mapper | match form fields to your answers | `APPLIEDIN_ORCHESTRATOR_MODEL` |
| Browser | fills the form in your own Chrome | `APPLIEDIN_CHROME_MODEL` |

Unset stages fall back to `APPLIEDIN_ORCHESTRATOR_MODEL`, so one variable moves
everything. A stronger model for the writer (essays) is the usual first upgrade.

### It applies in your own browser

A driven browser has never been anywhere. No history, no cookies the site has
seen before, and a fingerprint that reads as automation, so the pages that matter
push back: a security code on one posting, "your submission was flagged as
possible spam" on the next. A form filled perfectly still does not go out. That is
a browser problem, and no amount of work on the form logic fixes it.

So the application runs in the browser you already use, through
[Claude Code's Chrome integration](https://code.claude.com/docs/en/chrome), with
the sessions you are already signed into. The session is limited to browser tools
plus a scratch file for its report: filling a form cannot read your repo or run a
shell.

**This needs Claude Code and a direct Anthropic plan** (Pro, Max, Team or
Enterprise). The Chrome integration does not accept API keys, so an API key alone
will not enable it.

Everything else stays on OpenAI. Discovery, scoring, tailoring, the critic and the
writer all run on your `OPENAI_API_KEY`; the browser is the only part that
changed hands.

| Stage | Runs on |
| --- | --- |
| Discovery, scoring, tailoring, critic, writer | OpenAI (`APPLIEDIN_ORCHESTRATOR_MODEL`) |
| Filling and submitting the application | Claude, in your Chrome (`APPLIEDIN_CHROME_MODEL`) |

### What it refuses to do

These are checked in code, not asked for in a prompt, because a rule that is only
a sentence in a prompt is a rule nobody is enforcing:

- **It never declares a protected characteristic.** Disability, veteran status,
  race and gender questions are voluntary; unless you have answered one yourself,
  it is left blank. Your own negative answers still go through.
- **It never applies twice.** A job already marked applied is refused outright,
  before the browser opens.
- **It never records "applied" without a confirmation the page actually showed.**
  Clicking Submit and hoping is not a submission.
- **It never solves a CAPTCHA**, and never accepts an arbitration agreement or
  waiver you have not explicitly answered.

### Site quirks

Some employers need rules the generic path can't infer — a form inside an iframe,
a combobox that ignores typed text, a confirmation with unusual wording. Each is
one markdown file in [`src/agent/skills/site-quirks/`](src/agent/skills/site-quirks/),
injected into the apply for that site only. Adding a site is one file, no code.

## Checking it without spending anything

These drive the real code paths with Playwright and **no model calls**, so they
cost nothing to run as often as you like:

```bash
.venv/bin/python scripts/verify_form_targeting.py   # finds the real form, ignores cookie banners
.venv/bin/python scripts/verify_submit_path.py      # fill → submit → confirmation, local fixture
.venv/bin/python scripts/fill_form_demo.py <url>    # fills a real posting, screenshots, never submits
.venv/bin/python scripts/survey_company_forms.py    # what each employer's form will demand
```

## Layout

```
src/core/       models, config, stores factory, storage backends
src/discovery/  board feed adapters, ATS resolver, crawler, relevance filter
src/tools/      résumé render/diff, JD fetch, the browser apply engine
src/agent/      ADK graph (score→tailor→apply), skills/, site-quirks/
src/daemon.py   the always-on process (discovery + workers + web)
src/server.py   dashboard, API, extension endpoints
web/            the dashboard          extension/  assisted-apply driver
scripts/        no-cost verification and survey tools
```

## Requirements

Python 3.12, Redis, Chrome, and an OpenAI API key. Applying also needs
[Claude Code](https://code.claude.com) and a direct Anthropic plan, since the
Chrome integration does not accept API keys. `setup.sh` handles the rest.

## ⚠️ For learning purposes only

This is a personal project published to learn from and build on — not a product,
a service, or advice.

- **Applications go out under your name.** Read them before they are sent. An
  application you didn't check is still one you signed.
- **Employers' terms may prohibit automated applications.** Check them. Where you
  point this, and whether you should, is your call and your responsibility.
- **It never solves CAPTCHAs or bot checks.** When one appears the run stops and
  hands you the page. That boundary is deliberate — please leave it in.
- **No warranty.** It gets things wrong. The failure modes are documented rather
  than hidden, which is the most useful thing about reading the code.

---

## Licence

MIT — see [LICENSE](LICENSE).
