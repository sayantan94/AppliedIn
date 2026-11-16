"""AppliedIn — the agentic pipeline

Sequential pipeline, each stage the pattern that fits it:

  root = SequentialAgent
    1. scorer          structured output (MatchScore, no parsing).
    2. tailor_critique REVIEW-&-CRITIQUE (LoopAgent): the tailor operates on the
                       seed résumé via the `resume-tailoring` SKILL and saves a
                       TYPED résumé (save_tailored_resume — validated, no regex);
                       the critic refines until it calls exit_loop.
    3. applier         Agent that delegates to the browser apply (apply_to_job) —
                       a real browser agent that fills & submits from
                       human-approved answers only; HUMAN-IN-THE-LOOP workflow when required

Model follows the mode (Anthropic local / Bedrock cloud) via core.config.
"""

from __future__ import annotations

import os
from pathlib import Path
from contextvars import ContextVar

# Invocation-local evidence, never inherited from a durable ADK session.
_PDF_INVOCATION: ContextVar[dict | None] = ContextVar("pdf_invocation", default=None)

from google.adk.agents import LlmAgent, LoopAgent, SequentialAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.skills import load_skill_from_dir
from google.adk.tools import LongRunningFunctionTool, ToolContext
from google.adk.tools.skill_toolset import SkillToolset

