"""Free-text application answers, drafted from the candidate's real material.

When a job form asks an open-ended question ("Why are you interested in this
role?", "Describe your experience with X"), we don't gate the human — a
dedicated WRITER model (Sonnet by default) drafts a compelling, truthful answer
grounded ONLY in the résumé, the candidate's GitHub, and the JD. The goal is to
land an interview. If the question needs a fact that isn't in that material, it
declines (CANNOT_ANSWER) so the human is asked instead.
"""

from __future__ import annotations

import re

from core.logging import get_logger

log = get_logger(__name__)

_CANNOT = "CANNOT_ANSWER"


def _strip_dashes(text: str) -> str:
    """The candidate never wants dashes in written answers (a common AI tell).
    Replace em/en dashes with commas, de-hyphenate ` - ` separators AND compound
    words (multi-agent → multi agent), so no '-' survives at all."""
    text = text.replace("—", ", ").replace("–", ", ")  # — –
    text = re.sub(r"\s+-\s+", ", ", text)   # " - " used as punctuation
    text = re.sub(r"(\w)-(\w)", r"\1 \2", text)  # compound hyphens
    text = re.sub(r",\s*,", ", ", text)     # tidy any doubled commas
    return text.strip()


def draft_answer(question: str, company: str, jd_text: str, *,
                 resume: str = "", github: str = "", model: str | None = None) -> dict:
    """Draft a truthful, persuasive answer to a free-text application question
    from the TAILORED résumé + GitHub + JD. Returns {"answer": str}, or
    {"answer": "", "cannot": True} if it can't be answered from that material.

    `resume` should be the same tailored résumé used for THIS application (falls
    back to the base résumé); `github` the candidate's GitHub summary."""
    from litellm import completion

    from agent.run import _base_latex, _github_context  # same context the tailor uses
    from core.config import get_settings

    model = model or get_settings().agent_model("writer")
    resume = resume or _base_latex()
    github = github or _github_context()
    prompt = (
        "You are drafting the CANDIDATE'S answer to a free-text question on a real "
        "job application. Write in the first person as the candidate. The goal is to "
        "win an interview: specific, confident, genuinely compelling.\n\n"
        "GROUNDING — where the substance comes from, in strict priority order:\n"
        "1. PROFESSIONAL WORK from the résumé is the backbone of the answer: real "
        "employers, systems shipped in production, scale, outcomes, the patent, "
        "leadership. Lead with the single strongest professional accomplishment that "
        "matches what THIS role needs, and let employment history carry the answer.\n"
        "2. The JOB DESCRIPTION decides which of that work to emphasize: mirror its "
        "actual needs and vocabulary, never generic praise of the company.\n"
        "3. GITHUB is seasoning, not substance: AT MOST one short mention of one "
        "project, and only when it adds something the professional work cannot. Many "
        "answers need no GitHub mention at all. Never cite multiple side projects, "
        "and never let a side project outweigh employment experience.\n\n"
        "HARD RULES:\n"
        "- Every claim must be TRUE per the material below. NEVER invent an employer, "
        "title, metric, skill, or project that isn't there.\n"
        "- NEVER use a dash of any kind (no '—', no '–', no '-'). Use commas or short "
        "sentences instead. Do not hyphenate compound words; write them as separate "
        "words or rephrase.\n"
        "- Concrete beats abstract: name the actual system, scale, or result rather "
        "than saying 'extensive experience'. No filler ('I am passionate about…').\n"
        f"- If the question needs a fact you cannot find in the material (a number, a "
        f"credential, a personal preference), reply with exactly {_CANNOT} and nothing "
        "else.\n\n"
        f"QUESTION:\n{question}\n\n"
        f"ROLE — {company}, job description:\n{jd_text[:4000]}\n\n"
        f"RÉSUMÉ (LaTeX source — the professional record; the backbone):\n{resume[:6000]}\n\n"
        f"GITHUB (supporting color only):\n{github[:1500]}\n\n"
        "Write only the answer text (2 to 5 sentences unless the form clearly wants "
        f"more), or {_CANNOT}. No preamble, no quotes, no dashes."
    )
    try:
        resp = completion(model=model, messages=[{"role": "user", "content": prompt}])
        text = (resp["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        log.warning("narrative draft failed (%s)", exc)
        return {"answer": "", "cannot": True}

    if not text or _CANNOT in text.upper()[:40]:
        return {"answer": "", "cannot": True}
    return {"answer": _strip_dashes(text)}
