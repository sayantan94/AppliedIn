# AppliedIn Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the AppliedIn autonomous job-application pipeline as a maintainable multi-package monorepo — a TypeScript CDK stack plus focused Python service packages — following the approved HLD in `hld/idea.md`.

**Architecture:** A uv-workspace monorepo. One shared `core` package holds domain models, storage clients (DynamoDB/S3/Secrets), the swappable LLM provider, and queue helpers. Six service packages each own one runtime component (discovery, tailoring, worker, crawler-in-worker, dispatcher, whatsapp). A separate TypeScript CDK app under `infra/` provisions all AWS resources. Everything deploys to AWS per the cloud-only P0; the apply worker/crawler run as on-demand Fargate tasks with per-task public-IP rotation.

**Tech Stack:** Python 3.12 (uv workspace), Strands Agents SDK, Playwright + Chromium, Typst, boto3, pydantic v2, httpx; AWS CDK v2 (TypeScript); pytest + moto for tests.

## Global Constraints

- Runtime: Python 3.12 for all app code (managed by uv); TypeScript for CDK only.
- Cloud-only (P0): no home-machine path. Apply worker + crawler run on ECS Fargate, public subnet, `assignPublicIp: ENABLED`, **no NAT gateway** (a NAT collapses IP rotation to one static egress IP).
- LLM access is ALWAYS through `appliedin_core.llm.provider` — model is a config value (`APPLIEDIN_LLM_MODEL`), never hardcoded. Interim default: Claude Sonnet on Bedrock (`anthropic.claude-sonnet-*` inference profile).
- Agent framework is Strands Agents SDK — used in tailoring, agentic fill, and the WhatsApp Q&A agent. Never the Claude Agent SDK.
- No `facts.yaml`. DynamoDB `answer_bank` is the single source of truth for facts + answers, two scopes (`global`, `company#<name>`), seeded once by `scripts/seed_facts.py`, updated only via WhatsApp (gate replies or `/fact`).
- Dedup is enforced by conditional write (`PutItem` with `attribute_not_exists(pk)`), never read-then-write.
- Daily auto-submit cap default 5, enforced by a DynamoDB atomic conditional counter keyed by UTC date, incremented immediately before setting `submitting`.
- Max 2 total attempts per job. A job that reached `submitting` is NEVER auto-retried.
- Truthfulness validator runs on every tailored resume before render; structural facts (employer, title, dates, degree, cert) must exist verbatim in `resume/base.yaml`.
- The WhatsApp Q&A agent is strictly read-only. All state changes go through commands/buttons or the deterministic gate/approval flow.
- Config files repo-versioned: `config/watchlist.yaml`, `config/preferences.yaml`, `resume/base.yaml`. Bundled into the deploy artifact.

---

## File Structure