from core.config import get_settings
from core.logging import get_logger
from core.steering import before_model
from tools.schema import MatchScore
from discovery.relevance import classify_location_fit

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
    from core.role_titles import load_approved_titles
    from core.stores import make_stores
    from tools.render import render_pdf, restore_preamble
    from tools.truthfulness import active_calls, used_macros, validate

    invocation = _PDF_INVOCATION.get()
    if invocation is not None:
        invocation["attempted"] = True

    base_latex = tool_context.state.get("base_latex") or ""
    # Without the seed there is nothing to validate against, and both checks below
    # would pass trivially: `_anchors("")` is empty, so every fact counts as
    # surviving, and a bullet count of zero can never be reduced. Fail closed —
    # an unverifiable résumé must not reach an employer.
    if not base_latex.strip():
        log.error("no base résumé in session state for %s — refusing to save",
                  tool_context.state.get("pk", "?"))
        return {"ok": False,
                "message": "The seed résumé is missing from this run, so nothing "
                           "can be checked against it. Stop and report this rather "
                           "than saving."}
    # A non-empty seed with no recognizable live structure or bullets is just
    # as unverifiable as a missing one. Refuse it before competing diagnostics.
    base_used = used_macros(base_latex)
    if not base_used["anchors"] or not base_used["bullets"]:
        return {"ok": False,
                "message": "Unrecognized seed résumé format: no active supported "
                           "structural anchors or résumé-body bullets found. "
                           "Use a seed with supported role/heading and bullet calls, "
                           "then re-save."}

    # Nothing above \begin{document} is the tailor's to write, and a slip there is
    # invisible to every check below — a doubled backslash in the \resumeSubheading
    # macro once put the literal word "textit" in front of every job title on a PDF
    # that compiled, validated, and was queued for an employer. Put the seed's
    # preamble back rather than spending a turn asking the model to try again.
    restored = restore_preamble(base_latex, tailored_latex)
    if restored != tailored_latex:
        log.warning("tailor altered the preamble for %s — restored it from the seed",
                    tool_context.state.get("pk", "?"))
        tailored_latex = restored

    # The private file is a runtime input only. There is no edit tool, and only
    # the same filtered, seed-eligible options reach either model instruction.
    approved_titles = load_approved_titles(get_settings().local_dir)
    base_items = len(active_calls(base_latex, base_used["bullets"]))

    def _check_content(tex: str) -> dict | None:
        # Keep the legacy active-bullet and Counter guards on EVERY candidate,
        # including final material after any profile/render repair.
        new_items = len(active_calls(tex, base_used["bullets"]))
        if new_items < base_items:
            macros = " / ".join(base_used["bullets"])
            return {"ok": False,
                    "message": f"You dropped {base_items - new_items} bullet(s) "
                               f"({new_items} vs {base_items}). Keep EVERY {macros} — "
                               "restore the missing ones (reword, never delete) and re-save."}
        violations = validate(base_latex, tex, approved_titles)
        if violations:
            return {"ok": False, "missing_facts": violations,
                    "message": "These facts were dropped/altered — restore them verbatim "
                               "except for an eligible approved employment-title slot, "
                               "then re-save."}
        return None

    failure = _check_content(tailored_latex)
    if failure:
        return failure

    pk = tool_context.state.get("pk", "resume")
    stores = invocation["stores"] if invocation is not None else make_stores()
    if invocation is not None:
        from agent.run import _reader_check
        if pk != invocation["row"].get("pk") or _reader_check(pk, invocation["row"], stores):
            return {"ok": False, "reason": "preparation_state_changed"}

    # The application goes out under a chosen profile, so the PDF must carry that
    # profile's contact details — a form saying one address while the attached
    # résumé says another is the kind of mismatch a recruiter notices.
    from core import profiles as _profiles
    _prof = _profiles.resolve_for(stores.tracking.get(pk) or {})
    tailored_latex = _profiles.apply_to_latex(tailored_latex, _prof)
    failure = _check_content(tailored_latex)
    if failure:
        return failure
    artifacts = stores.artifacts  # filesystem (local) or S3 (cloud) — same call

    # Render and revalidate the FINAL content before writing either artifact.
    # The old sequence wrote a .tex before render, then let the sanitizer
    # publish a different, unguarded .tex/.pdf after a compile failure.
    fmt, pdf_bytes = "pdf", None
    try:
        pdf_bytes = render_pdf(tailored_latex)
    except RuntimeError as exc:
        from tools.render import sanitize_latex

        # Restore the already profile-selected head if the repair touched it.
        # Thus both the PDF input and persisted TeX retain the protected head.
        fixed = restore_preamble(tailored_latex, sanitize_latex(tailored_latex))
        if fixed != tailored_latex:
            failure = _check_content(fixed)
            if failure:
                return failure
            try:
                pdf_bytes = render_pdf(fixed)
            except RuntimeError:
                pdf_bytes = None
        if pdf_bytes is not None:
            tailored_latex = fixed
            log.info("PDF render repaired by escaping bare specials for %s", pk)
        else:
            # A failed render must never turn TeX or a retained older PDF into
            # successful evidence for this invocation.
            log.warning("PDF render failed; retaining prior artifacts")
            return {"ok": False, "format": "pdf", "reason": "pdf_compile_failed",
                    "message": "PDF compilation failed. Fix the LaTeX syntax and "
                               "re-save the full document; prior artifacts are retained."}

    # The final candidate (after profile application and any render repair) must
    # not carry an agent tell. Checked before the first write, so a rejected
    # candidate leaves the previous artifacts and tracking state untouched.
    from tools.freetext_check import tells
    hits = tells(tailored_latex)
    if hits:
        log.warning("tailored résumé for %s carries agent tells %s; not saved", pk, hits)
        return {"ok": False, "reason": "agent_tells", "tells": hits,
                "message": "The résumé contains phrases that read as written by an AI "
                           f"agent: {', '.join(hits)}. Reword those passages in plain "
                           "prose (keep every fact and bullet) and re-save the full "
                           "document; prior artifacts are retained."}

    # No write precedes this point. The initial, profile-adjusted and repaired
    # candidates all pass the same active anchor/bullet checks.
    # Generation-specific keys preserve prior evidence even if a concurrent
    # held/applied row wins the conditional tracking commit after the upload.
    from uuid import uuid4
    name = f"{pk}-{uuid4().hex}" if invocation is not None else pk
    tex_key = artifacts.put("resumes", f"{name}.tex", tailored_latex.encode(), "text/x-tex")
    key = artifacts.put("resumes", f"{name}.pdf", pdf_bytes, "application/pdf")
    from agent.run import _reader_check, _reader_patch, seed_fingerprint
    from datetime import datetime, timezone

    patch = dict(tailored_at=datetime.now(timezone.utc).isoformat(),
                 resume_s3_key=key, resume_tex_key=tex_key,
                 resume_version="tailored", resume_seed=seed_fingerprint())
    if invocation is not None:
        if _reader_check(pk, invocation["row"], stores):
            return {"ok": False, "reason": "preparation_state_changed"}
        committed, updated = _reader_patch(pk, invocation["row"], stores, patch)
        if not committed:
            return {"ok": False, "reason": "preparation_state_changed"}
        invocation["row"] = updated
    else:
        row = stores.tracking.get(pk) or {}
        stores.tracking.set_status(pk, row.get("status", "tailored"), **patch)
    tool_context.state["resume_s3_key"] = key
    tool_context.state["tailored"] = tailored_latex
    result = {"ok": True, "resume_s3_key": key, "format": "pdf"}
    if invocation is not None:
        import hashlib
        invocation["saved"] = dict(result)
        invocation["digest"] = hashlib.sha256(pdf_bytes).hexdigest()
    return result


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
    from tools.browser_runtime import configuration

    st = tool_context.state
    pk = st.get("pk", "")
    company = st.get("company", "")
    # This ADK tool has no queue-dispatch ownership. Even when another worker
    # leased this exact pk, its in-flight membership cannot authorize this call.
    # The BrowserSkill writer is available only through the explicit, approved
    # ApplyQueue.next -> run_queued -> _apply_direct route.
    try:
        engine = configuration()["engine"]
    except Exception:  # Unknown configuration cannot select a safe apply path.
        engine = None
    if engine != "chrome":
        detail = (
            "BrowserSkill ADK apply is not a leased dispatch. Obtain explicit "
            "approval and use the existing ApplyQueue.next -> run_queued -> "
            "_apply_direct path; no application was queued or submitted."
            if engine == "browser_skill" else
            "The application engine could not be verified. Fix its configuration "
            "before explicitly approving the existing leased queue dispatch; "
            "no application was queued or submitted."
        )
        return {"status": "gate",
                "reason": ("leased_dispatch_required" if engine == "browser_skill"
                           else "browser_engine_unverified"),
                "question": detail, "detail": detail}

    from tools.browser_apply import apply
    from tools.credentials import get_login
    stores = make_stores()
    from tools import submit_hold
    if submit_hold.blocked(pk, stores.tracking.get(pk) or {}, tracking=stores.tracking):
        return {'status': 'uncertain', 'reason': 'uncertain', 'detail': submit_hold.REASON}
    facts = stores.answer_bank.all_facts(company)
    # Last call on which address this goes out under. A job tailored before this
    # company started rotating still carries the old one, and this is the last
    # moment to fix it — the résumé is re-rendered from its saved .tex, so the
    # PDF and the form still agree.
    from core import rotation as _rotation

    # Deliberately NOT wrapped. If this company rotates and no address can be
    # assigned, the application must not go out at all — falling back to the base
    # address is the one outcome rotation exists to prevent, and it cannot be
    # undone. The failure is loud and nothing is submitted.
    _rotation.ensure(pk, company, stores)
    # Same override as the direct-apply path: the chosen profile's contact details
    # win, so the form and the attached résumé always agree.
    from core import profiles as _profiles
    _prof = _profiles.resolve_for(stores.tracking.get(pk) or {})
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
    from tools.browser_runtime import configuration
    drafted = result.pop('drafted', None) or {}
    if configuration()['engine'] != 'browser_skill':
        # Storage boundary only: this runs after apply(...) above, so it cannot
        # know what that call already typed. It keeps a tell-bearing draft out of
        # the answer bank, where it would be reused for future applications.
        from tools.freetext_check import tells
        for q, a in drafted.items():
            from core.models import AnswerScope
            hits = tells(str(a))
            if hits:
                log.warning("writer answer for %s not saved to the answer bank: tells %s",
                            pk, hits)
                continue
            stores.answer_bank.put(q, a, AnswerScope.COMPANY, company=company, source='writer')
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



