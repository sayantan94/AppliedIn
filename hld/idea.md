# AppliedIn — Autonomous Job Application Pipeline (HLD)

Date: 2026-07-15
Status: DRAFT (pending Sayantan's approval)

## Problem Statement

Good jobs show up on company career sites, but applying well is slow: read the JD, tweak the resume to match, create/log into the portal, fill a long form, and remember what was sent where. Done manually, each application takes 30-60 minutes, so good postings get skipped or get the generic resume.

AppliedIn watches the career pages of a handpicked list of companies (5-15), finds new job IDs matching Sayantan's preferences (keywords, titles, locations), tailors the base resume per JD using an LLM, applies through the company's own portal fully autonomously, and records everything: company, job ID, JD snapshot, resume version used, form answers, confirmation. It never applies to the same job ID twice; identical reposts are auto-skipped and near-duplicate reposts (same company, same normalized title) are flagged for a human decision instead of re-applied.

## Constraints

- Deploys on AWS (Sayantan's account), fully cloud — **P0: the entire pipeline, apply worker included, runs on AWS with no home-machine dependency**. The apply worker runs on ECS Fargate; datacenter-IP bot-detection exposure is mitigated by per-task IP rotation, with a per-portal residential proxy as escalation (see "Bot-detection reality" below).
- Agent framework: **Strands Agents SDK** (not Claude Agent SDK). Model provider must be swappable; the intended model is "Muse Spark" behind whatever endpoint exposes it. All LLM calls go through one provider module so the model is a config value, not an architecture decision. Interim default until Muse Spark access is confirmed: Claude Sonnet on Bedrock, swapped later by config.
- Portal accounts are **auto-created by the apply worker** when it hits an account wall: identity fields come from the global facts store in DynamoDB, a strong password is generated and saved to Secrets Manager BEFORE the signup form is submitted (a crash can never orphan an account the system doesn't know about), and email-verification links/codes are fetched via the Gmail API (read-only scope). One account per portal, enforced by checking Secrets Manager first: if creds already exist but login fails, that is NEVER treated as re-signup — it routes to `needs_human` with a screenshot (bad password? bot block?), same as any login-adjacent ambiguity. Signup flows blocked by CAPTCHA or SMS-2FA gate as `no_account`.
- Resume tailoring rewrites emphasis only: reorder bullets, mirror JD vocabulary, adjust summary/skills. It never invents experience, titles, dates, or credentials. Enforced mechanically, not by vibes: see "Truthfulness validator" below.
- Personal tool for one user. No multi-tenancy, no auth beyond Sayantan's own credentials.

## Premises

1. Discovery is feed-first, crawler-backed. Greenhouse, Lever, Ashby, and SmartRecruiters expose public JSON job feeds — for companies on those ATSes the feed IS the career site's job list (the site just renders the board), so polling the feed beats crawling the rendered page: faster, structured, zero bot surface. Workday has a queryable JSON endpoint (`/wday/cxs/...`), but it is unofficial, per-tenant, and can change without notice; treat the Workday adapter as best-effort. Companies whose career site exposes no usable feed get the **career-site crawler**: a scheduled Playwright task that loads the careers page, extracts postings via LLM-assisted extraction into the same normalized job record, and feeds the identical filter/dedup/tailor path. Crawled companies apply through the agentic fill engine unless a scripted adapter exists (premises 3-4) — discovery source never decides who clicks; the system does.
2. Matching is two-stage: a cheap deterministic keyword/title/location filter from `preferences.yaml` runs inside discovery; the LLM relevance score (0-10, default threshold 7) runs in the tailoring worker, behind the queue, so slow LLM calls never block or time out discovery. Only first-poll-forward postings are scored: discovery keeps a per-company watermark, and the initial backfill run is capped (default: 25 newest matching postings per company) so day one doesn't burn hundreds of LLM calls.
3. Form filling is system-driven on every portal — Sayantan filling a form by hand is never the designed path. Two fill engines: (a) **scripted** — deterministic Playwright per major ATS (Greenhouse/Lever/Ashby/SmartRecruiters), preferred where it exists because it is testable and stable; (b) **agentic** — a Strands agent driving Playwright for portals with no script (custom career portals, one-off ATSes, Workday tenants): it discovers the form, maps fields, and fills. Both engines share the same deterministic confidence rules and gates; a portal on the agentic engine starts `gated` and earns auto through burn-in like any other. Workday flows are per-tenant customized (multi-page, resume-parse-and-correct screens, tenant questionnaires); Workday tenants use the agentic engine and stay `gated` unless a specific tenant proves stable.
4. Fully autonomous submit is the target for EVERY portal, with a forced degrade path. CAPTCHA, failed auto-signup, unknown required field, or low-confidence field mapping stop automation and become a WhatsApp DM with a screenshot — phrased as a question Sayantan can answer in-chat; his reply feeds the answer back and the system resumes, re-fills, and submits itself. He answers a chat message; he never touches the portal. Portals without a deterministic script — including crawler-discovered custom portals — are NOT handed back to Sayantan; they go through the agentic fill engine (premise 3), gated. `mode: assist` (notify-and-assist package: tailored PDF + drafted answers + apply link) is a narrow last resort reserved for portals that actively defeat automation (hard CAPTCHA walls, anti-bot logins) even via the agentic engine + residential proxy — not a default for anything.
5. Tracking is the memory. Dedup is enforced by conditional write (`PutItem` with `attribute_not_exists(pk)`), not read-then-write, so overlapping poll cycles can't race. Reposts are caught two ways: a `jd_hash` GSI (sha256 of normalized JD text) auto-skips byte-identical reposts under a new ID (`status: skipped`, `skip_reason: duplicate_of=<original pk>`); a weaker heuristic — same company + same normalized title as an already-applied job — flags the posting as a suspected repost and gates it for a human decision rather than auto-applying. A job in any terminal or in-flight status is never re-processed.
6. Truthful tailoring only (see Constraints). Anything the resume claims must survive an interview.

## Approaches Considered

### Approach A: Notify-and-assist pipeline (minimal viable)
Discovery + matching + tailoring + tracking, but no portal automation. The system messages a package (tailored PDF, drafted answers, apply link); Sayantan clicks through the portal himself.
- Effort: M · Risk: Low
- Pros: ships fastest; zero bot-detection surface; every feed-capable ATS covered on day one.
- Cons: still ~10 min of human clicking per application. REJECTED as an end state — the entire point is that the SYSTEM clicks and applies, not Sayantan.
- Reuses: everything here carries forward into B and C unchanged, and survives only as the last-resort floor for portals that actively defeat automation.

### Approach B: Human-gated auto-apply
Everything in A, plus a Playwright apply worker that fills the entire form and pauses at submit for a one-tap WhatsApp approval.
- Effort: L · Risk: Medium
- Pros: ~95% of the time savings, near-zero burned-application risk; ideal testing posture.
- Cons: applications wait on Sayantan's availability.

### Approach C: Fully autonomous submit with guardrails (CHOSEN)
Everything in B, plus: the gate defaults to open (high-confidence fills submit unreviewed, receipt after the fact), a daily submit cap, error-rate alarms, per-portal burn-in bookkeeping, and post-hoc receipt messages. That delta is real work beyond B — it is "B plus an operations layer," not just a flag — but B's gate machinery is a strict subset of C, so nothing built for B is thrown away.
- Effort: L/XL · Risk: Medium-High (managed)
- Pros: the actual dream: zero-touch applications; guardrails + caps bound the blast radius.
- Cons: bot detection and CAPTCHA will force some fraction of applications through the human path regardless; one bad auto-submission to a liked company remains possible despite guardrails (mitigated by per-portal gated burn-in, the truthfulness validator, and the daily cap).

## Recommended Approach

**Approach C**, built in the order A → B → C so each layer is testable before the next automates it.

### Architecture

```
EventBridge (cron, default every 6h)
  ├─> Discovery Lambda (feed adapters)
  │     ├─ ATS adapters: Greenhouse / Lever / Ashby / SmartRecruiters / Workday(best-effort)
  │     ├─ per-company watermark; first run capped at 25 newest matches/company
  │     ├─ stage-1 filter: keywords/titles/locations from preferences.yaml
  │     └─ new job -> DynamoDB conditional PutItem (status: found) -> SQS `tailor-queue`
  └─> Career-site Crawler (Playwright + Chromium, Fargate task; same cron)
        ├─ for watchlist companies marked `discovery: crawl` (no usable feed)
        ├─ loads the careers page, LLM-assisted extraction -> normalized job records
        └─ same stage-1 filter, watermark, backfill cap, conditional PutItem, `tailor-queue`
SQS `tailor-queue`
  └─> Tailoring Lambda (Strands agent)
        ├─ stage-2 match: LLM scores JD vs profile (0-10); < threshold -> status: skipped
        ├─ tailor: base resume YAML + facts -> tailored YAML -> Typst -> PDF -> S3
        ├─ truthfulness validator (deterministic, see below); fail -> needs_human
        ├─ drafted answers (why us, salary, visa, notice period) + cover letter
        │    (generated only if watchlist.yaml sets requires_cover_letter — a flag
        │    Sayantan sets manually after observing the portal during burn-in)
        ├─ company with mode: assist -> notify-and-assist package -> gate to human
        └─ else status: tailored -> SQS `apply-queue`
SQS `apply-queue`
  └─> Apply Worker (Playwright + Chromium; one task per queue message)
        ├─ dispatch: SQS -> dispatcher Lambda -> ECS RunTask; ONE Fargate task per
        │    job, public subnet w/ assignPublicIp -> fresh public IP per task,
        │    torn down after (task-per-job launch IS the IP rotation)
        ├─ creds from Secrets Manager; session state persisted per portal in S3
        ├─ account wall -> AUTO-SIGNUP: identity from global facts (DDB), generated
        │    password saved to Secrets Manager BEFORE submitting signup, email
        │    verification link/code fetched via Gmail API, WhatsApp receipt
        │    "account created at X", then the apply continues; existing creds
        │    that fail login NEVER re-signup -> needs_human; signup CAPTCHA /
        │    SMS-2FA -> GATE (no_account)
        ├─ fill engine: per-ATS script where one exists, else agentic
        │    (LLM-driven Playwright); LLM maps profile facts -> unfamiliar fields
        ├─ AUTO path (all fields high-confidence, portal mode=auto, under cap):
        │    set status: submitting -> SUBMIT -> confirmation # + screenshot
        │    -> status: applied -> receipt msg
        │    (idempotency: on SQS redelivery, a job already in `submitting` is
        │     NEVER auto-retried -> needs_human, "verify on portal's My
        │     Applications page" — the submit may have landed before a crash)
        └─ GATE path (CAPTCHA / no account / unknown field / low confidence / gated):
             persist field-map JSON + form-structure snapshot + screenshots to S3,
             release the browser, status: needs_human + gate_reason
             -> WhatsApp message (reason-specific buttons)
Approval resume (on "Approve & submit" tap OR an in-chat answer reply)
  └─> any answer replies are merged into the persisted field-map JSON + answer
      bank first; then a FRESH apply task re-loads the live form and diffs its
      STRUCTURE against
      the gate-time snapshot: every reviewed field must still resolve (same
      label/type/options, same required-field set). Match -> re-fill from the
      persisted field-map JSON (no new LLM mapping) -> submit. Any new required
      field or unresolvable field -> back to needs_human with a fresh screenshot.
WhatsApp Bot (Meta WhatsApp Business Cloud API; API Gateway webhook -> Lambda)
  ├─ webhook verifies Meta's X-Hub-Signature-256 (app-secret HMAC) and accepts
  │    commands/taps only from Sayantan's WhatsApp number (wa_id) — a forged
  │    POST is a forged approval otherwise
  ├─ bot-initiated messages (receipts, gate alerts, digest) use pre-approved
  │    message TEMPLATES (WhatsApp rule outside a 24h reply window); any reply
  │    from Sayantan opens a 24h free-form session for follow-ups
  ├─ receipts, approvals (incl. gate_reason=no_account alerts), daily digest
  ├─ free-text Q&A, ANYTIME: any message that isn't a command or button reply
  │    goes to a read-only Strands agent over the tracking table + S3 artifacts
  │    (JD snapshot, resume version sent, drafted answers, confirmation,
  │    screenshots) — "what did we send to X?", "why was Y skipped?", "status
  │    of the Stripe one?"; replies can attach the stored PDF/screenshot.
  │    Strictly read-only: state changes still require commands/buttons.
  │    (inbound messages always open a 24h session, so no template needed)
  ├─ interactive reply buttons are gate-reason-specific (WhatsApp caps at 3
  │    buttons/message — every button set below fits):
  │    CAPTCHA: [I'll do it manually / Skip] — un-solvable by the bot
  │    no_account (auto-signup blocked): [Account created — retry / I'll do it
  │      manually / Skip]
  │      (retry re-enqueues after creds land in Secrets Manager; counts
  │       toward the 2-attempt limit)
  │    unknown_field / low_confidence: the DM quotes the exact form question(s)
  │      + the LLM's drafted answer + proposed save scope (global fact vs
  │      company-only); reply "ok" to approve or free text to override — the
  │      answer lands in the field map + answer bank at that scope and the job
  │      auto-resumes ("company only" restricts a global save; Sayantan
  │      answers a chat message, never fills the form)
  │    "I'll do it manually" -> job waits as needs_human; /done <id> (or the
  │      button's follow-up confirmation) marks it applied_manual
  └─ commands: /pause /resume /status /skip <id> /done <id> /fact <q> = <a>
       (kill switch; /fact is the deterministic global-fact write — the Q&A
        agent stays read-only; /skip only affects jobs not yet claimed by a
        worker — in-flight tasks finish or gate)
```

Why this decomposition and not one big cron task: the apply worker genuinely needs Chromium, long runtimes, and per-task IP rotation, so it must be separate; the two queues buy per-job retry semantics and let discovery stay a fast, dumb poller. Discovery and tailoring are NOT split further (matching lives inside tailoring) precisely to keep the moving parts down for a solo maintainer.

### Bot-detection reality (read this before trusting the zero-touch target)

Headless Chromium submitting forms from AWS datacenter IPs trips Cloudflare/reCAPTCHA/hCaptcha at far higher rates than a residential browser; some ATS front doors block datacenter ranges outright. And a CAPTCHA rendered inside a Fargate container cannot be solved by tapping "Approve" in WhatsApp. Cloud-only is P0 (no home-machine option), so mitigations are layered on AWS, in escalation order:

1. **IP rotation via task recycling (default).** The apply worker is already one Fargate task per job; launching each task in a public subnet with `assignPublicIp: ENABLED` gives every application a fresh public IP from AWS's pool, torn down afterward — no IP accumulates cross-application history. Honest limit: these are still AWS-ASN datacenter IPs. Rotation defeats per-IP rate limiting and reputation buildup, but NOT ASN/range classification — Cloudflare and friends score "this is AWS," not the specific address. Expect a higher CAPTCHA/gate rate than a residential connection; burn-in measures the real rate per portal.
2. **Per-portal residential proxy.** If burn-in shows a portal blocking or CAPTCHA-walling AWS ranges, route only that portal's tasks through a residential proxy (a per-company flag in watchlist.yaml; adds cost + a vendor, so opt-in per portal, never global).
3. **Permanent gated/assist.** Portals that block automation even through a proxy stay on the notify-and-assist path.

Zero-touch rates are measured per portal during burn-in; the rule in Success Criteria (sustain ≥80% or revert the portal to gated/assist) is the enforcement.

### Data model — DynamoDB `applications`

| Attribute | Notes |
| --- | --- |
| `pk` | `company#job_id`; dedup enforced via conditional PutItem |
| `jd_hash` | sha256 of normalized JD text; GSI; identical repost -> skipped w/ `duplicate_of` |
| `status` | found → tailored / skipped → submitting → applied / applied_manual / needs_human / capped / job_gone / error; GSI on `status` (used for capped re-enqueue and /status) |
| `gate_reason` | on needs_human: captcha, no_account, unknown_field, low_confidence, gated_mode, form_drift, suspected_repost, submit_uncertain |
| `jd_url`, `jd_s3_key` | JD snapshot at discovery time |
| `resume_s3_key`, `resume_version` | exactly which PDF was sent |
| `answers_s3_key`, `fieldmap_s3_key` | drafted answers; persisted form field map for approval resume |
| `match_score`, `skip_reason` | why it proceeded or didn't |
| `confirmation_id`, `screenshot_s3_key`, `submitted_at` | proof of submission |
| `attempts`, `last_error` | max 2 total attempts, then needs_human |

Status semantics: `submitting` = set immediately before the submit click; the idempotency marker (see AUTO path). `applied_manual` = Sayantan applied by hand (assist packages, CAPTCHA walls); counts as applied for dedup and the repost heuristic, exempt from the confirmation-ID/screenshot criterion. `capped` = daily cap reached; the Discovery Lambda's cron invocation also queries the status GSI for `capped` rows and re-enqueues them to `apply-queue`. `job_gone` = posting 404/closed when the apply worker arrived; terminal, no retries. `error` = infrastructure failure after both attempts (network, worker crash before any submit click) where no portal action is needed from a human; surfaces in the daily digest.

### Data model — DynamoDB `answer_bank`

| Attribute | Notes |
| --- | --- |
| `pk` | `global` or `company#<name>` — the reuse scope |
| `sk` | normalized question label (lowercased, punctuation-stripped; synonym list applied at lookup) |
| `answer`, `question_raw` | the approved answer, and the question exactly as the portal phrased it |
| `source`, `approved_at`, `use_count` | whatsapp_reply / gate_approval; audit trail + pruning |

Lookup order at fill time: per-company bank → global bank. First hit wins and is high-confidence (both tiers are human-approved). Miss → gate + WhatsApp DM, and the approved reply is banked so the same question never gates twice.

### Confidence — defined, not vibes

A form field is **high-confidence** only if it resolves to a canonical fact through the per-ATS field-mapping table (exact label match or a curated synonym list, e.g. "Notice period" = "When can you start?"). Canonical facts live in the DynamoDB `answer_bank` global tier — there is NO facts.yaml; DDB is the single source of truth. The global tier explicitly includes the EEO/self-identification block (gender, ethnicity, veteran, disability answers) — these dropdowns appear on virtually every Greenhouse/Lever/Workday form and would otherwise gate everything. Any field the LLM must answer free-form — essay questions, "describe a time when...", unrecognized dropdowns — is automatically low-confidence and gates the whole application. LLM self-reported confidence scores are NOT used; they are uncalibrated.

These rules are engine-independent: scripted and agentic fills use the same mapping table, synonym list, and answer bank — the agentic engine replaces only the navigation/DOM-discovery layer, never the confidence decision.

The LLM's field mappings are **draft-only for the human gate**: they pre-fill the package a human reviews, but never mark a field high-confidence for the AUTO path. A mapping that proves right repeatedly gets promoted into the curated synonym list manually (edit + redeploy), same as the auto-mode flip.

**Answer bank (DynamoDB, two scopes):** portals with required questions the mapping table doesn't cover would otherwise never auto-submit. Every answer Sayantan approves — via a gate approval or a direct WhatsApp reply — is saved under its normalized question label at one of two scopes, proposed in the DM itself:

- **Global facts** (`scope: global`): factual questions that mean the same thing everywhere — visa/H-1B sponsorship, relocation willingness, security clearance, years-of-experience-with-X. Answered ONCE, reused on any portal whose question matches by exact/synonym label, and high-confidence everywhere from then on. The gate DM states the proposed scope ("saving as a global fact"); replying "company only" restricts it.
- **Per-company answers** (`scope: company#<name>`): company-specific free text ("Why do you want to work here?"). High-confidence only for that company.

There is no facts.yaml — the global tier IS the canonical fact store (work auth/visa, notice period, salary, links, EEO, signup identity). It is seeded once at setup by a small seed script, then grows and updates exclusively through WhatsApp: gate replies, or the explicit `/fact <question> = <answer>` command. Fresh, never-seen essay questions always gate. Essay-heavy portals therefore start in gated mode and earn autonomy question-by-question. Consequence: the "no garbled application ever auto-submits" criterion is enforced by these deterministic rules plus gated burn-in, not by trusting the model.

**Terminology:** a "portal" is one company's career site. Burn-in, `mode`, and the answer bank are all per-company. A mature adapter does not exempt a new company: the second Greenhouse company on the watchlist still starts gated and earns its own 3 clean approvals (its form fields, questions, and CAPTCHA posture are its own).

### Truthfulness validator (deterministic)

After tailoring, before rendering: every employer name, job title, date range, degree, and certification in the tailored YAML must exist verbatim in `resume/base.yaml`. Any mismatch routes the job to `needs_human` with a diff. Bullets may be reworded/reordered (that's the point) but structural facts are checksummed. This runs on every application, forever — the one-time human eyeball in Next Steps is a quality check on tone, not the enforcement.

### Config files (repo-versioned)

- `watchlist.yaml` — per company: name, ATS type, board URL/token, portal login secret ref, `mode: auto|gated|assist`, `discovery: feed|crawl`, `requires_cover_letter`, optional residential-proxy flag
- `preferences.yaml` — include/exclude keywords, titles, locations, remote policy, seniority, min match score
- `resume/base.yaml` — the single source of truth resume (one-time conversion from current format)
- Facts + answer bank — DynamoDB `answer_bank`, two scopes: global canonical facts (work auth/visa, notice period, salary range, links, EEO/self-identification answers, signup identity) + per-company free text. No facts.yaml — DDB is the single source of truth, seeded once by a setup script, updated only via WhatsApp (gate replies or `/fact`)
- Configs are bundled into the deployment artifact; changing them requires `cdk deploy` (or a local redeploy script). Acceptable for one user; runtime config store is deliberate non-scope.
- IaC: AWS CDK app in TypeScript (one stack); app code stays Python. GitHub Actions deploy on push (optional, can start with `cdk deploy` from laptop)

### Guardrails (non-negotiable in C)

1. Daily auto-submit cap, global across portals, default 5, resets 00:00 UTC. Enforced by a DynamoDB atomic counter keyed by UTC date: conditional increment (`count < cap`) immediately before setting `submitting`, so concurrent apply tasks cannot overshoot. Jobs over the cap park as `capped` and re-enter on the next cron tick.
2. Per-portal `gated` burn-in — every new portal runs gated until 3 clean approved submissions. The flip to `auto` is **manual**: the bot messages "portal X has 3 clean approvals, consider flipping to auto", and Sayantan edits `watchlist.yaml` and redeploys. No self-modifying config.
3. Confidence gate — deterministic, as defined above. One low-confidence field gates the whole application.
4. Truthfulness validator on every tailored resume.
5. `/pause` kill switch in WhatsApp, plus CloudWatch alarms on error-rate spike AND on `apply-queue` ApproximateAgeOfOldestMessage — the latter catches a stalled apply pipeline (dispatcher failures, RunTask capacity errors, a broken container image) within one alarm period.
6. Max 2 total attempts per job, then needs_human (or job_gone/error as defined). A job that reached `submitting` is never auto-retried at all. Never hammer a portal.

## Open Questions

1. What endpoint exposes Muse Spark (Bedrock? direct API?), and what are its context limits? Interim default is Claude Sonnet on Bedrock behind the provider module; tailoring prompts are sized against that until Muse Spark is confirmed.
2. The actual watchlist. Which 5-15 companies, and which ATS does each use? This decides which adapters get built (build only the ones present in the list).
3. Base resume's current format (Word/PDF/LaTeX?) for the one-time conversion to `base.yaml`.
4. PARTIALLY RESOLVED: Gmail API (read-only scope) is now in scope — auto-signup requires fetching email-verification links/codes, and the same plumbing covers email codes on login. Remaining: portals using SMS 2FA stay gated/assist (no SMS automation).
5. RESOLVED (P0 decision): the apply worker runs on ECS Fargate — cloud-only, fresh public IP per task. Remaining sub-question: which residential proxy vendor, if burn-in shows a portal needs one.
6. ToS: most ATS ToS prohibit automated submission. Accepted as personal-use risk; per-portal gated/assist mode is the fallback if a portal actively blocks automation.
7. RESOLVED: direct Meta WhatsApp Business Cloud API (no middleman; free tier covers this volume). Remaining sub-question: which dedicated phone number the bot owns — it cannot be Sayantan's personal WhatsApp number.

## Success Criteria

- A new matching posting at a watchlist company is discovered within one poll cycle (default 6h).
- Each auto-mode portal sustains ≥80% zero-touch submissions after burn-in, or it is reverted to gated/assist. (Actionable rule, measured from tracking data — not an up-front promise.)
- Zero duplicate submissions: conditional writes + jd_hash for identical reposts, company+title heuristic gates suspected reposts, and the `submitting` marker prevents apply-time double-submits on crash/redelivery.
- Every applied row has: JD snapshot, resume version, answers, confirmation ID, screenshot (applied_manual exempt from the last two).
- WhatsApp receipt lands within 5 minutes of any submission or gate event.
- Any free-text WhatsApp question about a tracked job is answered from the tracking record + stored artifacts, at any time — no laptop required.
- A factual question answered once via WhatsApp (visa, relocation, clearance, ...) never gates again on ANY portal where it matches by exact/synonym label.
- No application containing a low-confidence (free-form-LLM) field ever auto-submits; the deterministic confidence gate + truthfulness validator are the enforcement, verified during burn-in.

## Deployment

Personal deployment: one AWS CDK stack in Sayantan's account, deployed via `cdk deploy` initially; GitHub Actions auto-deploy on merge once the repo is on GitHub. The apply worker and career-site crawler ship as one Playwright container image in ECR, run as on-demand Fargate tasks. No public distribution planned.

## Next Steps

1. **First, no code:** write the real watchlist. For each company, open its careers page and note the ATS from the URL (`boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, `myworkdayjobs.com`, ...). Accounts are auto-created by the apply worker, but optionally pre-create Workday accounts anyway — Workday signup is the flakiest flow and pre-creating de-risks it. Mark companies with no usable feed as `discovery: crawl`.
2. Scaffold repo (Python app + Strands, TypeScript CDK skeleton), convert base resume to `resume/base.yaml`, write `preferences.yaml`, and run the one-time facts seed script (global facts -> DynamoDB).
3. Build discovery adapters for the ATS types actually in the watchlist + DynamoDB tracking with conditional writes; add the career-site crawler for any `discovery: crawl` companies. Run locally; verify job IDs, watermarks, and dedup.
4. Build the tailoring worker: LLM match scoring, tailoring, truthfulness validator, Typst PDF render, S3 versioning. Eyeball 3-5 tailored outputs for tone (the validator handles truth).
5. WhatsApp bot (Meta Cloud API direct): set up the Meta business account + dedicated number, get message templates approved, then receipts + digest + /pause + reason-specific approval buttons + the read-only free-text Q&A agent over the tracking table.
6. Apply worker for the single most common non-Workday ATS in the watchlist, gated mode, running on Fargate with per-task IP rotation. Burn in with 3 real approved applications, exercising the field-map persist/resume path.
7. Flip that portal to auto in watchlist.yaml, add the daily cap + alarms, `cdk deploy` the whole pipeline.
8. Add remaining ATS apply scripts one at a time, each with gated burn-in. Then the agentic fill engine for unscripted/custom portals, burned in per company. Workday (agentic, per-tenant) last, and it stays gated.
