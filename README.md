# AppliedIn

Autonomous, agentic job-application pipeline. It finds jobs, tailors your résumé
to each one, and applies through the company's portal — pausing for you only at
the moments that need a human (approving a submission, or a fact it hasn't been
told). One codebase, two modes: local on your Mac, or cloud on AWS.

## How it works

The pipeline is Google ADK agents over a durable queue. Discovery finds jobs and
enqueues them; each job then runs through a sequential agent graph:

- **score** — an LLM rates fit 0–10 against your résumé + the JD; anything below
  your `min_match_score` is skipped before any tailoring is wasted on it.
- **tailor → critic** — a light-touch loop rewords the résumé's bullets toward
  the JD's vocabulary (never inventing facts, never touching your titles or
  summary), compiles it to PDF (Tectonic), and saves it — with a "what changed"
  diff.
- **apply** — human-gated: it asks you to approve, then a real browser agent
  (browser-use) fills and submits the form using only approved answers. If it
  hits an unknown field, an account wall, or a CAPTCHA, it stops and asks you;
  your answer is banked as a fact and never asked again.

Everything streams to a live dashboard — every agent step's input and output,
grouped by job, plus a screenshot of what the browser saw.

```mermaid
sequenceDiagram
    participant Cron as Daemon / EventBridge
    participant Disc as Discovery
    participant Q as Queue (Redis · SQS)
    participant Pipe as Apply pipeline (ADK)
    participant BU as browser-use
    participant Store as Store (Redis·DDB + FS·S3)
    participant You

    Cron->>Disc: discover (feeds + browser crawl)
    Disc->>Store: dedup (put_new + seen.json)
    Disc->>Q: enqueue {pk}
    Q->>Pipe: job {pk}
    Pipe->>Pipe: score (skip if below threshold)
    Pipe->>Pipe: tailor + critic → render PDF
    Pipe->>Store: save résumé + diff
    Pipe->>You: gate — "ready to apply?"
    You->>Pipe: approve
    Pipe->>BU: fill + submit (approved answers only)
    alt missing answer / account / CAPTCHA
        BU-->>You: ask; answer is banked as a fact
    end
    Pipe->>Store: status = applied
```

## Two modes (same code)

| | Local (your Mac) | Cloud (AWS) |
| --- | --- | --- |
| Store | Redis | DynamoDB |
| Queue | Redis list | SQS |
| Artifacts | filesystem | S3 |
| Orchestration LLM | Anthropic (Haiku) | Bedrock |
| Browser LLM | browser-use on Anthropic / OpenAI / OpenRouter | same |
| Triggers | `daemon` (cron + queue loop + web) | EventBridge + SQS |
| UI | localhost | Vercel |

`APPLIEDIN_MODE` picks the backends via `core/stores.py`; nothing else changes.
The browser-driving model is separate from orchestration — set
`APPLIEDIN_BROWSER_MODEL` (e.g. `gpt-4.1-mini`, `openrouter/moonshotai/kimi-k2`,
`claude-sonnet-4-6`) so orchestration stays cheap on Haiku while the browser
uses whatever drives it best.

## Run locally

```bash
# 1. cp .env.example .env, then paste your Anthropic key: ANTHROPIC_API_KEY=sk-ant-...
# 2. save YOUR résumé's LaTeX to resume/base.tex (git-ignored; the tailor edits
#    this — it never invents facts, only re-emphasizes)
# 3. list company career URLs in config/watchlist.yaml + your criteria in
#    config/preferences.yaml
./setup.sh            # one-time: venv, deps, Redis, Tectonic, Playwright/Chromium
./appliedin start     # → dashboard at http://127.0.0.1:8787
```

Daemon + CLI:

```bash
./appliedin start | stop | status | logs      # background daemon lifecycle
./appliedin discover | work | run             # one-shot passes (no server)
./appliedin resume <pk> "<answer>"            # answer a gate from the CLI
```

`setup.sh` installs everything; `appliedin start` runs the daemon detached — it
serves the dashboard, finds jobs on a schedule, tailors + applies, and waits on
you at gates (your answers become facts in `.local/facts.md`). Per-job output
(the JD + tailored résumé) lands in `output/` for inspection.

## Layout

```
src/core/       models, config, stores factory, storage interfaces + local/cloud backends
src/discovery/  feed adapters, ATS resolver, browser crawler, agentic relevance filter
src/tools/      résumé render/validate/diff, JD fetch, browser-use (apply/crawl/llm),
                per-job output, seen-list, github context, credentials, gmail
src/agent/      ADK: graph.py (score→tailor→apply), finder.py, run.py, skills/
src/daemon.py   local always-on (cron + queue + web)   src/server.py  dashboard + API
src/cli.py      start/stop/status/logs/…               src/pipeline.py  find→queue→apply
web/            live dashboard (Logs, gates, résumé PDF + diff)   infra/  AWS CDK
```
