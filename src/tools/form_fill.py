"""The ONE form-filling core every apply engine drives.

Every apply bug this pipeline has had took the same shape: an engine decided a
field's type up front, committed to one way of setting it, and when that guess
was wrong nothing was set and nobody noticed. The fix is a convergence loop that
never trusts its own writes: read the form, set every field we can answer with
`set_field` (which itself escalates option-click → typed → geocoder and re-reads
the DOM after each attempt), read again, repeat until no further progress, then
report honestly what is still unanswered.

Two engines wrap this core:
  - "scripted" passes no resolver: purely deterministic — banked owner answers
    and standard consents. Anything else becomes a gate for the owner.
  - "loop" passes a model-backed `resolve` callback: the model maps the labels
    the deterministic pass couldn't, and free text is drafted by the writer.

The safety policy is enforced HERE, in the tool layer, not in a prompt: no
placeholder text is ever typed, no protected-status affirmation is ever made on
the owner's behalf, and a sanctions question always gets the safe answer — no
matter what a resolver returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as _field

from core.logging import get_logger

from .browser_apply import (
    _READ_FORM_JS,
    _click_fields_pass,
    _consent_default,
    _emit,
    _ensure_sanctions_safe,
    _fact_for,
    _is_placeholder,
    _is_self_id_affirmation,
    _norm_txt,
    _page_gone,
    _safe_sanctions_answer,
    set_field,
)

log = get_logger(__name__)

MAX_ROUNDS = 5        # a form converges in 2-3 reads; more than this is thrashing
MAX_RESOLVER_CALLS = 2  # the model is consulted at most twice per form


@dataclass
class FillReport:
    """What actually happened to the form — every claim verified in the DOM."""

    filled: dict = _field(default_factory=dict)     # label -> value that took
    unanswered: list = _field(default_factory=list)  # required fields still empty
    refused: list = _field(default_factory=list)     # values the tool layer refused
    state: list = _field(default_factory=list)       # final form reading


async def read_form(frame, facts: dict | None = None, ats: str = "") -> list:  # noqa: ANN001
    """The form as the DOM shows it, each field carrying the owner's own banked
    answer (via ``_fact_for``) as ``owner_answer``.

    Attaching the bank's answer here — instead of making a model search a
    hundred facts for the one that matches an arbitration acknowledgement — is
    what stopped the owner being re-asked questions they had already answered.
    A known ATS (Ashby, …) gets its structure-aware extractor; anything else
    the generic reader.
    """
    fields = None
    if ats:
        try:
            from .ats import extract_fields
            fields = await extract_fields(frame, ats)
        except Exception:  # noqa: BLE001 — the skill is an upgrade, never a gate
            log.debug("ATS extractor failed; using the generic reader", exc_info=True)
    if not fields:
        try:
            fields = await frame.evaluate(_READ_FORM_JS)
        except Exception as exc:  # noqa: BLE001
            if _page_gone(exc):
                raise
            return []
    if facts:
        for f in fields:
            hit = _fact_for(f.get("label", ""), facts)
            if hit:
                f["owner_answer"] = hit[1]
    return fields


def guard_value(label: str, value: object) -> tuple[str | None, str | None]:
    """(value_to_write, refusal_note). The non-negotiables, enforced in code.

    - placeholder tokens ({{DRAFT_ESSAY_ANSWER}} and friends) are never typed;
    - a protected-status AFFIRMATION is never made for the owner (the negative
      answers pass through);
    - a sanctions question is forced to its safe option, whatever was proposed.
    """
    if _is_placeholder(value):
        return None, f"{label}: placeholder text"
    if _is_self_id_affirmation(label, value) or _is_self_id_affirmation(str(value)):
        return None, f"{label}: self-identification is the owner's to answer"
    safe = _safe_sanctions_answer(label, [str(value)])
    if safe and str(safe[0]) != str(value):
        return str(safe[0]), f"{label}: forced to the safe option {safe[0]!r}"
    return str(value), None


def _tight_match(key: str, label: str) -> bool:
    """A containment match — strict enough to fill an OPTIONAL field from."""
    k, l = _norm_txt(key), _norm_txt(label)  # noqa: E741
    return bool(k and l) and (k in l or l in k)


def _deterministic_answer(f: dict, facts: dict) -> str | None:
    """The answer this field gets WITHOUT a model, or None.

    Required fields take the owner's banked answer (word-overlap match — the
    same bar the engines already used for required fields). Optional fields only
    fill on a strict containment match, so a loose one-word overlap can never
    write into a field it doesn't answer. A required consent with no banked
    answer falls back to `_consent_default` (which refuses waivers/fees/
    arbitration without an explicit owner answer).
    """
    label = f.get("label") or ""
    ans = f.get("owner_answer")
    if ans:
        if f.get("required"):
            return str(ans)
        hit = _fact_for(label, facts or {})
        if hit and _tight_match(hit[0], label):
            return str(ans)
        return None
    if f.get("required"):
        cd = _consent_default(label, f.get("options"))
        if cd:
            return cd
    return None


def _todo(state: list) -> list:
    return [f for f in state
            if not f.get("value")
            and (f.get("label") or "")
            and f.get("type") not in ("file", "submit", "button", "hidden")]


async def fill_form(page, frame, facts: dict, *, resolve=None, ats: str = "",  # noqa: ANN001
                    pk: str = "", url: str = "", max_rounds: int = MAX_ROUNDS,
                    ) -> FillReport:
    """Run the deterministic convergence loop; return what happened.

    `resolve(fields) -> {label: value}` is optional and model-backed: it is
    handed the still-empty fields (label, type, options, required, owner_answer)
    and returns values for the ones it can source. Everything it returns goes
    through the same `guard_value` refusals and the same verified `set_field`
    writes as the deterministic pass — a resolver cannot bypass the policy.
    """
    filled: dict[str, str] = {}
    refused: list[str] = []
    tried: dict[str, set] = {}
    resolver_calls = 0

    # The sanctions guarantee runs FIRST: when the reader can't even group the
    # question, nothing downstream would answer it, and a required group would
    # go in blank.
    await _ensure_sanctions_safe(frame, pk, url)

    for round_no in range(max_rounds):
        state = await read_form(frame, facts, ats)
        if not state:
            break
        plan: dict[str, str] = {}
        for f in _todo(state):
            label = f["label"]
            if label in filled:
                continue
            v = _deterministic_answer(f, facts)
            if v is None:
                continue
            v2, why = guard_value(label, v)
            if why:
                refused.append(why)
            if v2 is None or v2 in tried.get(label, set()):
                continue
            plan[label] = v2

        if not plan and resolve is not None and resolver_calls < MAX_RESOLVER_CALLS:
            ask = [{"label": f["label"], "type": f.get("type"),
                    "options": f.get("options") or None,
                    "required": bool(f.get("required")),
                    "owner_answer": f.get("owner_answer")}
                   for f in _todo(state) if f["label"] not in filled]
            # Required first — they decide whether the form can be sent at all.
            ask.sort(key=lambda f: not f["required"])
            if ask:
                resolver_calls += 1
                try:
                    answers = await resolve(ask[:16]) or {}
                except Exception as exc:  # noqa: BLE001
                    if _page_gone(exc):
                        raise
                    log.warning("resolver failed (%s) — deterministic result stands", exc)
                    answers = {}
                labels = {f["label"] for f in ask}
                for label, v in answers.items():
                    if label not in labels:
                        continue  # a label the form doesn't have — never freelance
                    v2, why = guard_value(label, v)
                    if why:
                        refused.append(why)
                    if v2 is None or v2 in tried.get(label, set()):
                        continue
                    plan[label] = v2

        if not plan:
            break
        progress = 0
        for label, v in plan.items():
            tried.setdefault(label, set()).add(v)
            try:
                res = await set_field(frame, label, v, pk, url)
            except Exception as exc:  # noqa: BLE001
                if _page_gone(exc):
                    raise
                log.debug("set_field(%r) raised: %s", label[:40], exc)
                res = "failed"
            if res in ("set", "already"):
                filled[label] = v
                progress += 1
        if progress:
            _emit(pk, "response", agent="browser", url=url,
                  detail=f"✎ round {round_no + 1}: set {progress}/{len(plan)} field(s)")

    # Belt and braces before reporting: the sanctions answer survived every
    # re-render, and every box/text field gets its committing click.
    await _ensure_sanctions_safe(frame, pk, url)
    await _click_fields_pass(frame, pk, url)

    state = await read_form(frame, facts, ats)
    unanswered = [
        {"label": f.get("label"), "type": f.get("type"),
         "options": f.get("options") or None}
        for f in state
        if f.get("required") and not f.get("value")
        and f.get("type") not in ("file",)
        # A combobox we verifiably set commits to a hidden input and can read
        # back empty (Ashby) — trust our own verified receipt over the re-read.
        and not (f.get("combo") and filled.get(f.get("label") or ""))
    ]
    if refused:
        _emit(pk, "response", agent="browser", url=url,
              detail="🛡 refused: " + "; ".join(refused[:3]))
    return FillReport(filled=filled, unanswered=unanswered, refused=refused, state=state)