# ADK 2.5.0 validates a final output_schema response before yielding its event.
# A malformed reply therefore cannot be repaired by the runner's event loop.
# Keep recovery inside this agent so the sequential graph cannot enter tailoring
# without a fresh, terminal, schema-validated MatchScore.
from contextlib import aclosing
from contextvars import ContextVar

from core import steering as _steering


class ScorerInvalidOutput(Exception):
    """Both scorer generations completed without a valid terminal MatchScore."""


class ScorerModelError(RuntimeError):
    """The scorer model/transport failed; unlike bad content this is an error."""


class _ScorerBadContent(Exception):
    """A normally completed model generation returned invalid score content."""


_SCORER_REASK: ContextVar[bool] = ContextVar("scorer_reask", default=False)
_SCORER_STEERING: ContextVar[str | None] = ContextVar("scorer_steering", default=None)
_SCORER_FORMAT_CORRECTION = (
    "Your previous response did not validate as MatchScore. Using the SAME "
    "résumé, job description, preferences and constraints above, reply with "
    "ONLY one JSON object with score (integer 0..10), reasoning (one-line "
    "string), and optionally dealbreaker (object with kind: location, "
    "authorization, or other, and optional verbatim JD quote string). "
    "Omit dealbreaker when not evidenced. No Markdown or other text."
)