```
AppliedIn/
├── pyproject.toml                     # uv workspace root (members = packages/*)
├── uv.lock
├── .python-version                    # 3.12
├── ruff.toml                          # lint/format config
├── packages/
│   ├── core/                          # appliedin-core — shared, zero AWS-service-specific handlers
│   │   ├── pyproject.toml
│   │   └── src/appliedin_core/
│   │       ├── __init__.py
│   │       ├── config.py              # env-driven Settings (pydantic-settings)
│   │       ├── logging.py             # structured JSON logging
│   │       ├── models.py              # domain models + enums (Status, GateReason, JobRecord, ...)
│   │       ├── ids.py                 # pk/jd_hash/normalization helpers
│   │       ├── llm/
│   │       │   ├── __init__.py
│   │       │   └── provider.py        # get_model() -> Strands model; swappable
│   │       └── storage/
│   │           ├── __init__.py
│   │           ├── tracking.py        # applications table
│   │           ├── answer_bank.py     # two-scope answer bank
│   │           ├── artifacts.py       # S3 helper
│   │           ├── secrets.py         # Secrets Manager helper
│   │           └── queue.py           # SQS enqueue
│   ├── discovery/                     # appliedin-discovery — EventBridge Lambda
│   │   ├── pyproject.toml
│   │   └── src/appliedin_discovery/
│   │       ├── __init__.py
│   │       ├── handler.py             # Lambda entrypoint
│   │       ├── watchlist.py           # load + parse config
│   │       ├── filters.py             # stage-1 keyword/title/location filter
│   │       └── adapters/
│   │           ├── __init__.py        # ATSAdapter protocol + registry
│   │           ├── greenhouse.py
│   │           ├── lever.py
│   │           ├── ashby.py
│   │           ├── smartrecruiters.py
│   │           └── workday.py         # best-effort
│   ├── tailoring/                     # appliedin-tailoring — SQS Lambda (container)
│   │   ├── pyproject.toml
│   │   └── src/appliedin_tailoring/
│   │       ├── __init__.py
│   │       ├── handler.py
│   │       ├── scoring.py             # stage-2 LLM match score
│   │       ├── tailor.py              # Strands tailoring agent
│   │       ├── truthfulness.py        # deterministic validator
│   │       └── render.py              # YAML -> Typst -> PDF -> S3
│   ├── worker/                        # appliedin-worker — Fargate task (apply + crawler + agentic fill)
│   │   ├── pyproject.toml
│   │   ├── Dockerfile
│   │   └── src/appliedin_worker/
│   │       ├── __init__.py
│   │       ├── apply_main.py          # entrypoint: one job per task
│   │       ├── crawl_main.py          # entrypoint: career-site crawler
│   │       ├── engines/
│   │       │   ├── __init__.py        # FillEngine protocol
│   │       │   ├── scripted/          # per-ATS deterministic scripts
│   │       │   │   ├── greenhouse.py
│   │       │   │   └── lever.py
│   │       │   └── agentic.py         # Strands-driven Playwright fill
│   │       ├── signup.py              # auto-signup w/ Gmail verification
│   │       ├── gmail.py               # Gmail API read-only verification fetch
│   │       ├── confidence.py          # field-map + confidence gate
│   │       └── gates.py               # gate persistence + WhatsApp notify
│   ├── dispatcher/                    # appliedin-dispatcher — SQS -> RunTask Lambda
│   │   ├── pyproject.toml
│   │   └── src/appliedin_dispatcher/
│   │       ├── __init__.py
│   │       └── handler.py
│   └── whatsapp/                      # appliedin-whatsapp — webhook Lambda + agent
│       ├── pyproject.toml
│       └── src/appliedin_whatsapp/
│           ├── __init__.py
│           ├── webhook.py             # API GW handler: verify sig, ACK fast
│           ├── processor.py           # async: route command/button/free-text
│           ├── commands.py            # /pause /resume /status /skip /done /fact
│           ├── qa_agent.py            # read-only Strands Q&A agent
│           ├── templates.py           # outbound template messages
│           └── client.py              # Meta Cloud API client
├── infra/                             # TypeScript CDK app
│   ├── package.json
│   ├── tsconfig.json
│   ├── cdk.json
│   ├── bin/appliedin.ts
│   └── lib/
│       ├── appliedin-stack.ts         # one stack, composed from constructs
│       ├── data.ts                    # DynamoDB tables, S3, secrets
│       ├── queues.ts                  # SQS + DLQs
│       ├── discovery.ts               # EventBridge + discovery Lambda
│       ├── tailoring.ts               # tailor Lambda (container)
│       ├── compute.ts                 # ECS cluster, task defs, VPC (public subnets, no NAT)
│       ├── dispatcher.ts              # dispatcher Lambda + SQS source
│       └── whatsapp.ts                # API GW + webhook + processor Lambdas
├── config/
│   ├── watchlist.yaml
│   └── preferences.yaml
├── resume/
│   └── base.yaml
├── scripts/
│   └── seed_facts.py                  # one-time global-facts seed into DDB
└── docs/superpowers/
    ├── specs/                         # (design lives in hld/idea.md)
    └── plans/
```

---

## Phase 0 — Repo Foundation

### Task 0.1: uv workspace + tooling skeleton

**Files:**
- Create: `pyproject.toml`, `.python-version`, `ruff.toml`, `.gitignore`, `README.md`

**Interfaces:**
- Produces: a uv workspace where `uv sync` resolves all member packages; `uv run pytest` works.

- [ ] **Step 1:** Write root `pyproject.toml`:

```toml
[project]
name = "appliedin"
version = "0.1.0"
requires-python = ">=3.12"

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
appliedin-core = { workspace = true }

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "moto[dynamodb,s3,sqs,secretsmanager]>=5", "ruff>=0.6"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["packages"]
```

- [ ] **Step 2:** Write `.python-version` containing `3.12`, `ruff.toml` (line-length 100, target py312), and a Python `.gitignore` (`.venv`, `__pycache__`, `*.pyc`, `cdk.out`, `node_modules`, `.pytest_cache`).
- [ ] **Step 3:** Run `uv python install 3.12 && uv sync` — expect a resolved lockfile.
- [ ] **Step 4:** Commit: `git commit -m "chore: uv workspace skeleton"`.

### Task 0.2: core package skeleton

**Files:**
- Create: `packages/core/pyproject.toml`, `packages/core/src/appliedin_core/__init__.py`

