"""A scorer must produce a current-run ADK-validated MatchScore before tailoring.

Only synthetic Test User / example-co data. The fake implements BaseLlm, so
ADK itself still builds the model requests, validates output_schema and runs
the real root/review SequentialAgent boundaries; no network or browser exists.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from google.adk.agents import BaseAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

from agent import graph as G
from agent import run as R
from core.models import Status
from core.storage.base import guard_observation_matches, guard_patch
from tools import posting_guard as pg
from tools.schema import MatchScore


JD = ("example-co seeks a synthetic platform engineer to maintain test services, "
      "design APIs, validate integration behavior, and document release gates. " * 2).strip()
SEED = r"Test User: synthetic engineer; \section{Experience} API engineering."
SYNTHETIC_STATE = {
    "pk": "example-co#synthetic-1", "company": "example-co",
    "jd_text": JD, "base_latex": SEED, "prefs_brief": "remote; backend engineering",
    "prefs_notes": "no travel", "tailor_note": "", "github_context": "",
}


def _response(text, *, partial=False):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]),
                       partial=partial)


class _OfflineModel(BaseLlm):
    scripted: list[object] = Field(default_factory=list, exclude=True)
    requests: list[object] = Field(default_factory=list, exclude=True)

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(deepcopy(llm_request))
        if not self.scripted:
            raise AssertionError("scorer exceeded its generation budget")
        response = self.scripted.pop(0)
        if isinstance(response, BaseException):
            raise response
        for item in response if isinstance(response, tuple) else (response,):
            yield item


class _StrictTracking:
    """Cloud storage dereferences Status.value; string statuses must be rejected."""

    def __init__(self, row):
        self.row = dict(row)
        self.writes = []
        self.r = None

    def get(self, pk):
        return dict(self.row)

    def get_current(self, pk):
        return self.get(pk)

    def update_posting_guard(self, pk, observed, inspected=None, *,
                             reviewed_generation=None, source_witness=False,
                             settle_refusal=False):
        current = self.get_current(pk)
        if current.get("pk") != pk or not guard_observation_matches(current, observed):
            return False
        patch = guard_patch(current, inspected,
                            reviewed_generation=reviewed_generation,
                            source_witness=source_witness,
                            settle_refusal=settle_refusal)
        if patch is None:
            return False
        self.row.update(patch)
        return True

    def set_status(self, pk, status, **kwargs):
        assert isinstance(status, Status), f"tracking received non-enum: {status!r}"
        self.writes.append((status, dict(kwargs)))
        self.row.update(status=status.value, **kwargs)

    def update_if_status(self, pk, expected_status, updates, *, expected_reader):
        """Model the tracking CAS without relaxing the scorer's real runner."""
        reader_fields = (
            "jd_read_attempts", "jd_read_retry_at", "jd_read_revision",
            "jd_text", "jd_read_prepare_only",
        )
        if set(expected_reader) - set(reader_fields):
            raise ValueError("Unexpected reader witness key")
        status = getattr(expected_status, "value", expected_status)
        if self.row.get("pk") != pk or self.row.get("status") != status:
            return False
        if any(
            (field in self.row) != (field in expected_reader)
            or (field in expected_reader
                and self.row[field] != expected_reader[field])
            for field in reader_fields
        ):
            return False
        patch = dict(updates)
        if "status" in patch:
            patch["status"] = getattr(patch["status"], "value", patch["status"])
        self.row.update(patch)
        return True


class _NextAgent(BaseAgent):
    tracking: object
    observed: list[int] = Field(default_factory=list)

    async def _run_async_impl(self, ctx):
        # This is the next agent on the actual ADK graph. If a stale score
        # advances the graph, this assertion would see it before any side effect.
        current = MatchScore.model_validate(ctx.session.state.get("match_score"))
        self.observed.append(current.score)
        self.tracking.set_status(SYNTHETIC_STATE["pk"], Status.TAILORED,
                                 resume_s3_key="resumes/synthetic-new.pdf")
        if False:
            yield  # No real tailor, approval or applier can execute.