def _scorer_before_model(callback_context, llm_request):
    # Freeze the existing before_model steering across the two attempts. A
    # change on disk between calls must not change the candidate being scored.
    guidance = _SCORER_STEERING.get()
    if guidance is None:
        before_model(callback_context, llm_request)
    elif guidance:
        llm_request.append_instructions([guidance])
    if _SCORER_REASK.get():
        llm_request.append_instructions([_SCORER_FORMAT_CORRECTION])



def _normalize_no_claim_response(llm_response, parts, text: str) -> str:
    """Change the actual ADK response only for a strictly typed no-claim."""
    import json

    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return text
    if (not isinstance(obj, dict) or
            not isinstance(obj.get("dealbreaker"), str) or
            obj["dealbreaker"].strip().casefold() not in {"", "none", "n/a"} or
            type(obj.get("score")) is not int or
            not 0 <= obj["score"] <= 10 or
            ("reasoning" in obj and not isinstance(obj["reasoning"], str))):
        return text
    normalized = {**obj, "dealbreaker": None}
    canonical = json.dumps(normalized, ensure_ascii=True)
    # ADK saves this same object via __maybe_save_output_to_state; never
    # normalize a shadow copy after its output-key state has already changed.
    live_parts = [p for p in parts if not getattr(p, "thought", False)
                  and isinstance(getattr(p, "text", None), str)]
    if not live_parts:
        return text
    try:
        original_parts = [p.text for p in live_parts]
        live_parts[0].text = canonical
        for p in live_parts[1:]:
            p.text = ""
        observed = "".join(p.text or "" for p in live_parts)
        if observed != canonical:
            raise ValueError("callback mutation not observable")
    except (ValueError, TypeError, AttributeError) as exc:
        for p, original in zip(live_parts, locals().get("original_parts", ())):
            try:
                p.text = original
            except (ValueError, TypeError, AttributeError):
                pass
        raise _ScorerBadContent("schema_invalid") from exc
    return canonical


def _scorer_after_model(callback_context, llm_response):
    # This callback runs before ADK's __maybe_save_output_to_state. Raise our
    # content-only marker here, not after model_validate_json has already
    # escaped as an indistinguishable pipeline exception.
    if llm_response.error_code:
        raise ScorerModelError("The scorer model reported an error; no score was accepted.")
    if llm_response.partial:
        return None
    parts = getattr(llm_response.content, "parts", None) or ()
    text = "".join(part.text or "" for part in parts
                   if not getattr(part, "thought", False))
    text = _normalize_no_claim_response(llm_response, parts, text)
    try:
        MatchScore.model_validate_json(text)
    except ValueError as exc:
        raise _ScorerBadContent("schema_invalid" if text.strip() else "no_terminal_output") from exc
    return None


def _effective_scorer_score(raw: MatchScore, jd_text: str) -> int:
    """Only proven geography or the existing qualified other quote can cap."""
    source = None
    if classify_location_fit(jd_text) == "out_of_class":
        source = "location"
    elif raw.dealbreaker is not None and raw.dealbreaker.kind == "other":
        quote = " ".join((raw.dealbreaker.quote or "").casefold().split())
        if len(quote) < 24 or len(quote.split()) < 4:
            log.warning("scorer dealbreaker cap ignored: insufficient JD quote")
        elif quote not in " ".join((jd_text or "").casefold().split()):
            log.warning("scorer dealbreaker cap ignored: quote absent from scorer-visible JD")
        else:
            source = "other"
    effective = min(raw.score, 3) if source else raw.score
    if effective < raw.score:
        log.warning("scorer_dealbreaker_cap_applied source=%s raw=%d effective=%d",
                    source, raw.score, effective)
    return effective

