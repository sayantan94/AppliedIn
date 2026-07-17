"""AppliedIn — the agentic pipeline in pure ADK (no Strands).

Sequential pipeline, each stage the pattern that fits it:

  root = SequentialAgent
    1. scorer          SINGLE-AGENT + structured output (MatchScore, no parsing).
    2. tailor_critique REVIEW-&-CRITIQUE (LoopAgent): the tailor operates on the
                       seed résumé via the `resume-tailoring` SKILL and saves a
                       TYPED résumé (save_tailored_resume — validated, no regex);
                       the critic refines until it calls exit_loop.
    3. applier         LlmAgent that delegates to browser-use (apply_to_job) —
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


def _model() -> LiteLlm:
    model = os.environ.get("APPLIEDIN_ADK_MODEL") or get_settings().litellm_model
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
    artifacts = stores.artifacts  # filesystem (local) or S3 (cloud) — same call
    tex_key = artifacts.put("resumes", f"{pk}.tex", tailored_latex.encode(), "text/x-tex")
    try:
        key = artifacts.put("resumes", f"{pk}.pdf", render_pdf(tailored_latex), "application/pdf")
        fmt = "pdf"
    except RuntimeError as exc:  # tectonic not installed — fall back to source
        log.warning("PDF render failed (%s); storing .tex only", exc)
        key = tex_key
        fmt = "tex"

    tool_context.state["resume_s3_key"] = key
    tool_context.state["tailored"] = tailored_latex
    # Record on the TRACKING ROW too — that's what the dashboard reads, so this is
    # how the résumé PDF + the "what changed" diff show up in the UI.
    row = stores.tracking.get(pk) or {}
    stores.tracking.set_status(pk, row.get("status", "tailored"), resume_s3_key=key,
                               resume_tex_key=tex_key, resume_version="tailored")
    return {"ok": True, "resume_s3_key": key, "format": fmt}


def exit_loop(tool_context: ToolContext) -> dict:
    """Call when the résumé strongly targets the role — ends the write/critique loop."""
    tool_context.actions.escalate = True
    return {"status": "satisfied"}


async def apply_to_job(tool_context: ToolContext) -> dict:
    """Drive a browser (browser-use) to fill and submit THIS job's application,
    using only human-approved answers + the saved portal login. Returns
    status 'applied' (with a confirmation) or 'gate' (with the missing question)."""
    from core.config import get_settings
    from core.stores import make_stores
    from tools.browser_apply import apply
    from tools.credentials import get_login

    st = tool_context.state
    pk = st.get("pk", "")
    company = st.get("company", "")
    stores = make_stores()
    facts = stores.answer_bank.all_facts(company)
    creds = get_login(company, stores.secrets)
    if creds:  # let the browser agent sign in with the saved login
        facts["Login email/username"] = creds.get("username", "")
        facts["Login password"] = creds.get("password", "")

    result = await apply(st.get("jd_url", ""), company, facts, get_settings().browser_model, pk=pk)

    # Persist the final screenshot so the dashboard can show what the agent saw.
    # Keep the base64 OUT of the value returned to the LLM (it's huge).
    shot = result.pop("screenshot_b64", None)
    if shot and pk:
        try:
            import base64
            key = stores.artifacts.put("screenshots", f"{pk}.png",
                                       base64.b64decode(shot), "image/png")
            st["screenshot_s3_key"] = key
            row = stores.tracking.get(pk) or {}
            stores.tracking.set_status(pk, row.get("status", "applying"), screenshot_s3_key=key)
        except Exception as exc:  # a screenshot is nice-to-have, never fatal
            log.warning("could not save screenshot for %s: %s", pk, exc)
    return result


def ask_human(question: str, tool_context: ToolContext) -> dict:
    """Ask the human a question the agent can't answer from approved data. PAUSES
    the run; the dashboard surfaces it and the run resumes with the answer."""
    return {"status": "pending", "question": question}


ask_human_tool = LongRunningFunctionTool(func=ask_human)


def _skill(name: str) -> SkillToolset:
    return SkillToolset(skills=[load_skill_from_dir(_SKILLS / name)])


# --- agents ------------------------------------------------------------------
scorer = LlmAgent(
    name="scorer", model=_model(),
    description="Agentic discovery: extract the role and match-score it.",
    instruction=(
        "You are a senior technical recruiter scoring how well ONE candidate fits ONE role.\n\n"
        "CANDIDATE — résumé (LaTeX source):\n{base_latex}\n\n"
        "ROLE — {company}, job description / title:\n{jd_text}\n\n"
        "Score fit 0-10 weighing skills/stack overlap (highest), seniority fit, domain "
        "relevance, and hard dealbreakers (a real dealbreaker caps at 3). Anchors: "
        "3=stretch, 5=generic, 7=worth applying, 9-10=exceptional. Be calibrated and "
        "slightly conservative. If the job description is only a title with little detail, "
        "score from the title + résumé rather than refusing. Return the score and a "
        "one-line reasoning."
    ),
    output_schema=MatchScore, output_key="match_score",
)

tailor = LlmAgent(
    name="tailor", model=_model(),
    description="Re-emphasizes the seed résumé LaTeX for the JD (via the tailoring skill).",
    instruction=(
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
        "- Do NOT add anything not already in the résumé — no Summary, objective, skill, "
        "or sentence. NEVER change or upgrade the seniority/level or employer: titles are "
        "EXACTLY the \\resumeSubheading lines (e.g. 'Software Development Engineer / "
        "Technical Lead') — do NOT call them 'Staff' or invent years of experience.\n"
        "- Leave every \\resumeSubheading line and any Summary BYTE-FOR-BYTE unchanged; "
        "no dangling punctuation (', ,'); keep it compilable.\n"
        "Then call save_tailored_resume with the FULL .tex. If it reports missing_facts, "
        "restore those exact lines and re-save."
    ),
    tools=[_skill("resume-tailoring"), save_tailored_resume],
)

critic = LlmAgent(
    name="critic", model=_model(),
    description="Reviews the draft; ends the loop when it's strong.",
    instruction=(
        "Review the tailored résumé against the job. Bias HARD toward APPROVING — the "
        "goal is a light touch, not a polished rewrite.\n\n"
        "TAILORED RÉSUMÉ:\n{tailored?}\n\n"
        "JOB:\n{jd_text}\n\n"
        "Call exit_loop if the résumé is truthful and reasonably aligned — it does NOT "
        "need to be perfect. Only if a bullet clearly misses obvious JD vocabulary, give "
        "ONE small emphasis-only tweak; never ask to add content, drop bullets, change "
        "formatting, upgrade seniority, or restructure. When in doubt, exit_loop."
    ),
    tools=[_skill("resume-review"), exit_loop],
)

tailor_critique = LoopAgent(name="tailor_critique", sub_agents=[tailor, critic], max_iterations=2)

applier = LlmAgent(
    name="applier", model=_model(),
    description="Human-gated apply: waits for approval, then submits via browser-use.",
    instruction=(
        "This is the final APPLY step, and it is HUMAN-GATED — never submit without "
        "explicit approval.\n"
        "1. FIRST, call ask_human(\"Ready to apply to {company}? The tailored résumé is "
        "saved — approve to submit the application.\") and STOP. Do not apply yet.\n"
        "2. Once the human approves, call apply_to_job() — it drives a real browser "
        "(browser-use) to fill and submit using only approved answers + the saved login.\n"
        "   - status 'applied' → report the confirmation. Done.\n"
        "   - status 'gate' → call ask_human with the returned question and STOP.\n"
        "Never fabricate an answer — the gate exists for approval and for unknowns."
    ),
    tools=[_skill("application-filling"), apply_to_job, ask_human_tool],
)

root_agent = SequentialAgent(
    name="appliedin_pipeline",
    description="Score → tailor (write-until-happy) → apply via browser-use (+ human gate).",
    sub_agents=[scorer, tailor_critique, applier],
)