def _material(req):
    """The constructed model request contains expanded seed/JD/preferences."""
    parts = []
    system = getattr(getattr(req, "config", None), "system_instruction", None)
    if isinstance(system, str):
        parts.append(system)
    elif system is not None:
        parts.extend(p.text or "" for p in (getattr(system, "parts", None) or ()))
    for msg in getattr(req, "contents", ()):
        parts.extend(p.text or "" for p in (getattr(msg, "parts", None) or ()))
    return "\n".join(parts)


@pytest.fixture(params=[False, True], ids=["normal", "prepare_only"])
def harness(monkeypatch, request):
    prep = request.param
    pk = SYNTHETIC_STATE["pk"]
    row = {"pk": pk, "company": "example-co", "title": "Synthetic Platform Engineer",
           "jd_url": "https://example.com/jobs/synthetic", "jd_text": JD,
           "posting_guard": pg.analyze(JD)["posting_guard"],
           "status": "found", "match_score": 9,
           "resume_s3_key": "resumes/historical.pdf"}
    tracking = _StrictTracking(row)
    stores = SimpleNamespace(tracking=tracking)
    fake = _OfflineModel(model="offline-scorer", scripted=[])
    graph = G.review_agent if prep else G.root_agent
    scorer = graph.sub_agents[0]
    next_agent = _NextAgent(name="synthetic_next", tracking=tracking)
    next_agent.parent_agent = graph
    monkeypatch.setattr(scorer, "model", fake)
    monkeypatch.setattr(graph, "sub_agents", [scorer, next_agent])
    sessions = InMemorySessionService()
    monkeypatch.setattr(R, "_session_service", lambda: sessions)
    monkeypatch.setattr(R, "_session_state", lambda row, jd: dict(SYNTHETIC_STATE))
    monkeypatch.setattr(R, "_claim", lambda pk, stores: True)
    monkeypatch.setattr(R, "_release", lambda pk, stores: None)
    monkeypatch.setattr(R, "_min_score", lambda: 7)
    monkeypatch.setattr(G._steering, "instructions", lambda: "Synthetic steering: no invented claims")
    from core import steering
    from discovery.watchlist import Preferences
    monkeypatch.setattr(steering, "read", lambda: {"content": ""})
    monkeypatch.setattr(R, "_effective_prefs", lambda company: Preferences())

    async def _jd(_):
        # The runner consumes metadata, never an untracked plain-string fetch.
        return pg.analyze(JD)

    monkeypatch.setattr(R, "_jd_meta", _jd)
    saved = []
    monkeypatch.setattr(R, "_save_output", lambda pk, row, jd, stores: saved.append(dict(row)))
    # A previous session can contain 9. Neither the runner nor the scorer may
    # treat that stored value as a current terminal validation witness.
    R._run(sessions.create_session(app_name=R._APP, user_id=R._USER,
                                  session_id=pk,
                                  state={**SYNTHETIC_STATE, "match_score": {"score": 9}}))
    return SimpleNamespace(prep=prep, pk=pk, stores=stores, fake=fake,
                           next=next_agent, saved=saved)


def _run(h):
    return R.run_job(h.pk, h.stores, prepare_only=h.prep)


@pytest.mark.parametrize("score", [2, 9])
def test_skip_before_score_gate_cannot_be_revived(harness, monkeypatch, score):
    h = harness
    gate = R._score_gate

    def skip_then_score(*args):
        h.stores.tracking.set_status(h.pk, Status.SKIPPED, skip_reason="user_skipped")
        return gate(*args)

    monkeypatch.setattr(R, "_score_gate", skip_then_score)
    h.fake.scripted = [_response(f'{{"score":{score},"reasoning":"fit"}}')]
    assert _run(h)["result"] == "conflict"
    assert h.stores.tracking.row["status"] == "skipped"
    assert h.stores.tracking.row["skip_reason"] == "user_skipped"
    assert h.stores.tracking.row["resume_s3_key"] == "resumes/historical.pdf"
    assert h.next.observed == []


@pytest.mark.parametrize("failure", [G.ScorerModelError("synthetic failure"),
                                   RuntimeError("synthetic failure"),
                                   G.ScorerInvalidOutput("synthetic invalid")])
