"""AppliedIn — the agentic pipeline

Sequential pipeline, each stage the pattern that fits it:

  root = SequentialAgent
    1. scorer          SINGLE-AGENT + structured output (MatchScore, no parsing).
    2. tailor_critique REVIEW-&-CRITIQUE (LoopAgent): the tailor operates on the
                       seed résumé via the `resume-tailoring` SKILL and saves a
                       TYPED résumé (save_tailored_resume — validated, no regex);
                       the critic refines until it calls exit_loop.
    3. applier         LlmAgent that delegates to the browser apply (apply_to_job) —
                       a real browser agent (on Anthropic) fills & submits from
                       human-approved answers only; HUMAN-IN-THE-LOOP via
                       ask_human (ADK long-running tool) whenever it's blocked.

Model follows the mode (Anthropic local / Bedrock cloud) via core.config.
"""

from __future__ import annotations

import os
from pathlib import Path

from google.adk.agents import LlmAgent, LoopAgent, SequentialAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.skills import load_skill_from_dir
from google.adk.tools import LongRunningFunctionTool, ToolContext
from google.adk.tools.skill_toolset import SkillToolset

from core.config import get_settings
from core.logging import get_logger
from tools.schema import MatchScore

log = get_logger(__name__)
_SKILLS = Path(__file__).parent / "skills"


def _model(agent: str = "") -> LiteLlm:
    # APPLIEDIN_ADK_MODEL is a blanket override for every agent; otherwise each
    # agent uses its own model (agent_model) so you can mix & match.
    model = os.environ.get("APPLIEDIN_ADK_MODEL") or get_settings().agent_model(agent)
    return LiteLlm(model=model)


def save_tailored_resume(tailored_latex: str, tool_context: ToolContext) -> dict:
    """Save the tailored résumé LaTeX: validate it against the seed (truthfulness),
    compile to PDF (Tectonic), and upload.

    `tailored_latex` is the full edited .tex — no JSON, no parsing. On a
    truthfulness violation the tool returns the missing facts so the agent can
    restore them (emphasis only) and re-save."""
    from core.stores import make_stores
    from tools.render import render_pdf
    from tools.truthfulness import validate

    base_latex = tool_context.state.get("base_latex") or ""
    # Keep every bullet: dropping \resumeItem lines is the #1 over-tailoring failure.
    base_items, new_items = base_latex.count("\\resumeItem"), tailored_latex.count("\\resumeItem")
    if new_items < base_items:
        return {"ok": False,
                "message": f"You dropped {base_items - new_items} bullet(s) "
                           f"({new_items} vs {base_items}). Keep EVERY \\resumeItem — "
                           "restore the missing ones (reword, never delete) and re-save."}
    violations = validate(base_latex, tailored_latex)
    if violations:
        return {"ok": False, "missing_facts": violations,
                "message": "These facts were dropped/altered — restore them verbatim "
                           "(emphasis only) and re-save."}

    pk = tool_context.state.get("pk", "resume")
    stores = make_stores()

    # The application goes out under a chosen profile, so the PDF must carry that
    # profile's contact details — a form saying one address while the attached
    # résumé says another is the kind of mismatch a recruiter notices.
    from core import profiles as _profiles
    _prof = _profiles.resolve((stores.tracking.get(pk) or {}).get("profile_id", ""))
    tailored_latex = _profiles.apply_to_latex(tailored_latex, _prof)
    artifacts = stores.artifacts  # filesystem (local) or S3 (cloud) — same call
    tex_key = artifacts.put("resumes", f"{pk}.tex", tailored_latex.encode(), "text/x-tex")
    try:
        key = artifacts.put("resumes", f"{pk}.pdf", render_pdf(tailored_latex), "application/pdf")
        fmt = "pdf"
    except RuntimeError as exc:
        # Most compile failures are bare specials ($500K, 30%, AT&T) — repair
        # deterministically and retry ONCE before considering any downgrade.
        from tools.render import sanitize_latex
        pdf_bytes = None
        fixed = sanitize_latex(tailored_latex)
        if fixed != tailored_latex:
            try:
                pdf_bytes = render_pdf(fixed)
            except RuntimeError:
                pdf_bytes = None
        if pdf_bytes is not None:
            tailored_latex = fixed
            tex_key = artifacts.put("resumes", f"{pk}.tex", fixed.encode(), "text/x-tex")
            key = artifacts.put("resumes", f"{pk}.pdf", pdf_bytes, "application/pdf")
            fmt = "pdf"
            log.info("PDF render repaired by escaping bare specials for %s", pk)
        else:
            # NEVER downgrade a good PDF to a .tex pointer: the apply step needs a PDF
            # to upload. If a previous save compiled, keep it and make the agent fix
            # its broken edit; only fall back to .tex when no PDF ever existed.
            prev = (stores.tracking.get(pk) or {}).get("resume_s3_key", "")
            if prev.endswith(".pdf"):
                log.warning("PDF render failed (%s); keeping previous good PDF", exc)
                return {"ok": False,
                        "message": "Your edited LaTeX does NOT compile (syntax error). The "
                                   "previous valid résumé is kept. Fix the error and re-save "
                                   "the FULL corrected .tex."}
            log.warning("PDF render failed (%s); storing .tex only", exc)
            key = tex_key
            fmt = "tex"

    tool_context.state["resume_s3_key"] = key
    tool_context.state["tailored"] = tailored_latex
    # Record on the TRACKING ROW too — that's what the dashboard reads, so this is
    # how the résumé PDF + the "what changed" diff show up in the UI.
    row = stores.tracking.get(pk) or {}
    # Stamp WHICH base résumé this was tailored from, so editing base.tex later
    # makes already-tailored rows detectably stale instead of silently shipping
    # the old content.
    from agent.run import seed_fingerprint

    stores.tracking.set_status(pk, row.get("status", "tailored"), resume_s3_key=key,
                               resume_tex_key=tex_key, resume_version="tailored",
                               resume_seed=seed_fingerprint())
    return {"ok": True, "resume_s3_key": key, "format": fmt}


