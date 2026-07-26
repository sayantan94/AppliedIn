---
name: resume-tailoring
description: Re-emphasizes a candidate's LaTeX résumé for one specific job description — reorders and rewords bullets for keyword match and sharpens the summary — then compiles it. Use when tailoring, customizing, or targeting a résumé to a job posting. Edits LaTeX, never invents facts.
---

# Résumé tailoring

Tailor the candidate's seed résumé — the **LaTeX** in state `base_latex` — to the
job description (state `jd_text`). Output the full tailored `.tex` via
`save_tailored_resume`. The result must stay 100% truthful and still compile.

## Instructions

### Step 1: Read the JD for signal
Extract must-have skills, seniority, domain, and the exact vocabulary it uses
("agentic", "MCP", "multi-agent", "platform", "LLM evaluation").

### Step 2: Edit the LaTeX — reword, rephrase, then reorder
Tailoring is primarily **rewording and rephrasing**, not restructuring. Change
**only** these:
- `\resumeItem{...}` bullets — **rephrase each bullet in the JD's own vocabulary
  and framing**: keep the true accomplishment, but say it the way the JD says it
  (its verbs, its nouns, its emphasis). Only where it's genuinely true. Then
  reorder so the most JD-relevant come first.
- The Summary line — rephrase it to target this exact role.
- The order of skills within the Skills line.

Example — JD stresses "multi-agent orchestration":
`\resumeItem{Built workflows where agents talk to each other}` →
`\resumeItem{Built multi-agent orchestration for agent-to-agent workflows}`
(same fact, JD's words). Never claim orchestration if the seed doesn't show it.

Leave **untouched, byte-for-byte**:
- Every `\resumeSubheading{...}` / `\resumeSubheadingSingle{...}` line (employer,
  title, dates, project name, patent) — these are the immutable facts.
- The preamble, `\section` headers, and document structure.

For detailed techniques (truthful vocabulary mirroring, quantification, hard
cases), consult `references/emphasis-techniques.md`.

### Step 3: Save
Call `save_tailored_resume(tailored_latex=<the full .tex>)`. It validates the
facts survived, compiles the PDF with Tectonic, and uploads it. If it returns
`missing_facts`, you altered a `\resumeSubheading` line — restore it verbatim and
re-save.

## Hard rules
- NEVER change or drop an employer, title, employment date, degree, institution,
  certification, or patent number. Copy those lines verbatim.
- NEVER add a skill or achievement the seed doesn't contain.
- Keep the LaTeX valid — balanced braces, defined macros only. Every claim must
  survive an interview.
- NEVER write internal engineering minutiae. A bullet states what was built and
  what it achieved, never the private history of how the code got there. Banned:
  line/file counts and deltas ("cut 3.1K lines across 5 files to 636 across 2"),
  refactor and rewrite narratives, bug-hunt stories, commit or PR counts, names of
  internal modules or subprocesses, and framing that describes fixing your own
  earlier mistake. A reader outside the repo cannot verify any of it, and
  reducing code is not an accomplishment on its own — it reads as churn.
  Rewrite to the outcome: what the system now does, at what scale, for whom.