**Interfaces:**
- Produces: importable `appliedin_core` package other members depend on via `appliedin-core = { workspace = true }`.

- [ ] **Step 1:** Write `packages/core/pyproject.toml` (name `appliedin-core`, deps: `boto3`, `pydantic>=2`, `pydantic-settings`, `httpx`, `pyyaml`, hatchling build backend, package path `src/appliedin_core`).
- [ ] **Step 2:** Empty `__init__.py` with `__version__ = "0.1.0"`.
- [ ] **Step 3:** `uv sync`; commit `chore: core package skeleton`.

---

## Phase 1 — Core Domain & Storage (TDD)

### Task 1.1: Domain models & enums

**Files:**
- Create: `packages/core/src/appliedin_core/models.py`
- Test: `packages/core/tests/test_models.py`

**Interfaces:**
- Produces:
  - `class Status(str, Enum)`: `FOUND, TAILORED, SKIPPED, SUBMITTING, APPLIED, APPLIED_MANUAL, NEEDS_HUMAN, CAPPED, JOB_GONE, ERROR`
  - `class GateReason(str, Enum)`: `CAPTCHA, NO_ACCOUNT, UNKNOWN_FIELD, LOW_CONFIDENCE, GATED_MODE, FORM_DRIFT, SUSPECTED_REPOST, SUBMIT_UNCERTAIN`
  - `class DiscoveryMode(str, Enum)`: `FEED, CRAWL`
  - `class ApplyMode(str, Enum)`: `AUTO, GATED, ASSIST`
  - `class JobRecord(BaseModel)`: `company, job_id, title, jd_url, jd_text, location, ats` and computed `pk` (`company#job_id`), `jd_hash`.
  - `class AnswerScope(str, Enum)`: `GLOBAL, COMPANY`

- [ ] **Step 1: Write failing test**

```python
from appliedin_core.models import JobRecord, Status

def test_pk_is_company_hash_job():
    r = JobRecord(company="Acme", job_id="123", title="SWE", jd_url="u",
                  jd_text="build things", location="Remote", ats="greenhouse")
    assert r.pk == "acme#123"

def test_jd_hash_is_stable_and_normalized():
    a = JobRecord(company="Acme", job_id="1", title="SWE", jd_url="u",
                  jd_text="Build  things.\n", location="R", ats="greenhouse")
    b = JobRecord(company="Acme", job_id="2", title="SWE", jd_url="u",
                  jd_text="build things.", location="R", ats="greenhouse")
    assert a.jd_hash == b.jd_hash  # normalized text -> identical repost detectable
```

- [ ] **Step 2:** Run `uv run pytest packages/core/tests/test_models.py -v` — expect FAIL (module missing).
- [ ] **Step 3:** Implement `models.py` — pydantic models, enums, and a `pk` property (lowercased `company#job_id`). `jd_hash` = sha256 of normalized text (lowercase, collapse whitespace, strip trailing punctuation) via a helper in `ids.py` (Task 1.2) — for this task inline a `_normalize` then refactor in 1.2.
- [ ] **Step 4:** Run tests — expect PASS.
- [ ] **Step 5:** Commit `feat(core): domain models and enums`.

### Task 1.2: ID & normalization helpers

**Files:**
- Create: `packages/core/src/appliedin_core/ids.py`
- Test: `packages/core/tests/test_ids.py`
- Modify: `models.py` to import from `ids`.

**Interfaces:**
- Produces: `normalize_text(s) -> str`, `jd_hash(s) -> str`, `make_pk(company, job_id) -> str`, `normalize_label(q) -> str` (for answer-bank sk: lowercase, strip punctuation, collapse whitespace).

- [ ] **Step 1:** Test `normalize_label("Notice period?") == normalize_label("notice  period")` and `jd_hash` determinism.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement helpers; refactor `models.py` to use them.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `refactor(core): centralize id/normalization helpers`.

### Task 1.3: Config settings

**Files:**
- Create: `packages/core/src/appliedin_core/config.py`
- Test: `packages/core/tests/test_config.py`

**Interfaces:**
- Produces: `class Settings(BaseSettings)` with `applications_table`, `answer_bank_table`, `artifacts_bucket`, `tailor_queue_url`, `apply_queue_url`, `llm_model`, `llm_provider`, `daily_cap` (default 5), `aws_region`; env prefix `APPLIEDIN_`. `get_settings()` cached accessor.