def test_skip_before_scorer_error_preserves_intent_and_releases_claim(
        harness, monkeypatch, failure):
    h = harness
    released = []
    monkeypatch.setattr(R, "_release", lambda *args: released.append(True))

    async def fail(*args, **kwargs):
        h.stores.tracking.set_status(h.pk, Status.SKIPPED, skip_reason="user_skipped")
        raise failure

    monkeypatch.setattr(R, "_drive_preparation", fail)
    if isinstance(failure, G.ScorerInvalidOutput):
        assert _run(h)["result"] == "conflict"
    else:
        with pytest.raises(type(failure)):
            _run(h)
    assert released == [True]
    assert h.stores.tracking.row["status"] == "skipped"
    assert h.stores.tracking.row["skip_reason"] == "user_skipped"
    assert h.stores.tracking.row["resume_s3_key"] == "resumes/historical.pdf"


@pytest.mark.parametrize("content,token", [("private fake model text", "schema_invalid"),
                                        ("", "no_terminal_output")])
def test_rejected_attempt_logs_only_diagnostic_class(harness, caplog, content, token):
    h = harness
    h.fake.scripted = [_response(content), _response(content)]
    _run(h)
    messages = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("scorer_output_rejected")]
    assert messages == [f"scorer_output_rejected class={token}"] * 2
    assert "private fake model text" not in caplog.text


def test_reopen_resets_same_seed_session_before_rescoring(harness, monkeypatch):
    h = harness
    sessions = R._session_service()
    deleted = []
    delete = sessions.delete_session

    async def record_delete(**kwargs):
        deleted.append(kwargs["session_id"])
        await delete(**kwargs)

    monkeypatch.setattr(sessions, "delete_session", record_delete)
    h.stores.tracking.row["rescore_requested"] = True
    h.fake.scripted = [_response('{"score":2,"reasoning":"fit"}')]
    _run(h)
    assert deleted == [h.pk]
    assert h.stores.tracking.row["rescore_requested"] is False
    assert len(h.fake.requests) == 1


@pytest.mark.parametrize("skip", [False, True])
def test_unsent_root_fallthrough_preserves_accepted_skip(harness, monkeypatch, skip):
    h = harness
    if h.prep:
        # Exercise the unsent branch using this fixture's fully offline graph;
        # the normal root singleton is not patched by the review-shell fixture.
        monkeypatch.setattr(R, "root_agent", G.review_agent)
    h.prep = False

    async def no_tool(self, ctx):
        if skip:
            h.stores.tracking.set_status(h.pk, Status.SKIPPED, skip_reason="user_skipped")
        if False:
            yield

    monkeypatch.setattr(_NextAgent, "_run_async_impl", no_tool)
    h.fake.scripted = [_response('{"score":9,"reasoning":"fit"}')]
    result = _run(h)
    assert result["result"] == ("conflict" if skip else "failed")
    row = h.stores.tracking.row
    assert row["status"] == ("skipped" if skip else "failed")
    if skip:
        assert row["skip_reason"] == "user_skipped"
    assert row["resume_s3_key"] == "resumes/historical.pdf"
    assert len(h.fake.requests) == 1


def test_reopen_session_delete_failure_never_reuses_stale_preferences(harness, monkeypatch):
    h = harness
    sessions = R._session_service()
    reads, released = [], []
    h.stores.tracking.row["rescore_requested"] = True

    async def cannot_delete(**kwargs):
        raise RuntimeError("synthetic session deletion failure")

    async def stale_but_readable(**kwargs):
        reads.append(kwargs["session_id"])
        return SimpleNamespace(state={
            **SYNTHETIC_STATE, "prefs_brief": "synthetic old preferences",
            "posting_guard_generation": h.stores.tracking.row["posting_guard"]["generation"]})

    monkeypatch.setattr(sessions, "delete_session", cannot_delete)
    monkeypatch.setattr(sessions, "get_session", stale_but_readable)
    monkeypatch.setattr(R, "_release", lambda *args: released.append(True))
    with pytest.raises(RuntimeError, match="synthetic session deletion failure"):
        _run(h)
    assert reads == [h.pk]
    assert released == [True]
    assert h.stores.tracking.row["status"] == "error"
    assert h.fake.requests == h.next.observed == []