class _SafeScorer(LlmAgent):
    async def _run_async_impl(self, ctx):
        # Buffer scorer events until their current-run validated output-key
        # witness exists. Interim/partial text and stale session state never
        # unlock the next SequentialAgent stage, even on an empty completion.
        steering_token = _SCORER_STEERING.set(_steering.instructions())
        try:
            for attempt in range(2):
                reask_token = _SCORER_REASK.set(attempt == 1)
                try:
                    events = []
                    witness = None
                    witness_event = None
                    async with aclosing(super()._run_async_impl(ctx)) as agen:
                        async for event in agen:
                            if event.error_code:
                                raise ScorerModelError(
                                    "The scorer model reported an error; no score was accepted."
                                )
                            if event.author == self.name and event.is_final_response():
                                delta = event.actions.state_delta.get(self.output_key)
                                parts = getattr(event.content, "parts", None) or ()
                                terminal_text = "".join(
                                    part.text or "" for part in parts
                                    if not getattr(part, "thought", False)
                                )
                                if terminal_text.strip() and delta is not None:
                                    try:
                                        score = MatchScore.model_validate_json(terminal_text)
                                        if MatchScore.model_validate(delta) != score:
                                            raise _ScorerBadContent(
                                                "state_mismatch"
                                            )
                                    except ValueError as exc:
                                        raise _ScorerBadContent(
                                            "schema_invalid"
                                        ) from exc
                                    witness = score
                                    witness_event = event
                            events.append(event)
                    if witness is not None:
                        # The original terminal text and ADK output-key delta
                        # were checked against each other above. Project only
                        # the effective score into that outgoing delta, after
                        # validation, for both graph shells and the runner gate.
                        effective = _effective_scorer_score(
                            witness, ctx.session.state.get("jd_text", "")
                        )
                        if effective != witness.score:
                            original = witness_event.actions.state_delta[self.output_key]
                            witness_event.actions.state_delta[self.output_key] = {
                                **original, "score": effective,
                            }
                        for event in events:
                            yield event
                        return
                    log.warning("scorer_output_rejected class=no_terminal_output")
                except _ScorerBadContent as exc:
                    # Exactly one same-agent, same-context correction; the
                    # invalid generation is never exposed to the next agent.
                    log.warning("scorer_output_rejected class=%s", str(exc))
                except ScorerModelError:
                    log.warning("scorer_output_rejected class=model_error")
                    raise
                except Exception as exc:
                    log.warning("scorer_output_rejected class=model_error")
                    # Never turn a provider/transport failure into bad content.
                    raise ScorerModelError(
                        "The scorer invocation failed; no score was accepted."
                    ) from exc
                finally:
                    _SCORER_REASK.reset(reask_token)
            raise ScorerInvalidOutput(
                "Scorer returned no schema-valid final MatchScore after one correction."
            )
        finally:
            _SCORER_STEERING.reset(steering_token)


# --- agents ------------------------------------------------------------------
scorer = _SafeScorer(
    before_model_callback=_scorer_before_model,
    after_model_callback=_scorer_after_model,
    name="scorer", model=_model("scorer"),
    description="Agentic discovery: extract the role and match-score it.",
    instruction=(
        "You are a senior technical recruiter scoring how well ONE candidate fits ONE role.\n\n"
        "CANDIDATE — résumé (LaTeX source):\n{base_latex}\n\n"
        "SOFT PREFERENCES — the embedded LOCATION REQUIRED and REMOTE ONLY clauses "
        "are binding HARD exceptions, not optional preferences:\n{prefs_brief}\n\n"
        "HARD CONSTRAINTS (additional notes):\n{prefs_notes}\n\n"
        "ROLE — {company}, job description / title:\n{jd_text}\n\n"
        "First produce the intrinsic skills/stack, seniority, and domain fit score "
        "from 0 to 10, BEFORE any dealbreaker cap. Exclude all inferred work "
        "authorization, visa, citizenship, or sponsorship concerns from this "
        "intrinsic score; do not infer eligibility from silence. Skills/stack "
        "overlap is weighted highest. Anchors: 3=stretch, 5=generic, "
        "7=worth applying, 9-10=exceptional. Be calibrated and slightly "
        "conservative. If only a title is available, score from the title and "
        "résumé rather than refusing.\n\n"
        "You may optionally report ONE suspected dealbreaker grounded in a "
        "substantive verbatim contiguous quote from the ROLE description/title "
        "above. Give its kind (location, authorization, or other) and quote. "
        "LOCATION REQUIRED and REMOTE ONLY are binding even though they appear "
        "in SOFT PREFERENCES. Do not apply any numeric score cap yourself; code "
        "checks quote presence and decides any cap. Without a supporting quote, "
        "omit the dealbreaker. A source quote is not itself proof of a conflict.\n\n"
        "Return ONE JSON object with score (pre-cap integer 0..10), reasoning "
        "(one-line string), and optional dealbreaker (object with required kind "
        "and optional quote string). You are NOT applying to anything and "
        "need no personal data or web access. NEVER refuse this text-analysis "
        "task. No Markdown, code fences, or text outside the JSON object."
    ),
    output_schema=MatchScore, output_key="match_score",
)