- [ ] **Step 1:** Test that env `APPLIEDIN_DAILY_CAP=7` yields `Settings().daily_cap == 7` and default is 5.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement with pydantic-settings.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat(core): settings`.

### Task 1.4: Tracking store (applications table)

**Files:**
- Create: `packages/core/src/appliedin_core/storage/tracking.py`
- Test: `packages/core/tests/test_tracking.py` (moto `mock_aws`)

**Interfaces:**
- Produces `class TrackingStore`:
  - `put_new(job: JobRecord) -> bool` — conditional `attribute_not_exists(pk)`; returns False on duplicate (never raises for the dup case).
  - `get(pk) -> dict | None`
  - `set_status(pk, status, **attrs)`
  - `find_by_jd_hash(jd_hash) -> str | None` — GSI query, returns original pk or None.
  - `try_increment_daily_cap(date_str, cap) -> bool` — atomic conditional increment on `count < cap`.
  - `query_status(status) -> list[dict]` — status GSI (for `capped` re-enqueue and `/status`).

- [ ] **Step 1: Write failing test**

```python
import boto3, pytest
from moto import mock_aws
from appliedin_core.models import JobRecord
from appliedin_core.storage.tracking import TrackingStore

@mock_aws
def test_put_new_dedups():
    _make_table()
    store = TrackingStore("applications")
    job = JobRecord(company="Acme", job_id="1", title="SWE", jd_url="u",
                    jd_text="x", location="R", ats="greenhouse")
    assert store.put_new(job) is True
    assert store.put_new(job) is False  # conditional write blocks the second

@mock_aws
def test_daily_cap_atomic():
    _make_table()
    store = TrackingStore("applications")
    assert all(store.try_increment_daily_cap("2026-07-16", cap=2) for _ in range(2))
    assert store.try_increment_daily_cap("2026-07-16", cap=2) is False
```

(`_make_table` creates the table with `pk` key, a `jd_hash-index` GSI, and a `status-index` GSI — include it in the test file.)

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement `TrackingStore` on boto3 resource; use `ConditionExpression` for `put_new` and the cap counter (catch `ConditionalCheckFailedException` -> return False).
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat(core): tracking store with conditional writes + cap counter`.

### Task 1.5: Answer bank (two-scope)

**Files:**
- Create: `packages/core/src/appliedin_core/storage/answer_bank.py`
- Test: `packages/core/tests/test_answer_bank.py`

**Interfaces:**
- Produces `class AnswerBank`:
  - `lookup(question: str, company: str) -> str | None` — order: `company#<company>` then `global`; label normalized via `normalize_label`.
  - `put(question: str, answer: str, scope: AnswerScope, company: str | None, source: str)` — writes pk (`global`|`company#<name>`), sk (normalized label), plus `question_raw`, `answer`, `source`, `approved_at`.
  - `seed_global(entries: dict[str,str])` — bulk seed.