def exit_loop(tool_context: ToolContext) -> dict:
    """Call when the résumé strongly targets the role — ends the write/critique loop."""
    tool_context.actions.escalate = True
    return {"status": "satisfied"}


async def apply_to_job(tool_context: ToolContext) -> dict:
    """Drive a browser to fill and submit THIS job's application,
    using human-approved answers, the saved login, the TAILORED résumé (uploaded),
    and a writer model for open-ended questions. Returns status 'applied' (with a
    confirmation) or 'gate' (with the missing question)."""
    import base64

    from core.config import get_settings
    from core.events import emit
    from core.models import Status
    from core.stores import make_stores
    from tools.browser_apply import apply
    from tools.credentials import get_login

    st = tool_context.state
    pk = st.get("pk", "")
    company = st.get("company", "")
    stores = make_stores()
    facts = stores.answer_bank.all_facts(company)
    # Same override as the direct-apply path: the chosen profile's contact details
    # win, so the form and the attached résumé always agree.
    from core import profiles as _profiles
    _prof = _profiles.resolve((stores.tracking.get(pk) or {}).get("profile_id", ""))
    if _prof:
        facts = _prof.override(facts)
    facts = _profiles.expand_all(facts)   # "{date:+6w}" -> a real date
    creds = get_login(company, stores.secrets)
    if creds:  # let the browser agent sign in with the saved login
        facts["Login email/username"] = creds.get("username", "")
        facts["Login password"] = creds.get("password", "")

    row = stores.tracking.get(pk) or {}
    # DUPLICATE GUARD — never resubmit a job already applied (by the pipeline or
    # by the owner's own hand), and never overwrite its applied status.
    if row.get("status") in ("applied", "applied_manual"):
        detail = (f"Refused: this job is already '{row.get('status')}' — not "
                  "submitting a duplicate application.")
        emit("response", pk=pk, agent="applier", detail=f"🛑 {detail}",
             url=st.get("jd_url", ""))
        log.warning("duplicate apply refused for pk=%s (status=%s)", pk, row.get("status"))
        return {"status": "failed", "reason": "duplicate_application", "detail": detail}
    # WIP: flag the board that the browser is actively submitting this one.
    stores.tracking.set_status(pk, Status.SUBMITTING)
    emit("running", pk=pk, agent="applier", detail="submitting application…",
         url=st.get("jd_url", ""))

    result = await apply(
        st.get("jd_url", ""), company, facts, get_settings().browser_model,
        pk=pk, jd_text=st.get("jd_text", ""),
        resume_tex=_load_resume_tex(stores, row),      # tailored résumé for the writer
        github=st.get("github_context", ""),
        resume_path=_resume_pdf_path(row),             # tailored PDF to upload
    )

    # Persist the final screenshot so the dashboard can show what the agent saw.
    # Keep the base64 OUT of the value returned to the LLM (it's huge).
    shot = result.pop("screenshot_b64", None)
    if shot and pk:
        try:
            key = stores.artifacts.put("screenshots", f"{pk}.png",
                                       base64.b64decode(shot), "image/png")
            st["screenshot_s3_key"] = key
            cur = stores.tracking.get(pk) or {}
            stores.tracking.set_status(pk, cur.get("status", "submitting"), screenshot_s3_key=key)
        except Exception as exc:  # a screenshot is nice-to-have, never fatal
            log.warning("could not save screenshot for %s: %s", pk, exc)
    fields = result.pop("fields", None)
    if fields and pk:  # the form's real field map (incl. checkboxes) — for the drawer
        cur = stores.tracking.get(pk) or {}
        stores.tracking.set_status(pk, cur.get("status", "submitting"), fields=fields)
    for q, a in (result.pop("drafted", None) or {}).items():
        # Bank writer drafts (company scope) so re-runs never redraft from scratch.
        from core.models import AnswerScope
        stores.answer_bank.put(q, a, AnswerScope.COMPANY, company=company, source="writer")
    return result