def test_reopen_can_reset_a_genuinely_absent_session(harness, monkeypatch):
    sessions = R._session_service()

    async def missing(**kwargs):
        raise KeyError("synthetic absent session")

    async def absent(**kwargs):
        return None

    monkeypatch.setattr(sessions, "delete_session", missing)
    monkeypatch.setattr(sessions, "get_session", absent)
    R._run(R._reset_session(harness.pk, strict=True))


@pytest.mark.parametrize("kind", ["state_mismatch", "no_terminal_output", "model_error"])
def test_event_rejections_log_one_safe_token_per_attempt(monkeypatch, caplog, kind):
    """Cover ADK state drift and empty streams, not only malformed model text."""
    from google.adk.agents import LlmAgent
    from google.adk.events import Event, EventActions

    async def events(self, ctx):
        if kind == "model_error":
            raise RuntimeError("private fake model text")
        if kind == "state_mismatch":
            yield Event(author="scorer", content=types.Content(role="model", parts=[
                types.Part(text='{"score":8,"reasoning":"private fake model text"}')]),
                actions=EventActions(state_delta={"match_score": {"score":2,"reasoning":"fit"}}))

    monkeypatch.setattr(LlmAgent, "_run_async_impl", events)
    monkeypatch.setattr(G._steering, "instructions", lambda: "")

    async def collect():
        return [event async for event in G.scorer._run_async_impl(
            SimpleNamespace(session=SimpleNamespace(state={}))) ]

    with pytest.raises(G.ScorerModelError if kind == "model_error" else G.ScorerInvalidOutput):
        R._run(collect())
    messages = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("scorer_output_rejected")]
    assert messages == [f"scorer_output_rejected class={kind}"] * (1 if kind == "model_error" else 2)
    assert "private fake model text" not in caplog.text


@pytest.mark.parametrize("failure", [TimeoutError("synthetic timeout"),
                                   RuntimeError("synthetic transport error")])
def test_model_timeout_or_exception_exits_error_and_releases_claim(harness, monkeypatch, failure):
    h = harness
    released = []
    monkeypatch.setattr(R, "_release", lambda *args: released.append(True))
    h.fake.scripted = [failure]
    with pytest.raises(G.ScorerModelError):
        _run(h)
    assert h.stores.tracking.row["status"] == "error"
    assert h.stores.tracking.row["match_score"] is None
    assert released == [True]


def _assert_same_context(h):
    assert len(h.fake.requests) == 2
    left, right = map(_material, h.fake.requests)
    for field in (SEED, JD, "remote; backend engineering", "no travel",
                  "example-co", "Synthetic steering: no invented claims"):
        assert field in left and field in right
    assert G._SCORER_FORMAT_CORRECTION not in left
    assert G._SCORER_FORMAT_CORRECTION in right
    canon = lambda text: " ".join(text.split())
    assert canon(left) == canon(right.replace(G._SCORER_FORMAT_CORRECTION, ""))
    assert h.fake.requests[0].model == h.fake.requests[1].model


@pytest.mark.parametrize("score", [7, 9])
def test_initial_valid_score_enters_next_agent_once_without_reask(harness, score):
    h = harness
    h.fake.scripted = [_response(f'{"{"}"score":{score},"reasoning":"fit"{"}"}')]
    _run(h)
    assert len(h.fake.requests) == 1
    assert h.next.observed == [score]
    assert h.stores.tracking.row["match_score"] == score
    assert h.saved[0]["match_score"] == score  # never the stale 9


def test_non_json_then_valid_keeps_original_model_and_material(harness):
    h = harness
    h.fake.scripted = [_response("I will not emit JSON"),
                       _response('{"score":8,"reasoning":"backend fit"}')]
    _run(h)
    _assert_same_context(h)
    assert h.next.observed == [8]
    assert h.stores.tracking.row["match_score"] == 8
    assert h.saved[0]["match_score"] == 8