- [ ] **Step 1:** Test: put a global fact "Do you require sponsorship?"->"Yes, H-1B"; `lookup` for a different company by synonym label returns it; a company-scoped answer shadows global for that company only.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat(core): two-scope answer bank`.

### Task 1.6: Artifacts (S3) + Secrets + Queue helpers

**Files:**
- Create: `storage/artifacts.py`, `storage/secrets.py`, `storage/queue.py`
- Test: `tests/test_artifacts.py`, `tests/test_secrets.py`, `tests/test_queue.py`

**Interfaces:**
- `ArtifactStore.put(prefix, key, data: bytes, content_type) -> str` (returns s3 key); `.get(key) -> bytes`; `.presign(key) -> str`. Prefixes: `jd/ resumes/ screenshots/ fieldmaps/ sessions/`.
- `SecretsClient.get_json(name) -> dict | None`; `.put_json(name, obj)`.
- `Queue.enqueue(queue_url, body: dict)`.

- [ ] Standard TDD cycle per helper with moto; one commit per file: `feat(core): S3 artifact store`, `feat(core): secrets client`, `feat(core): sqs queue helper`.

### Task 1.7: LLM provider module

**Files:**
- Create: `packages/core/src/appliedin_core/llm/provider.py`
- Test: `tests/test_provider.py`

**Interfaces:**
- Produces `get_model() -> Model` returning a Strands `BedrockModel` (default) selected by `Settings.llm_provider`; `Settings.llm_model` sets the model id. A `bedrock` branch now; a `muse_spark` branch stubbed to raise `NotImplementedError("Muse Spark endpoint pending OQ1")` so the swap point is explicit.

- [ ] **Step 1:** Test: with `APPLIEDIN_LLM_PROVIDER=bedrock`, `get_model()` returns an object whose `.model_id` equals `Settings.llm_model`; with `=muse_spark`, raises `NotImplementedError`.
- [ ] **Step 2–4:** Implement, verify. (Import `strands` lazily inside the function so core stays importable without the SDK in Lambdas that don't use it.)
- [ ] **Step 5:** Commit `feat(core): swappable LLM provider`.

---

## Phase 2 — Discovery Service (TDD)

### Task 2.1: ATS adapter protocol + Greenhouse

**Files:**
- Create: `packages/discovery/pyproject.toml`, `.../adapters/__init__.py`, `.../adapters/greenhouse.py`
- Test: `packages/discovery/tests/test_greenhouse.py`

**Interfaces:**
- `class ATSAdapter(Protocol)`: `def fetch(self, company_cfg) -> list[JobRecord]`.
- `ADAPTERS: dict[str, ATSAdapter]` registry keyed by ats type.
- Greenhouse adapter parses `https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true`.

- [ ] **Step 1:** Test with a recorded JSON fixture (httpx mocked via `respx` or a fake transport) → returns `JobRecord`s with correct `company/job_id/title/jd_text`.
- [ ] **Step 2–4:** Implement + verify.
- [ ] **Step 5:** Commit `feat(discovery): greenhouse adapter`.

### Task 2.2: Lever, Ashby, SmartRecruiters, Workday adapters

- [ ] One task each, same shape as 2.1, each with a JSON fixture and a test. Workday marked best-effort with a defensive parse (missing fields → skip row, don't crash). Commits: `feat(discovery): <ats> adapter`.

### Task 2.3: Stage-1 filter

**Files:**
- Create: `packages/discovery/src/appliedin_discovery/filters.py`
- Test: `tests/test_filters.py`

**Interfaces:**
- `stage1_match(job: JobRecord, prefs: Preferences) -> bool` — include/exclude keywords, title patterns, locations, remote policy. `Preferences` model loaded from `config/preferences.yaml`.

- [ ] TDD: excluded keyword rejects; missing include keyword rejects; location mismatch rejects. Commit `feat(discovery): stage-1 filter`.

### Task 2.4: Discovery handler (watermark, backfill cap, enqueue)

**Files:**
- Create: `handler.py`, `watchlist.py`
- Test: `tests/test_handler.py` (moto for DDB + SQS, adapters monkeypatched)

**Interfaces:**
- `handler(event, context)` — for each watchlist company: adapter.fetch → stage1 filter → per-company watermark (stored on a `watermarks` partition in the applications table or a small config item) → first-run backfill cap 25 → `TrackingStore.put_new` (skips dups) → `Queue.enqueue(tailor_queue)`. Also re-enqueues `capped` rows to apply-queue.

- [ ] **Step 1:** Test: two companies, 30 new matches each on first run → only 25 each enqueued; second run with watermark advanced enqueues only newer ids; a `put_new` returning False does not enqueue.
- [ ] **Step 2–4:** Implement + verify.
- [ ] **Step 5:** Commit `feat(discovery): handler with watermark, backfill cap, dedup`.

---

## Phase 3 — Tailoring Service (TDD)

### Task 3.1: Truthfulness validator

**Files:**
- Create: `packages/tailoring/src/appliedin_tailoring/truthfulness.py`
- Test: `tests/test_truthfulness.py`

**Interfaces:**
- `validate(base: dict, tailored: dict) -> list[str]` — returns list of violations (empty = pass). Every employer name, title, date range, degree, certification in `tailored` must exist verbatim in `base`. Bullet wording is NOT checked.

- [ ] **Step 1:** Test: reordered bullets pass; an invented employer returns a violation; a changed date range returns a violation.
- [ ] **Step 2–4:** Implement (set-membership over structural fields) + verify.
- [ ] **Step 5:** Commit `feat(tailoring): deterministic truthfulness validator`.

### Task 3.2: Match scoring (Strands)

**Files:**
- Create: `scoring.py`; Test: `tests/test_scoring.py` (provider stubbed)

**Interfaces:**
- `score_match(jd_text, profile) -> int` (0–10) via a Strands agent using `get_model()`; prompt returns a bare integer; parse defensively (clamp 0–10). Threshold from prefs.

- [ ] TDD with the model call monkeypatched to return "8" → `score_match(...) == 8`; "garbage" → gates/raises handled by caller. Commit `feat(tailoring): LLM match scoring`.

### Task 3.3: Tailoring agent + Typst render

**Files:**
- Create: `tailor.py`, `render.py`; Test: `tests/test_render.py`

**Interfaces:**
- `tailor(base: dict, jd_text: str) -> dict` — Strands agent, emphasis-only rewrite.
- `render_pdf(tailored: dict) -> bytes` — YAML → Typst template → invoke `typst compile` → PDF bytes. Test asserts a non-empty PDF (`%PDF` header) from a fixed input using a bundled `typst` binary (skip test if binary absent, but CI installs it).

- [ ] TDD; commit `feat(tailoring): tailoring agent + Typst render`.

### Task 3.4: Tailoring handler

**Files:**
- Create: `handler.py`; Test: `tests/test_tailoring_handler.py`

**Interfaces:**
- SQS handler: load job → `score_match` (< threshold → status `skipped`) → `tailor` → `validate` (violations → `needs_human`, gate_reason none, store diff) → `render_pdf` → S3 → drafted answers → assist-mode branch (notify-and-assist, gate to human) → else status `tailored` + enqueue apply-queue.

- [ ] TDD the routing branches with stubbed sub-functions. Commit `feat(tailoring): handler routing`.

---

## Phase 4 — Apply Worker & Crawler (TDD where deterministic)

### Task 4.1: Confidence gate + field map

**Files:**
- Create: `packages/worker/src/appliedin_worker/confidence.py`; Test: `tests/test_confidence.py`

**Interfaces:**
- `resolve_field(label, ats, answer_bank, company) -> FieldResolution` — exact/synonym mapping table per ATS; hits answer bank; returns `(value, high_confidence: bool)`. Free-form/unrecognized → `high_confidence=False`.
- `all_high_confidence(fields) -> bool`.

- [ ] TDD: known EEO label resolves high-confidence from a seeded bank; unknown essay label → low-confidence. Commit `feat(worker): confidence gate`.

### Task 4.2: Gate persistence + WhatsApp notify

**Files:**
- Create: `gates.py`; Test: `tests/test_gates.py`

**Interfaces:**
- `raise_gate(pk, reason: GateReason, fieldmap, snapshot, screenshots)` — persist fieldmap JSON + form-structure snapshot + screenshots to S3, set status `needs_human` + gate_reason, publish a WhatsApp gate message (client injected/mocked).

- [ ] TDD with mocked S3 + WhatsApp client. Commit `feat(worker): gate persistence + notify`.

### Task 4.3: Auto-signup + Gmail verification

**Files:**
- Create: `signup.py`, `gmail.py`; Test: `tests/test_signup.py`

**Interfaces:**
- `ensure_account(portal, secrets) -> Creds` — if secret exists, return it; else generate password, `secrets.put_json` BEFORE submitting signup, drive Playwright signup (page object injected/faked in tests), fetch verification via `gmail.fetch_code(...)`, return creds. Existing-but-login-fails is handled by the caller as a gate, never re-signup.
- `gmail.fetch_code(query) -> str | None` — Gmail API readonly; token from secrets.

- [ ] TDD the credential-before-submit ordering and the "existing secret → no signup" path with a fake page + fake gmail. Commit `feat(worker): crash-safe auto-signup + gmail verification`.

### Task 4.4: Scripted fill engines (Greenhouse, Lever)

**Files:**
- Create: `engines/__init__.py`, `engines/scripted/greenhouse.py`, `engines/scripted/lever.py`; Test: `tests/test_scripted_greenhouse.py`

**Interfaces:**
- `class FillEngine(Protocol)`: `async def fill(self, page, job, resolver) -> FillResult`.
- Greenhouse/Lever scripts locate known fields, fill from `resolve_field`, collect a `FillResult(fields, low_confidence_labels, form_snapshot)`.

- [ ] TDD against a saved static HTML form fixture served to a real Playwright `page` (or a lightweight DOM double). Commit `feat(worker): scripted greenhouse/lever fill`.

### Task 4.5: Agentic fill engine

**Files:**
- Create: `engines/agentic.py`; Test: `tests/test_agentic.py` (model + page faked)

**Interfaces:**
- `class AgenticFillEngine(FillEngine)` — Strands agent with Playwright tools discovers fields, maps via `resolve_field` (LLM only proposes labels; confidence decision stays deterministic), returns same `FillResult`. Shares the confidence rules from Task 4.1.

- [ ] TDD the contract that agentic output still routes through `resolve_field` (assert a free-form field yields low-confidence). Commit `feat(worker): agentic fill engine`.

### Task 4.6: Apply main (AUTO/GATE paths, idempotency)

**Files:**
- Create: `apply_main.py`; Test: `tests/test_apply_main.py`

**Interfaces:**
- `run_apply(pk)` — load job; ensure account; pick engine (scripted if present else agentic); fill; if not all high-confidence or portal gated/over-cap → gate; AUTO path: `try_increment_daily_cap` → set `submitting` → submit → confirmation + screenshot → `applied`. Idempotency: a job already `submitting` on redelivery → `needs_human` (`submit_uncertain`), never re-submit.

- [ ] TDD the branch matrix with fakes. Commit `feat(worker): apply orchestration with idempotency`.

### Task 4.7: Career-site crawler main

**Files:**
- Create: `crawl_main.py`; Test: `tests/test_crawl_main.py`

**Interfaces:**
- `run_crawl(company_cfg)` — Playwright load careers page → LLM-assisted extraction → `JobRecord`s → same stage-1 filter/watermark/`put_new`/enqueue path as discovery (reuse discovery filter + core stores).

- [ ] TDD extraction→enqueue with a static careers-page fixture + stubbed model. Commit `feat(worker): career-site crawler`.

### Task 4.8: Worker Dockerfile

**Files:**
- Create: `packages/worker/Dockerfile`

- [ ] Base `mcr.microsoft.com/playwright/python:v1.47.0`; `uv` install; copy core+worker; two entrypoints selected by `APPLIEDIN_TASK_MODE=apply|crawl`. Build locally: `docker build`. Commit `build(worker): playwright container image`.

---

## Phase 5 — Dispatcher (TDD)

### Task 5.1: Dispatcher handler

**Files:**
- Create: `packages/dispatcher/src/appliedin_dispatcher/handler.py`; Test: `tests/test_dispatcher.py`

**Interfaces:**
- `handler(event, context)` — per SQS record: check a max-concurrent lid (count RUNNING tasks via `ecs.list_tasks`), `ecs.run_task` with the apply task def, `assignPublicIp=ENABLED`, public subnets, override env `APPLIEDIN_JOB_PK`. Over the lid → leave message for redelivery (raise to retry).

- [ ] TDD with mocked ECS client asserting `run_task` called with public-IP networking + correct override. Commit `feat(dispatcher): SQS to Fargate RunTask`.

---

## Phase 6 — WhatsApp Bot (TDD)

### Task 6.1: Meta client + templates

**Files:**
- Create: `client.py`, `templates.py`; Test: `tests/test_client.py`

**Interfaces:**
- `MetaClient.send_text(wa_id, text)`, `.send_buttons(wa_id, text, buttons)`, `.send_template(wa_id, name, params)`, `.send_document(wa_id, s3_presigned, caption)`.
- `templates.receipt(...)`, `templates.gate(reason, ...)` return payloads (≤3 buttons).

- [ ] TDD payload shape (assert ≤3 buttons; template vs free-text selection). Commit `feat(whatsapp): meta client + templates`.

### Task 6.2: Webhook (signature verify + fast ACK)

**Files:**
- Create: `webhook.py`; Test: `tests/test_webhook.py`

**Interfaces:**
- `handler(event, context)` — GET verify challenge; POST verify `X-Hub-Signature-256` HMAC (app secret from Secrets), reject non-Sayantan `wa_id`, enqueue raw update to an internal SQS/async invoke, return 200 fast.

- [ ] TDD: bad signature → 403; foreign wa_id → ignored; good → enqueued + 200. Commit `feat(whatsapp): signed webhook with fast ack`.

### Task 6.3: Command router

**Files:**
- Create: `commands.py`, `processor.py`; Test: `tests/test_commands.py`

**Interfaces:**
- `route(update) ->` dispatches: `/pause /resume` (toggle a kill-switch flag item), `/status`, `/skip <id>`, `/done <id>` (→ `applied_manual`), `/fact <q> = <a>` (→ `AnswerBank.put(scope=GLOBAL)`), button taps (approve/retry/skip/company-only), free text → Q&A agent OR pending-gate answer merge.

- [ ] TDD each command with mocked stores. Commit `feat(whatsapp): command + button router`.

### Task 6.4: Read-only Q&A agent

**Files:**
- Create: `qa_agent.py`; Test: `tests/test_qa_agent.py`

**Interfaces:**
- `answer(question) -> str` — Strands agent with READ-ONLY tools over TrackingStore + ArtifactStore (get/query only; no mutation tools bound). Can return a presigned artifact link.

- [ ] TDD: tool set contains no writer; a "why was X skipped" question calls the read tool. Commit `feat(whatsapp): read-only Q&A agent`.

### Task 6.5: Gate-answer resume

**Files:**
- Modify: `processor.py`; Test: `tests/test_gate_resume.py`

**Interfaces:**
- On a free-text reply to an open gate: merge answer into fieldmap JSON + `AnswerBank.put` at proposed scope, then re-enqueue a fresh apply task (structure-diff + refill happens in worker Task 4.6 resume path).

- [ ] TDD merge + re-enqueue with mocks. Commit `feat(whatsapp): conversational gate-answer resume`.

---

## Phase 7 — CDK Infrastructure (TypeScript)

### Task 7.1: CDK app skeleton + data constructs

**Files:**
- Create: `infra/package.json`, `tsconfig.json`, `cdk.json`, `bin/appliedin.ts`, `lib/appliedin-stack.ts`, `lib/data.ts`

**Interfaces:**
- `data.ts` exports a `DataResources` construct: `applications` table (pk, `jd_hash-index`, `status-index` GSIs, on-demand), `answer_bank` table (pk+sk), one S3 bucket (SSE, block public), and secret placeholders (portal creds, whatsapp, gmail).

- [ ] **Step 1:** `npm init` in `infra/`, add `aws-cdk-lib`, `constructs`, `aws-cdk`, `typescript`, `ts-node`.
- [ ] **Step 2:** Write `data.ts` + wire into the stack.
- [ ] **Step 3:** `npx cdk synth` — expect a CloudFormation template with both tables + bucket.
- [ ] **Step 4:** Commit `feat(infra): cdk skeleton + data resources`.

### Task 7.2: Queues + DLQs

- [ ] `lib/queues.ts`: `tailor-queue`, `apply-queue`, each with a DLQ (maxReceiveCount 2 — matches the 2-attempt rule), plus the internal whatsapp-processing queue. `cdk synth` asserts presence. Commit `feat(infra): sqs queues + dlqs`.

### Task 7.3: Discovery (EventBridge + Lambda)

- [ ] `lib/discovery.ts`: Python Lambda (bundled via `PythonFunction` or Docker bundling of the `discovery` package + core), 6h EventBridge rule, env wiring, DDB/SQS grants. `cdk synth`. Commit `feat(infra): discovery lambda + schedule`.

### Task 7.4: Tailoring Lambda (container image)

- [ ] `lib/tailoring.ts`: `DockerImageFunction` from a tailoring Dockerfile (Typst bundled), SQS event source from tailor-queue, grants. Commit `feat(infra): tailoring container lambda`.

### Task 7.5: Compute (VPC, ECS, task defs) — the IP-rotation core

- [ ] `lib/compute.ts`: a VPC with **public subnets only, no NAT gateway** (`natGateways: 0`), an ECS cluster, a Fargate task definition from the worker ECR image with two container command modes, task role grants (DDB/S3/Secrets/SQS). Assert in `cdk synth` that no `AWS::EC2::NatGateway` exists. Commit `feat(infra): ecs fargate compute, public-subnet no-NAT for IP rotation`.

### Task 7.6: Dispatcher + WhatsApp API

- [ ] `lib/dispatcher.ts`: dispatcher Lambda with apply-queue event source + `ecs:RunTask`/`iam:PassRole` grants scoped to the task def. `lib/whatsapp.ts`: HTTP API Gateway → webhook Lambda → processing queue → processor Lambda; grants. Commit `feat(infra): dispatcher + whatsapp api`.

### Task 7.7: Alarms + kill switch wiring

- [ ] CloudWatch alarms: apply error-rate, `apply-queue` `ApproximateAgeOfOldestMessage`. Commit `feat(infra): operational alarms`.

---

## Phase 8 — Config, Seed, Docs

### Task 8.1: Config templates + seed script

**Files:**
- Create: `config/watchlist.yaml`, `config/preferences.yaml`, `resume/base.yaml` (example), `scripts/seed_facts.py`

- [ ] `seed_facts.py` reads a local `facts.seed.yaml` (git-ignored, user-provided) and calls `AnswerBank.seed_global`. Provide a committed `facts.seed.example.yaml`. Commit `chore: config templates + facts seed script`.

### Task 8.2: README + run docs

- [ ] Root `README.md`: architecture summary, `uv sync`, running tests, `cdk deploy`, seeding facts, WhatsApp setup checklist. Commit `docs: project readme`.

---

## Self-Review Notes

- **Spec coverage:** discovery (feed+crawl), tailoring (+truthfulness), apply worker (scripted+agentic+auto-signup+idempotency), dispatcher (IP rotation), WhatsApp (webhook/commands/Q&A/gate-resume), two-scope answer bank, daily cap, alarms, cloud-only no-NAT VPC — all mapped to tasks. Burn-in/mode flips are operational (watchlist edits), not code.
- **No facts.yaml anywhere** — global facts are DDB-only, seeded by `scripts/seed_facts.py`.
- **Type consistency:** `JobRecord`, `Status`, `GateReason`, `FillResult`, `resolve_field`, `TrackingStore`, `AnswerBank` names are used identically across producing/consuming tasks.