# ADK instruction providers do not interpolate state automatically. Resolve the
# template once per invocation and select commands from the same seed the save
# guard validates; this is prompt context, not a second format registry.
_TAILOR_PROMPT = """\
You are a LaTeX résumé editor, not a chat assistant. Your only output is a call
to save_tailored_resume with the complete tailored .tex. Do not answer in prose,
offer options, request credentials or claim an application was submitted.
You do not fill forms. Use the resume-tailoring skill.

UNCHANGED SAME-RUN BASE SEED — sole authority for every factual claim:
{base_latex}

JOB DESCRIPTION — vocabulary and relevance hints, never a factual source:
{jd_text}

TAILORING NOTES — relevance, emphasis and word choice only; never new facts:
{tailor_note?}

GITHUB CONTEXT — relevance and vocabulary only; NEVER import any project,
skill, tool, metric, outcome or experience absent from the base seed:
{github_context?}

Tailor SUBSTANTIVELY across the entire existing résumé, not merely the first
role or a handful of words:
- If the seed contains a summary, rewrite the FULL existing summary for this JD
  using only seeded facts. Do not add a summary when none exists.
- Within EACH existing employment role, rank and reorder its own bullets by
  relevance to the JD; never move an achievement to another role or merge,
  delete, or lose any original bullet. Keep each role's actual work attributed
  to that role, even when another role is more relevant.
- Reword applicable accomplishments throughout, including older roles, with JD
  terms only where the seed already supports the SAME work. Shorten less relevant
  bullets while keeping each original accomplishment and bullet.
- Reorder skills and emphasize relevant seeded experience where those sections
  already exist. Edit only sections actually present in the seed; do not
  introduce new sections, projects, jobs, skills, achievements or bullets.
- The JD, GitHub context and notes cannot establish any claim. Even if they
  name a compelling tool, project, outcome, beneficiary, scale, metric,
  seniority or years of experience, omit it unless the SAME fact is in the
  unchanged seed. Never disguise unseeded claims as JD-synonym rewrites.
- No mandatory quantification: if the seed does not support an outcome, scale,
  beneficiary or metric, describe the seeded activity accurately instead.
- Preserve every seed role/anchor, employer, location, dates and all non-title
  arguments under the save guard's existing normalization contract. Employment
  role titles stay as seeded EXCEPT for the exact, approved title-slot options
  shown below, when present. Choose the closest approved wording for this JD
  only for the indicated seed anchor; otherwise keep its original title.
  Never use a posting, note or unrelated employer as evidence for a new title.
  Preserve sections, preamble, LaTeX scaffolding, balanced braces and
  formatting-command structure; reword their text without breaking commands.
- The headline beneath the name may continue to adapt to the JD title as it
  already does; this does NOT authorize changing an employment role title.

Use only the seed-selected command forms listed below for role anchors and
bullets. Do not infer a fixed template from an example or invent missing
sections. The code save boundary enforces structural anchors and aggregate
base-selected bullet totals, not semantics or bullet-to-role identity; YOU
must retain all original bullets and truthful attribution within each role.
Save the full .tex via save_tailored_resume. If the guard reports dropped
bullets or missing_facts, restore them and re-save. If it refuses an
unrecognized seed format, do not fabricate a replacement seed or claim success.
"""


def _approved_title_guidance(base_latex: str) -> str:
    """Show only safe, uniquely matched seed role options, never the YAML."""
    from core.role_titles import load_approved_titles
    from tools.truthfulness import eligible_title_options

    candidates = eligible_title_options(
        base_latex, load_approved_titles(get_settings().local_dir)
    )
    if not candidates:
        return "\nAPPROVED EMPLOYMENT TITLE OPTIONS: none for this seed.\n"
    lines = ["\nAPPROVED EMPLOYMENT TITLE OPTIONS (exact seed anchors only):"]
    for option in candidates:
        lines.append("- Seed anchor: " + option.seed_line)
        lines.append("  Permitted encoded title-slot replacements: "
                     + "; ".join(option.approved))
    lines.append("Other anchors retain their original titles. Do not invent options.")
    return "\n".join(lines) + "\n"