@pytest.mark.parametrize("responses", [
    [_response("not-json"), _response("still not json")],
    [_response("not-json"), _response("  ")],
    [_response("  "), _response("not-json")],
    [LlmResponse(content=None), LlmResponse(content=None)],
    [(_response('{"score":9,"reasoning":"interim"}', partial=True),
      _response("  ")), _response("  ")],
])
def test_terminal_invalid_defers_both_graphs_without_artifacts(harness, monkeypatch, responses):
    h = harness
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(R, "_utc_now", lambda: now)
    h.stores.tracking.row.update(rescore_requested=False, jd_read_attempts=1)
    h.fake.scripted = list(responses)
    result = _run(h)
    row = h.stores.tracking.row
    assert result["result"] == "deferred"
    assert result["reason"] == "scorer_invalid_output"
    # Not terminal: back in the ordinary FOUND pool with a FUTURE scorer deadline.
    assert row["status"] == Status.FOUND.value
    assert row.get("skip_reason") != "scorer_invalid_output"
    assert datetime.fromisoformat(row["scorer_retry_at"]) == now + R._SCORER_RETRY_DELAY
    assert row["match_score"] is None
    assert row["resume_s3_key"] == "resumes/historical.pdf"
    # The posting-reader budget and the manual re-score flag are untouched.
    assert row["jd_read_attempts"] == 0  # reset at admission, never spent by the scorer
    assert row.get("jd_read_retry_at") in (None, "")
    assert row["rescore_requested"] is False
    assert h.next.observed == []
    assert h.saved == []  # no diagnostic, tailoring, approval, enqueue, submit
    assert len(h.fake.requests) == 2
    assert not R._reader_eligible(row, now=now)

@pytest.mark.parametrize("score,override,expected_next", [(2, False, []), (7, False, [7]),
                                                           (2, True, [2])])
def test_threshold_and_explicit_override(harness, score, override, expected_next):
    h = harness
    h.stores.tracking.row["score_override"] = override
    h.fake.scripted = [_response(f'{"{"}"score":{score},"reasoning":"ordinary fit"{"}"}')]
    result = _run(h)
    assert len(h.fake.requests) == 1
    assert h.next.observed == expected_next
    assert h.stores.tracking.row["match_score"] == score
    if not expected_next:
        assert result["result"] == "skipped"
        assert h.stores.tracking.row["skip_reason"] == "low_score"


def test_schema_valid_refusal_remains_error_without_reask(harness):
    h = harness
    h.fake.scripted = [_response('{"score":0,"reasoning":"As an AI, I cannot score this"}')]
    result = _run(h)
    assert result == {"result": "error", "pk": h.pk, "reason": "scorer_refused"}
    assert h.stores.tracking.row["status"] == Status.ERROR.value
    assert h.next.observed == []
    assert len(h.fake.requests) == 1


@pytest.mark.parametrize("failure", [
    RuntimeError("synthetic provider unavailable"),
    LlmResponse(error_code="SERVICE_UNAVAILABLE", error_message="synthetic provider fault"),
])
def test_provider_failures_are_real_errors_even_for_manual_reopen(harness, failure):
    h = harness
    h.fake.scripted = [failure]
    with pytest.raises(G.ScorerModelError):
        _run(h)
    assert len(h.fake.requests) == 1
    assert h.stores.tracking.row["status"] == Status.ERROR.value
    assert h.stores.tracking.row["match_score"] is None
    assert "Scorer model/request failed" in h.stores.tracking.row["error"]
    assert h.next.observed == []
    assert h.saved == []


@pytest.mark.parametrize("placeholder", [
    "", " none ", "N/A", " n/A ", "  NONE  ",
])
def test_no_claim_placeholder_canonicalizes_before_adk_state_save(
    harness, monkeypatch, placeholder,
):
    import json

    h = harness
    witnessed = []
    original = G._SafeScorer._run_async_impl

    async def capture(self, ctx):
        async for event in original(self, ctx):
            if event.author == "scorer" and event.is_final_response():
                text = "".join(p.text or "" for p in event.content.parts)
                witnessed.append((text, deepcopy(
                    event.actions.state_delta.get("match_score")
                )))
            yield event

    monkeypatch.setattr(G._SafeScorer, "_run_async_impl", capture)
    h.fake.scripted = [_response(json.dumps(
        {"score": 8, "reasoning": "synthetic fit", "dealbreaker": placeholder}
    ))]
    _run(h)
    assert len(h.fake.requests) == 1
    assert len(witnessed) == 1
    text, delta = witnessed[0]
    assert json.loads(text)["dealbreaker"] is None
    assert MatchScore.model_validate_json(text) == MatchScore.model_validate(delta)
    assert h.next.observed == [8]


