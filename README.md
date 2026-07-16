# AppliedIn

Autonomous job-application pipeline. Watches a handpicked list of companies,
finds new matching postings, tailors the resume per JD, and applies through
each company's own portal — fully on AWS, with a WhatsApp control surface.

Design of record: [`hld/idea.md`](hld/idea.md).
Implementation plan: [`docs/superpowers/plans/2026-07-16-appliedin-foundation.md`](docs/superpowers/plans/2026-07-16-appliedin-foundation.md).

## Layout

Monorepo: a uv workspace of Python service packages plus a TypeScript CDK app.

```
packages/
  core/         appliedin-core — shared models, storage clients, LLM provider
  discovery/    ATS feed adapters + EventBridge poller Lambda
  tailoring/    match scoring, tailoring agent, truthfulness validator, Typst render
  worker/       apply worker + career-site crawler + scripted/agentic fill (Fargate)
  dispatcher/   SQS -> ECS RunTask (one Fargate task per job = IP rotation)
  whatsapp/     Meta Cloud API webhook, command router, read-only Q&A agent
infra/          AWS CDK app (TypeScript), one stack
config/         watchlist.yaml, preferences.yaml (repo-versioned)
scripts/        seed_facts.py (one-time global-facts seed)
```

Everything is deployed to AWS. The apply worker/crawler run as on-demand
Fargate tasks in a **public-subnet, no-NAT VPC** so each task gets a fresh
public IP — the datacenter-IP rotation described in the HLD.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Node 20+.

```bash
uv python install 3.12
uv sync --all-packages          # resolve + install every workspace package
uv run pytest packages -q       # run the whole test suite
uv run ruff check packages      # lint
```

Per-package tests: `uv run pytest packages/discovery -q`.

## Configuration

- `config/watchlist.yaml` — the companies to watch (name, ATS, board token,
  `mode`, `discovery: feed|crawl`). Replace the examples with the real list.
- `config/preferences.yaml` — stage-1 keyword/title/location filter.
- **Facts** (visa, notice period, salary, EEO, signup identity) live only in
  DynamoDB — there is no `facts.yaml`. Seed once:
  ```bash
  cp facts.seed.example.yaml facts.seed.yaml   # fill in real values
  APPLIEDIN_ANSWER_BANK_TABLE=<table> uv run python scripts/seed_facts.py facts.seed.yaml
  ```
  After seeding, facts update only via WhatsApp (gate replies or `/fact`).

## Deploy

The CDK app provisions the whole pipeline as one stack. Deploying builds the
worker/tailoring container images, so **Docker must be running**.

```bash
cd infra
npm install
npx cdk synth        # requires Docker (bundles Lambda + container assets)
npx cdk deploy
```

Type-check the infra without Docker: `cd infra && npx tsc --noEmit`.

### WhatsApp setup (Meta Cloud API)

1. Create a Meta business account + WhatsApp app, add a dedicated phone number
   (not your personal WhatsApp number).
2. Put the token, `phone_number_id`, `app_secret`, `verify_token`, and your
   own `wa_id` into the `appliedin/whatsapp` secret.
3. Set the Meta webhook callback URL to the stack's `WhatsAppWebhookUrl` output.
4. Get the outbound message templates (receipt / gate / digest) approved.

## Guardrails (non-negotiable)

Daily submit cap, per-portal gated burn-in, deterministic confidence gate,
truthfulness validator on every resume, `/pause` kill switch, max 2 attempts
per job, and a job that reached `submitting` is never auto-retried. See the
HLD's Guardrails section.