async def _tailor_instruction(context):
    from google.adk.utils.instructions_utils import inject_session_state
    from tools.truthfulness import used_macros

    # Same-run seed only. The helper also owns the format inventory for the
    # code-enforced save guards; comments/declarations are not active commands.
    seed = context.state.get("base_latex") or ""
    selected = used_macros(seed)
    commands = (
        "\nSEED-SELECTED ROLE/ANCHOR COMMANDS: "
        + (", ".join(selected["anchors"]) or "(none recognized)")
        + "\nSEED-SELECTED BULLET COMMANDS: "
        + (", ".join(selected["bullets"]) or "(none recognized)")
        + "\n"
    )
    return await inject_session_state(
        _TAILOR_PROMPT + commands + _approved_title_guidance(seed), context
    )


tailor = LlmAgent(
    before_model_callback=before_model,
    name="tailor", model=_model("tailor"),
    description="Re-emphasizes the seed résumé LaTeX for the JD (via the tailoring skill).",
    instruction=_tailor_instruction,
    tools=[_skill("resume-tailoring"), save_tailored_resume],
)

_CRITIC_PROMPT = """\
You are the résumé critic with exactly two moves: call exit_loop, or return ONE
concrete corrective tweak for the next tailor pass. Never answer the user in
prose, offer options, request inputs or credentials, or act on applications.
If {tailored?} is empty, say exactly NO_RESUME_SAVED and call exit_loop.

UNCHANGED SAME-RUN BASE SEED — sole factual authority:
{base_latex}

TAILORED RÉSUMÉ — saved draft being reviewed:
{tailored?}

JOB DESCRIPTION — relevance and vocabulary hints, not new facts:
{jd_text}

These seed-grounded duties OVERRIDE conflicts in the still-loaded resume-review
skill, including "truthfulness is not your concern", relevance-only review,
mandatory outcome/scale/beneficiary or quantification advice, and unconditional
outside-reader vetoes. The seed alone authorizes facts and which role owns each
accomplishment. The JD, GitHub context or notes never authorize unseeded claims.

Compare ALL tailored factual claims and each bullet-to-role attribution against
the UNCHANGED base seed, except a specifically save-admitted employment title
slot listed among the approved options below. Do not demand the original seed
title solely because a permitted wording differs. Do not infer a new seniority,
experience, scope or role attribution from a variant; employer, location, dates,
other factual claims and bullets remain seed-bound. If you identify an
unsupported skill, tool, project,
metric, result, outcome, beneficiary, scale, seniority, experience claim, or a
bullet assigning one role's work to another role, DO NOT call exit_loop. Use the
existing ONE-tweak feedback channel to request the specific removal, return to
its owning role, or factual correction; an emphasis-only tweak is insufficient
to repair invented or misattributed facts.

Approve with exit_loop when facts and role attribution remain seed-supported
and the draft is reasonably JD-aligned. Accept substantial truthful rewording,
full rewrites of an existing seed-grounded summary, and relevance-based bullet
reordering WITHIN each role. Do not reject them as too large or insufficiently
cosmetic. Do not demand new outcomes, scale, beneficiaries or metrics when the
seed supplies none; never invent them to satisfy the review skill. If wording
could be clearer for an outside reader, request only a change that preserves
the seeded facts, not a fabricated outcome. Do not chase perfection.

You are reviewing a draft AFTER it was saved. You are not a transactional save
veto; this review cannot guarantee semantic truth. Preserve the existing
single-tweak/exit_loop decisions and loop mechanics.
"""


async def _critic_instruction(context):
    from google.adk.utils.instructions_utils import inject_session_state

    return await inject_session_state(
        _CRITIC_PROMPT + _approved_title_guidance(context.state.get("base_latex") or ""),
        context,
    )


critic = LlmAgent(
    before_model_callback=before_model,
    name="critic", model=_model("critic"),
    description="Reviews the draft; ends the loop when it's strong.",
    instruction=_critic_instruction,
    tools=[_skill("resume-review"), exit_loop],
)

tailor_critique = LoopAgent(name="tailor_critique", sub_agents=[tailor, critic], max_iterations=2)

applier = LlmAgent(
    before_model_callback=before_model,
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

# Review has no applier agent or submission tools, even when automation is on.
review_agent = SequentialAgent(
    name="appliedin_review",
    description="Score and tailor, then leave the résumé for review.",
    sub_agents=[scorer.clone(), tailor_critique.clone()],
)