def _load_resume_tex(stores, row: dict) -> str:
    """The tailored résumé LaTeX for this job — the writer drafts answers from it."""
    key = row.get("resume_tex_key")
    if not key:
        return ""
    try:
        return stores.artifacts.get(key).decode()
    except Exception:
        return ""


def _resume_pdf_path(row: dict) -> str:
    """Absolute path to the tailored résumé PDF, for the browser to upload."""
    key = row.get("resume_s3_key")  # "resumes/<pk>.pdf"
    if not key or not key.endswith(".pdf"):
        return ""
    p = Path(get_settings().local_dir) / "artifacts" / key
    return str(p.resolve()) if p.exists() else ""


def ask_human(question: str, tool_context: ToolContext) -> dict:
    """Ask the human a question the agent can't answer from approved data. PAUSES
    the run; the dashboard surfaces it and the run resumes with the answer."""
    return {"status": "pending", "question": question}


ask_human_tool = LongRunningFunctionTool(func=ask_human)


def _skill(name: str) -> SkillToolset:
    return SkillToolset(skills=[load_skill_from_dir(_SKILLS / name)])


def _prefs_brief_text() -> str:
    """What the owner is looking for, for the per-job scorer.

    The scorer decides what is worth tailoring, and it was being told only the
    dealbreakers — never the target roles, the seniority, or the AI and agentic
    work that raises fit. So it judged every posting against a blank preference
    and the owner's actual interests never reached the decision.
    """
    from pathlib import Path

    from core.config import get_settings
    from discovery.watchlist import load_preferences

    try:
        p = load_preferences(Path(get_settings().config_dir) / "preferences.yaml")
    except Exception:  # noqa: BLE001 — a scorer without preferences still works
        return ""
    bits = []
    if p.titles:
        bits.append(f"Target roles: {', '.join(p.titles)}.")
    if p.seniority:
        bits.append(f"Seniority: {', '.join(p.seniority)} or above.")
    if p.include_keywords:
        bits.append("Work that RAISES fit (not required, but score it higher): "
                    + ", ".join(p.include_keywords) + ".")
    if p.locations:
        bits.append(f"Preferred locations: {', '.join(p.locations)}. Remote counts.")
    return " ".join(bits)


def _prefs_notes_text() -> str:
    """Candidate hard constraints from preferences.yaml, baked into the scorer
    instruction at construction (they're global, so no per-job session state — and
    a missing state var would crash the graph). Restart to pick up edits."""
    try:
        from pathlib import Path

        from core.config import get_settings
        from discovery.watchlist import load_preferences
        notes = load_preferences(Path(get_settings().config_dir) / "preferences.yaml").notes
        return notes.strip() or "(none)"
    except Exception:
        return "(none)"