@pytest.mark.parametrize("data", [
    {"score": 8, "reasoning": "fit"},
    {"score": 8, "reasoning": "fit", "dealbreaker": None},
    {"score": 8, "reasoning": "fit", "dealbreaker": {"kind": "location"}},
])
def test_valid_dealbreaker_requires_no_mutation_or_reask(harness, monkeypatch, data):
    import json

    h = harness
    original = json.dumps(data, indent=2)
    observed = []
    impl = G._SafeScorer._run_async_impl

    async def capture(self, ctx):
        async for event in impl(self, ctx):
            if event.author == "scorer" and event.is_final_response():
                observed.append("".join(p.text or "" for p in event.content.parts))
            yield event

    monkeypatch.setattr(G._SafeScorer, "_run_async_impl", capture)
    h.fake.scripted = [_response(original)]
    _run(h)
    assert observed == [original]
    assert len(h.fake.requests) == 1
    assert h.next.observed == [8]


@pytest.mark.parametrize("bad", [
    {"score": 8, "dealbreaker": "unknown"},
    {"score": 8, "dealbreaker": "APAC only"},
    {"score": 8, "dealbreaker": False},
    {"score": 8, "dealbreaker": []},
    {"score": 8, "dealbreaker": {}},
    {"score": 8, "dealbreaker": {"kind": "none"}},
    {"score": 8, "dealbreaker": {"quote": "synthetic JD quote"}},
    {"score": "8", "dealbreaker": "none"},
    {"score": True, "dealbreaker": "n/a"},
    {"score": 8.0, "dealbreaker": ""},
    {"score": 8, "reasoning": 42, "dealbreaker": "none"},
])
def test_bad_placeholder_or_other_fields_need_existing_one_reask(harness, bad):
    import json

    h = harness
    h.fake.scripted = [
        _response(json.dumps(bad)),
        _response('{"score":8,"reasoning":"corrected synthetic fit"}'),
    ]
    _run(h)
    _assert_same_context(h)
    assert h.next.observed == [8]
    assert h.stores.tracking.row["match_score"] == 8


def test_callback_mutation_failure_reasks_without_state_patch(harness, monkeypatch):
    # If callback mutation is unsupported, no ad hoc delta/state repair is safe.
    original = G._normalize_no_claim_response
    once = [True]

    def unavailable(*args):
        if once[0]:
            once[0] = False
            raise G._ScorerBadContent("schema_invalid")
        return original(*args)

    monkeypatch.setattr(G, "_normalize_no_claim_response", unavailable)
    h = harness
    h.fake.scripted = [
        _response('{"score":8,"reasoning":"fit","dealbreaker":"none"}'),
        _response('{"score":8,"reasoning":"second valid"}'),
    ]
    _run(h)
    assert len(h.fake.requests) == 2
    assert h.next.observed == [8]
    assert h.saved[0]["match_score"] == 8


NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def _found(**extra):
    return {"pk": "example-co#due", "company": "example-co", "status": "found", **extra}


def test_deferred_scorer_retry_is_eligible_only_once_due():
    due = (NOW + timedelta(minutes=30)).isoformat()
    row = _found(scorer_retry_at=due)
    assert not R._reader_eligible(row, now=NOW)
    assert not R._reader_eligible(row, now=NOW + timedelta(minutes=29, seconds=59))
    assert R._reader_eligible(row, now=NOW + timedelta(minutes=30))
    # Independent of the posting-reader deadline and budget.
    assert not R._reader_eligible(
        _found(scorer_retry_at=due, jd_read_attempts=3), now=NOW + timedelta(hours=1))
    # A corrupt deadline never authorizes an immediate retry.
    assert not R._reader_eligible(_found(scorer_retry_at="garbage"), now=NOW)
    assert not R._reader_eligible(_found(scorer_retry_at="2026-11-23T12:00:00"), now=NOW)
    assert R._reader_eligible(_found(), now=NOW)


