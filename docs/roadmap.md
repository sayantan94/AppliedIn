# AppliedIn roadmap

Goal: this fork finds relevant roles (AI and agent work first, ordinary software roles kept),
prepares a tailored résumé per role, and applies through a guarded browser engine in a dedicated
browser profile, using a ChatGPT subscription for model calls, while staying mergeable with
upstream `sayantan94/AppliedIn`.

The rule that outranks the rest (CLAUDE.md): an application is sent under a real person's name
and cannot be recalled. Every phase keeps the safety guards in code, never in prompts.
Personal constraints (location, work authorization, salary, demographics) live only in the
owner's private files and never appear in this repository.

## Done — merged on `main`

| Item | Merged PR |
|---|---|
| Upstream sync (`./appliedin update`), ChatGPT subscription access and model shim | [PR #1](https://github.com/chetwerikoff/AppliedIn/pull/1) |
| Career Ops keyword/location search, domain tiers, sorting and scan cap | [PR #1](https://github.com/chetwerikoff/AppliedIn/pull/1) |
| Discovery test isolation, orchestrator-pack policies, initial CI and Node 24 | [PR #1](https://github.com/chetwerikoff/AppliedIn/pull/1) |
| BrowserSkill runtime dispatch for reads, discovery and guarded application; dedicated, pinned Chrome profile setup | [PR #3](https://github.com/chetwerikoff/AppliedIn/pull/3) |
| Web review-day tests independent of time zone; obsolete `TZ=UTC` CI pin removed | [PR #12](https://github.com/chetwerikoff/AppliedIn/pull/12) |
| Bounded scorer-format correction and strict validated score admission | [PR #14](https://github.com/chetwerikoff/AppliedIn/pull/14) |
| BrowserSkill exact grants, idempotent setup and synthetic native-form fixture isolation | [PR #16](https://github.com/chetwerikoff/AppliedIn/pull/16) |
| Unleased ADK/browser apply-entry and independent manual-outcome safeguards | [PR #17](https://github.com/chetwerikoff/AppliedIn/pull/17) |

## Phase 0 — foundation on `main` (complete)

1. **Done:** offline test isolation and CLI-free operation from Issue #2, incorporated in [PR #1](https://github.com/chetwerikoff/AppliedIn/pull/1).
2. **Done:** [PR #1](https://github.com/chetwerikoff/AppliedIn/pull/1) merged into `main` with the roadmap and pack target binding.

## Phase 1 — browser engine (complete in merged code)

Depends on Phase 0.

1. **Done:** the guarded BrowserSkill engine, dedicated browser setup and its review fixes landed in [PR #3](https://github.com/chetwerikoff/AppliedIn/pull/3), with subsequent exact-grant and offline fixture hardening in [PR #16](https://github.com/chetwerikoff/AppliedIn/pull/16).
2. **Done:** independent outcome/ADK apply-entry protection landed in [PR #17](https://github.com/chetwerikoff/AppliedIn/pull/17). Guards remain enforced by code, not prompts. Operator-only live checks are not asserted here.

## Phase 2 — pipeline reliability (items 1 and 2 complete)

1. **Done:** bounded scorer-format correction with validated `MatchScore` admission, merged in [PR #14](https://github.com/chetwerikoff/AppliedIn/pull/14).
2. **Done:** time-zone-independent web date grouping and removal of the CI `TZ=UTC` pin, merged in [PR #12](https://github.com/chetwerikoff/AppliedIn/pull/12).
3. **Pending — operator only:** restart the daemon safely and re-run a small initial batch of jobs that failed under the old scorer, followed by the rest as appropriate. The restart/re-run has not been verified as performed.
4. **Pending — [Issue #18](https://github.com/chetwerikoff/AppliedIn/issues/18):** normalize trusted relative discovery posting URLs and update this roadmap. A task branch or this documentation edit is not evidence of merge/completion.
5. **Pending — [Issue #19](https://github.com/chetwerikoff/AppliedIn/issues/19), deferred T2 reader-outage work:** bounded per-pk JD-read retry/backoff/attempt and visible manual-attention state, with conditional status-safe completion against concurrent terminal/manual outcomes. This is separate from Issue #18 and does not change application-failure semantics.

## Phase 3 — first applications (owner-driven)

Depends on Phase 1 item 1 merged and Phase 2 item 1 merged.

1. Shortlist from the latest search: AI tier first, then IT, filtered by the owner's private
   constraints. Owner and local agents only; nothing from the board is published.
2. Score and tailor the shortlist; the owner reviews each résumé.
3. Board agents approve postings and fill and submit applications in the application
   browser (owner decisions 2026-10-28 and 2026-11-04). `precheck.py` enforces duplicates,
   caps and one Submit in code. New personal facts, owner accounts and new legal facts stay
   with the owner.
4. Multi-step forms: after real attempts, add support for the form patterns that gated
   (for example Workday steps), each with a synthetic local fixture test.

## Posting safety — Issue #136 (implementation PR, verification pending)

The T3 posting guard sanitizes untrusted ATS, crawler and browser JD text in
code, retains source-evidence reasons per tracked JD generation, and requires a
currently displayed job-specific review before admitting flagged/unchecked/error
material to a browser apply. A Career Ops directory description without matched
original ATS HTML is **unchecked**, regardless of length. Synthetic-only offline
regressions are part of the implementation PR; **CI, merge, and owner/operator
live smoke are not asserted here**. Never use a live application as a smoke test.
No approval, queueing or submission belongs to a fixture.

## Deferred

- A second browser engine (Grok Bot). Not before Phase 3 shows the BrowserSkill path works.
- A GLM API key as an alternative model provider. The owner chose ChatGPT only for now.
- Periodic upstream sync as its own task after Phase 1 (one `./appliedin update` PR at a time).