# --- agents ------------------------------------------------------------------
scorer = LlmAgent(
    name="scorer", model=_model("scorer"),
    description="Agentic discovery: extract the role and match-score it.",
    instruction=(
        "You are a senior technical recruiter scoring how well ONE candidate fits ONE role.\n\n"
        "CANDIDATE — résumé (LaTeX source):\n{base_latex}\n\n"
        "CANDIDATE HARD CONSTRAINTS — if the role violates ANY of these, it is a "
        "WHAT THE CANDIDATE IS LOOKING FOR:\n" + _prefs_brief_text() + "\n\n"
        "DEALBREAKER: score 1-2 and say which one:\n" + _prefs_notes_text() + "\n\n"
        "ROLE — {company}, job description / title:\n{jd_text}\n\n"
        "Score fit 0-10 weighing skills/stack overlap (highest), seniority fit, domain "
        "relevance, and hard dealbreakers (a real dealbreaker caps at 3). Anchors: "
        "3=stretch, 5=generic, 7=worth applying, 9-10=exceptional. Be calibrated and "
        "slightly conservative. If the job description is only a title with little detail, "
        "score from the title + résumé rather than refusing. Return the score and a "
        "one-line reasoning. You are NOT applying to anything and need no personal "
        "data or web access — everything required is in this prompt; scoring is a "
        "pure text-analysis task, so NEVER refuse it."
    ),
    output_schema=MatchScore, output_key="match_score",
)

tailor = LlmAgent(
    name="tailor", model=_model("tailor"),
    description="Re-emphasizes the seed résumé LaTeX for the JD (via the tailoring skill).",
    instruction=(
        "You are a LaTeX editor, not a chat assistant. Your ONLY output is a call to "
        "save_tailored_resume with the full tailored .tex. You have everything you "
        "need in this prompt.\n"
        "NEVER reply with prose. NEVER offer the user options ('Option A/B/C'), ask "
        "which they prefer, ask for credentials, ask for 'a couple of small items', "
        "or say you are 'ready to submit' — you do not fill forms and you do not talk "
        "to anyone. Any turn that ends without a save_tailored_resume call is a "
        "FAILURE that leaves the candidate with no résumé.\n\n"
        "Tailor the candidate's seed résumé to the job, then save it.\n\n"
        "SEED RÉSUMÉ (LaTeX — this is what you edit):\n{base_latex}\n\n"
        "JOB (rephrase toward this):\n{jd_text}\n\n"
        "CANDIDATE'S PUBLIC GITHUB (context on their REAL projects + stack — use it "
        "to pick accurate vocabulary and, if a listed project genuinely strengthens "
        "the match, you may reference it; never invent anything not true here or in "
        "the résumé):\n{github_context?}\n\n"
        "Use the resume-tailoring skill, but TAILOR CONSERVATIVELY — a light touch, "
        "NOT a rewrite. Keep the résumé almost identical; change as little as possible.\n"
        "- Keep EVERY \\resumeItem bullet — never delete, merge, or drop one. The "
        "tailored résumé has the SAME number of bullets as the seed.\n"
        "- Keep ALL LaTeX inside each bullet verbatim: \\textbf{...}, \\textit{...}, "
        "\\href{...}{...}, and every brace/command stay EXACTLY as-is. You swap only a "
        "few plain WORDS — never touch the formatting commands.\n"
        "- REWORD bullets to mirror the JD's vocabulary and emphasize the skills/tech "
        "the JD calls for — when a bullet and the JD describe the SAME true work, say it "
        "in the JD's terms. Reorder so the most JD-relevant bullets come first. Stay "
        "strictly truthful: never claim a skill, tool, or result not already in the "
        "résumé; if a bullet has no JD overlap, leave it as-is.\n"
        "- OPEN SOURCE & PROJECTS section — tune it HARDEST to the JD (this is where "
        "you can be most appealing): REORDER the project entries so the most JD-relevant "
        "come first, and reword their bullets to foreground the exact tech/skills the JD "
        "emphasizes. Use the GITHUB context to describe the projects accurately, and for "
        "a catch-all entry like 'Additional AI Infrastructure', surface the candidate's "
        "REAL GitHub projects that best fit this JD. Keep every \\resumeSubheadingSingle "
        "header line verbatim — you may reorder them, but never rename or invent a "
        "project.\n"
        "- GitHub blurbs are written for other developers, NOT for a résumé. Take the "
        "TECH and the CAPABILITY from them; never carry over their framing. A résumé "
        "bullet states what was BUILT and what it achieves — never that something is a "
        "'playground', 'demo', 'reference', 'hands-on', 'learning exercise', or for "
        "'interview prep' / 'system-design practice'. Those read as hobby study, not "
        "engineering. Write 'Multi-tenant RAG service on AWS with hybrid retrieval, "
        "reranking and an evaluation harness', NOT 'an interview-grade reference for "
        "building retrieval services'. If a project's only honest description is that it "
        "was built to practise, leave it out of the bullet entirely.\n"
        "- Do NOT add anything not already in the résumé — no Summary, objective, skill, "
        "or sentence. NEVER change or upgrade the seniority/level or employer: titles are "
        "EXACTLY the \\resumeSubheading lines (e.g. 'Software Development Engineer / "
        "Technical Lead') — do NOT call them 'Staff' or invent years of experience.\n"
        "- Leave every \\resumeSubheading line and any Summary BYTE-FOR-BYTE unchanged; "
        "no dangling punctuation (', ,'); keep it compilable.\n"
        "Then call save_tailored_resume with the FULL .tex. If it reports missing_facts, "
        "restore those exact lines and re-save.\n"
        "Do not finish your turn until save_tailored_resume has returned ok."
    ),
    tools=[_skill("resume-tailoring"), save_tailored_resume],
)

