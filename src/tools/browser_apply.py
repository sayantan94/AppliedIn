"""Actual applying, via browser-use (a browser-agent framework, on Anthropic).

ADK orchestrates (find → score → tailor → gate/resume); browser-use is the
agent that drives the real browser to fill and submit the application. It's
given ONLY the human-approved answers and told never to invent one — if a
required field has no approved answer (or a login/CAPTCHA blocks it), it stops
and reports, and the ADK applier turns that into a human gate.
"""

from __future__ import annotations

from core.logging import get_logger

log = get_logger(__name__)


async def apply(url: str, company: str, facts: dict, model: str, pk: str = "") -> dict:
    """Drive a browser to apply. Returns one of:
      {status:'applied', confirmation}   {status:'gate', reason, question}
      {status:'unknown', detail}
    Each browser step is streamed to the dashboard under this job's `pk`.
    """
    try:
        from browser_use import Agent

        from tools.browser_llm import make_llm
    except Exception as exc:  # not installed
        log.error("browser-use not installed: %s (run ./setup.sh)", exc)
        return {"status": "unknown", "detail": "browser-use unavailable"}

    facts_str = "\n".join(f"- {q}: {a}" for q, a in facts.items()) or "(none provided)"
    task = (
        f"Go to {url} and complete the job application for {company}.\n"
        f"Fill each field using ONLY these approved answers:\n{facts_str}\n\n"
        "Rules:\n"
        "- NEVER invent an answer. If a REQUIRED field has no approved answer above, "
        "STOP and finish your response with exactly:  MISSING: <the exact field label>\n"
        "- If the portal requires creating an account, or shows a CAPTCHA, STOP and "
        "finish with:  BLOCKED: <reason>\n"
        "- After a successful submit, finish with:  APPLIED: <confirmation id or 'submitted'>"
    )
    def _on_step(_state: object, output: object, n: int) -> None:  # streamed to the UI
        goal = (getattr(output, "next_goal", "") or "").strip()
        if goal and pk:
            from core.events import emit
            emit("response", pk=pk, agent="browser", detail=f"[step {n}] {goal}"[:240], url=url)

    agent = Agent(task=task, llm=make_llm(model), register_new_step_callback=_on_step)
    history = await agent.run(max_steps=60)
    text = (history.final_result() if hasattr(history, "final_result") else str(history)) or ""
    shot = _last_screenshot(history)  # base64 PNG of where it ended up (or None)

    if "MISSING:" in text:
        result = {"status": "gate", "reason": "low_confidence",
                  "question": text.split("MISSING:", 1)[1].strip()[:200]}
    elif "BLOCKED:" in text:
        result = {"status": "gate", "reason": "no_account",
                  "question": text.split("BLOCKED:", 1)[1].strip()[:200]}
    elif "APPLIED:" in text:
        result = {"status": "applied", "confirmation": text.split("APPLIED:", 1)[1].strip()[:80]}
    else:
        result = {"status": "unknown", "detail": text[:300]}
    result["screenshot_b64"] = shot
    return result


def _last_screenshot(history: object) -> str | None:
    """The final page screenshot as base64 PNG, so the UI can show what the agent
    saw when it finished (applied, or stuck at a gate)."""
    try:
        shots = history.screenshots(n_last=1) or []  # type: ignore[attr-defined]
    except Exception:
        return None
    return next((s for s in shots if s), None)