def test_daemon_sweep_follows_the_scorer_deadline(monkeypatch):
    import daemon
    from core import flags

    clock = {"now": NOW}
    monkeypatch.setattr(R, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(flags, "skipped_companies", lambda: set())
    rows = [_found(pk="example-co#deferred",
                   scorer_retry_at=(NOW + timedelta(minutes=30)).isoformat()),
            {"pk": "example-co#skipped", "company": "example-co", "status": "skipped",
             "gate_reason": "approval", "match_score": 7}]
    queued = []
    stores = SimpleNamespace(
        tracking=SimpleNamespace(query_status=lambda status: [
            r for r in rows if r["status"] == status.value]),
        queue=SimpleNamespace(enqueue=lambda q, item: queued.append(item["pk"])),
        tailor_queue="tailor")
    daemon._sweep_found(stores)
    assert queued == []                       # not due yet
    clock["now"] = NOW + timedelta(minutes=31)
    daemon._sweep_found(stores)
    assert queued == ["example-co#deferred"]  # due: the normal lane claims it again
    # A historical SKIPPED/approval row is never swept into a new run.
    assert "example-co#skipped" not in queued


def test_second_invalid_run_defers_again_then_valid_score_proceeds(harness, monkeypatch):
    h = harness
    clock = {"now": NOW}
    monkeypatch.setattr(R, "_utc_now", lambda: clock["now"])
    h.fake.scripted = [_response("not-json"), _response("still not json")]
    assert _run(h)["result"] == "deferred"
    first = h.stores.tracking.row["scorer_retry_at"]
    # Not due: the run is refused without another generation.
    assert _run(h)["result"] == "already_done"
    assert len(h.fake.requests) == 2
    # Due, but invalid again: a LATER deadline, still FOUND, no spin.
    clock["now"] = NOW + timedelta(minutes=31)
    h.fake.scripted = [_response("not-json"), _response("still not json")]
    assert _run(h)["result"] == "deferred"
    second = h.stores.tracking.row["scorer_retry_at"]
    assert datetime.fromisoformat(second) > datetime.fromisoformat(first)
    assert h.stores.tracking.row["status"] == Status.FOUND.value
    assert h.next.observed == []
    # Due again with a valid score: ordinary score gate, deadline cleared.
    clock["now"] = NOW + timedelta(hours=2)
    h.fake.scripted = [_response('{"score":8,"reasoning":"fit"}')]
    _run(h)
    assert h.next.observed == [8]
    assert h.stores.tracking.row["match_score"] == 8
    assert not h.stores.tracking.row.get("scorer_retry_at")


@pytest.mark.parametrize("score", [7, 9])
def test_floor_score_reaches_tailoring_with_approval_metadata(harness, score):
    """The at-floor row is never held by gate_reason; it reaches the tailor stage."""
    h = harness
    if not h.prep:
        pytest.skip("AC3 is the explicit review-only (prepare_only) route")
    h.stores.tracking.row.update(
        jd_url="https://jobs.example.com/custom/synthetic-role", gate_reason="approval",
        match_score=None)
    h.fake.scripted = [_response(f'{{"score":{score},"reasoning":"fit"}}')]
    _run(h)
    row = h.stores.tracking.row
    assert h.next.observed == [score]               # the next (tailor) stage ran
    assert row["status"] == Status.TAILORED.value   # settled by it, not just TAILORING
    assert row["resume_s3_key"] == "resumes/synthetic-new.pdf"
    assert row["match_score"] == score
    assert not hasattr(h.stores, "queue")           # no queue entry can have been made


def test_below_floor_with_approval_metadata_is_not_promoted(harness):
    h = harness
    h.stores.tracking.row.update(gate_reason="approval", match_score=None)
    h.fake.scripted = [_response('{"score":6,"reasoning":"weak fit"}')]
    result = _run(h)
    assert result["result"] == "skipped"
    assert h.stores.tracking.row["skip_reason"] == "low_score"
    assert h.next.observed == []


@pytest.mark.parametrize("reason", ["user_skipped", "review_rejected", "low_score",
                                    "title_filter", "preferences"])
def test_historical_skipped_approval_rows_are_not_eligible(reason):
    row = {"pk": "example-co#old", "company": "example-co", "status": "skipped",
           "skip_reason": reason, "gate_reason": "approval", "match_score": 8}
    assert not R._reader_eligible(row, now=NOW)