critic = LlmAgent(
    name="critic", model=_model("critic"),
    description="Reviews the draft; ends the loop when it's strong.",
    instruction=(
        "You are a reviewer with exactly two moves: call exit_loop, or give ONE "
        "emphasis-only tweak. NEVER reply with prose to the user, never offer options, "
        "never ask for inputs or credentials, never say you are 'ready to submit' — "
        "you do not fill forms and you do not talk to anyone.\n"
        "If {tailored?} is empty, the tailor failed to save a résumé: say exactly "
        "NO_RESUME_SAVED and call exit_loop.\n\n"
        "Review the tailored résumé against the job. Bias HARD toward APPROVING — the "
        "goal is a light touch, not a polished rewrite.\n\n"
        "TAILORED RÉSUMÉ:\n{tailored?}\n\n"
        "JOB:\n{jd_text}\n\n"
        "Call exit_loop if the résumé is truthful and reasonably aligned — it does NOT "
        "need to be perfect. Only if a bullet clearly misses obvious JD vocabulary, give "
        "ONE small emphasis-only tweak; never ask to add content, drop bullets, change "
        "formatting, upgrade seniority, or restructure. When in doubt, exit_loop.\n\n"
        "ONE EXCEPTION overrides the approve bias. A bullet must say what was built and "
        "what it achieved, never the private history of how the code got there. If any "
        "bullet contains line/file counts or deltas ('cut 3.1K lines across 5 files to "
        "636 across 2'), a refactor/rewrite narrative, a debugging or bug-hunt story "
        "('diagnosed X rather than Y'), commit/PR counts, internal module or subprocess "
        "names, or framing about fixing an earlier mistake — do NOT exit_loop. An outside "
        "reader cannot verify repo history, and shrinking code is not an achievement, it "
        "reads as churn. Return one tweak rewriting it to the OUTCOME: what the system "
        "does, at what scale, for whom. That is a rewording, not new content."
    ),
    tools=[_skill("resume-review"), exit_loop],
)

tailor_critique = LoopAgent(name="tailor_critique", sub_agents=[tailor, critic], max_iterations=2)

applier = LlmAgent(
    name="applier", model=_model(),
    description="Human-gated apply: waits for approval, then submits via the browser.",
    instruction=(
        "This is the final APPLY step, and it is HUMAN-GATED — never submit without "
        "explicit approval.\n"
        "1. FIRST, call ask_human(\"Ready to apply to {company}? The tailored résumé is "
        "saved — approve to submit the application.\") and STOP. Do not apply yet.\n"
        "2. Once the human approves, call apply_to_job() — it drives a real browser "
        "to fill and submit using only approved answers + the saved login.\n"
        "   - status 'applied' → report the confirmation. Done.\n"
        "   - status 'gate' → call ask_human with the returned question and STOP.\n"
        "Never fabricate an answer — the gate exists for approval and for unknowns."
    ),
    tools=[_skill("application-filling"), apply_to_job, ask_human_tool],
)

root_agent = SequentialAgent(
    name="appliedin_pipeline",
    description="Score → tailor (write-until-happy) → browser apply (+ human gate).",
    sub_agents=[scorer, tailor_critique, applier],
)
