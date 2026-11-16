"""Offline scorer cap checks: quote membership, not semantic eligibility.

Test User and example-co are synthetic. Real ADK fake models exercise both
normal and prepare-only graphs; no browser, Redis, queue, or submission runs.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy

import pytest
from google.adk.sessions import InMemorySessionService

from agent import graph as G
from agent import run as R
from core.models import Status
from tools.schema import MatchScore

from tests.agent.test_scorer_invalid_output import (
    JD, SEED, SYNTHETIC_STATE, _material, _response, harness,
)

GOOD_QUOTE = "design APIs, validate integration behavior, and document release gates."
TITLE_ONLY = "Principal Remote Platform Engineer with Distributed Services Ownership"
TITLE_QUOTE = "Principal Remote Platform Engineer with Distributed Services Ownership"
GENERIC_AUTH_JD = (
    JD + " Applicants should confirm their right to work in their local region. "
    "example-co reviews applications based on skills and relevant experience."
)
AUTH_QUOTE = "Applicants should confirm their right to work in their local region."
_effective_prefs = R._effective_prefs


def _payload(score=8, *, kind=None, quote=None, include_quote=True):
    result = {"score": score, "reasoning": "synthetic engineering fit"}
    if kind is not None:
        result["dealbreaker"] = {"kind": kind}
        if include_quote:
            result["dealbreaker"]["quote"] = quote
    return json.dumps(result)


def _fresh_scorer_state(monkeypatch, jd_text, **state_changes):
    """Recreate a scorer session; the separate current-row JD is not evidence."""
    sessions = InMemorySessionService()
    state = {**SYNTHETIC_STATE, "jd_text": jd_text, **state_changes}
    monkeypatch.setattr(R, "_session_service", lambda: sessions)
    monkeypatch.setattr(R, "_session_state", lambda row, jd: dict(state))


def _run(h):
    return R.run_job(h.pk, h.stores, prepare_only=h.prep)


def test_schema_optional_quote_and_legacy_score():
    for payload in (
        {"score": 8, "reasoning": "fit"},
        {"score": 8, "reasoning": "fit", "dealbreaker": None},
        {"score": 8, "reasoning": "fit", "dealbreaker": {"kind": "location"}},
        {"score": 8, "reasoning": "fit",
         "dealbreaker": {"kind": "other", "quote": None}},
        {"score": 8, "reasoning": "fit",
         "dealbreaker": {"kind": "authorization", "quote": "   "}},
        {"score": 8, "reasoning": "fit",
         "dealbreaker": {"kind": "location", "quote": GOOD_QUOTE}},
    ):
        assert MatchScore.model_validate(payload).score == 8
    for bad in (
        {"score": 8, "dealbreaker": {}},
        {"score": 8, "dealbreaker": {"kind": "unknown"}},
        {"score": 8, "dealbreaker": {"kind": "location", "quote": 17}},
    ):
        with pytest.raises(ValueError):
            MatchScore.model_validate(bad)


def test_rendered_instruction_order_and_binding_exceptions(harness, monkeypatch):
    h = harness
    first = ("Target stack: service integration. "
             "LOCATION REQUIRED: example regions. "
             "REMOTE ONLY: on-site roles are disallowed.")
    second = "HARD: no overnight travel for Test User."
    _fresh_scorer_state(monkeypatch, JD, prefs_brief=first, prefs_notes=second)
    h.fake.scripted = [_response(_payload())]
    _run(h)
    text = _material(h.fake.requests[0])
    assert "SOFT PREFERENCES" in text
    assert "HARD CONSTRAINTS (additional notes)" in text
    assert text.index("SOFT PREFERENCES") < text.index(first)
    assert text.index(first) < text.index("HARD CONSTRAINTS (additional notes)")
    assert text.index("HARD CONSTRAINTS (additional notes)") < text.index(second)
    assert "LOCATION REQUIRED and REMOTE ONLY clauses" in text
    assert "binding HARD exceptions" in text
    assert "Exclude all inferred work authorization" in text
    assert "BEFORE any dealbreaker cap" in text
    assert "score 1-2" not in text.lower()
    assert "caps at 3" not in text.lower()
    assert "real dealbreaker caps" not in text.lower()
    assert SEED in text
    assert len(h.fake.requests) == 1


@pytest.mark.parametrize("kind", ["location", "other"])
@pytest.mark.parametrize("source,quote", [
    (JD, GOOD_QUOTE),
    (TITLE_ONLY, TITLE_QUOTE),
])
def test_verified_quote_projects_effective_score_after_raw_witness(
    harness, monkeypatch, kind, source, quote,
):
    h = harness
    _fresh_scorer_state(monkeypatch, source)
    h.stores.tracking.row["score_override"] = True
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
    h.fake.scripted = [_response(_payload(kind=kind, quote=quote.swapcase()
                                          .replace(" ", " \n  ")))]
    result = _run(h)
    assert result["result"] != "skipped"
    assert len(h.fake.requests) == 1
    assert len(witnessed) == 1
    raw_text, forwarded = witnessed[0]
    assert json.loads(raw_text)["score"] == 8  # unchanged ADK model witness
    effective = 3 if kind == "other" else 8
    assert forwarded["score"] == effective        # after validation, before yield
    assert forwarded["dealbreaker"]["kind"] == kind
    assert h.next.observed == [effective]  # next ADK agent sees projected score
    assert h.stores.tracking.row["match_score"] == effective
    assert h.saved[0]["match_score"] == effective


@pytest.mark.parametrize("kind", ["location", "other"])
def test_code_cap_reaches_ordinary_shortlist_threshold(harness, monkeypatch, kind):
    h = harness
    if kind == "location":
        _fresh_scorer_state(monkeypatch, JD + "\nRemote — Japan-only")
    h.fake.scripted = [_response(_payload(kind=kind, quote=GOOD_QUOTE))]
    result = _run(h)
    assert result["result"] == "skipped"
    assert h.stores.tracking.row["skip_reason"] == "low_score"
    assert h.stores.tracking.row["match_score"] == 3
    assert h.next.observed == []  # skip before tailoring or approvals
    assert len(h.fake.requests) == 1


def test_verbatim_location_dealbreaker_without_geo_proof_does_not_cap(harness, monkeypatch):
    # A model quote about an unspecified example city is not geographic proof.
    h = harness
    quote = "This role requires on-site work in Example City every weekday."
    _fresh_scorer_state(monkeypatch, JD + " " + quote)
    h.fake.scripted = [_response(_payload(score=9, kind="location", quote=quote))]
    result = _run(h)
    assert result["result"] != "skipped"
    assert h.stores.tracking.row["match_score"] == 9
    assert h.next.observed == [9]
    assert len(h.fake.requests) == 1


@pytest.mark.parametrize("claim", [
    {"kind": "location"},
    {"kind": "location", "quote": None},
    {"kind": "location", "quote": ""},
    {"kind": "other", "quote": "    "},
    {"kind": "location", "quote": "API"},
    {"kind": "location", "quote": "synthetic platform engineer"},  # 3 words
    {"kind": "other", "quote": "fabricated full-time engineering office location"},
])
def test_insufficient_or_false_quotes_are_non_capping(
    harness, caplog, claim,
):
    h = harness
    h.fake.scripted = [_response(json.dumps(
        {"score": 8, "reasoning": "fit", "dealbreaker": claim}
    ))]
    with caplog.at_level(logging.WARNING):
        _run(h)
    assert len(h.fake.requests) == 1  # optional missing quote is not malformed
    assert h.next.observed == [8]
    assert h.stores.tracking.row["match_score"] == 8
    assert h.saved[0]["match_score"] == 8
    warnings = [r.getMessage() for r in caplog.records
                if "scorer dealbreaker cap ignored" in r.getMessage()]
    if claim["kind"] == "other":
        assert warnings
    else:
        assert not warnings  # model location claims are not quote-capped
    assert all(GOOD_QUOTE not in message and JD not in message for message in warnings)


@pytest.mark.parametrize("location", ["runner-title", "preferences", "current-row"])
def test_quote_outside_scorer_visible_jd_never_caps(
    harness, monkeypatch, location,
):
    h = harness
    phrase = "Remote engineering collaboration across multiple example regions"
    _fresh_scorer_state(monkeypatch, "A synthetic role with distributed systems experience.")
    if location == "runner-title":
        h.stores.tracking.row["title"] = phrase  # in free-form user message
    elif location == "preferences":
        _fresh_scorer_state(monkeypatch,
                            "A synthetic role with distributed systems experience.",
                            prefs_notes=phrase)
    else:
        # The runner reads a new JD, but the reused scorer session still has
        # an old jd_text; this Issue explicitly does not refresh it.
        from tools import posting_guard as pg
        async def current_jd(_row):
            return pg.analyze(JD)
        monkeypatch.setattr(R, "_jd_meta", current_jd)
        phrase = GOOD_QUOTE
    h.fake.scripted = [_response(_payload(kind="location", quote=phrase))]
    _run(h)
    assert len(h.fake.requests) == 1
    assert h.next.observed == [8]
    assert h.stores.tracking.row["match_score"] == 8


@pytest.mark.parametrize("score,expected_next", [(8, [8]), (2, [])])
def test_authorization_claim_never_adds_code_cap(
    harness, monkeypatch, score, expected_next,
):
    h = harness
    _fresh_scorer_state(monkeypatch, GENERIC_AUTH_JD)
    from tools import posting_guard as pg
    async def generic_jd(_row):
        return pg.analyze(GENERIC_AUTH_JD)
    monkeypatch.setattr(R, "_jd_meta", generic_jd)
    assert not R._no_sponsorship(GENERIC_AUTH_JD)
    h.fake.scripted = [_response(_payload(
        score=score, kind="authorization", quote=AUTH_QUOTE
    ))]
    result = _run(h)
    assert h.stores.tracking.row["match_score"] == score
    assert h.next.observed == expected_next
    assert len(h.fake.requests) == 1
    if score == 2:
        # A low raw model fit still skips; the code has added no further cap.
        assert result["result"] == "skipped"
        assert h.stores.tracking.row["skip_reason"] == "low_score"
    else:
        assert h.saved[0]["match_score"] == 8


def test_substantive_quote_can_be_misclassified_by_model(harness, monkeypatch):
    # This is an acknowledged limitation, not semantic proof of incompatibility.
    source = JD + " Our teams collaborate across multiple international locations."
    _fresh_scorer_state(monkeypatch, source)
    h = harness
    h.fake.scripted = [_response(_payload(
        kind="other", quote="Our teams collaborate across multiple international locations."
    ))]
    _run(h)
    assert h.stores.tracking.row["match_score"] == 3


def test_lower_raw_fit_is_never_raised_by_a_cap(harness):
    h = harness
    h.fake.scripted = [_response(_payload(
        score=2, kind="location", quote=GOOD_QUOTE
    ))]
    _run(h)
    assert h.stores.tracking.row["match_score"] == 2


@pytest.mark.parametrize("private", [
    "sponsorship: required",
    "I do not require sponsorship.\nsponsorship: required",
    "Review sponsorship options.\n  - sponsorship: required",
])
def test_explicit_sponsorship_refusal_stops_before_scorer(harness, monkeypatch, private):
    h = harness
    refusal_jd = JD + " example-co will not sponsor visa applicants."
    assert R._no_sponsorship(refusal_jd)

    from tools import posting_guard as pg
    async def denied(_row):
        return pg.analyze(refusal_jd)

    monkeypatch.setattr(R, "_jd_meta", denied)
    from core import steering
    monkeypatch.setattr(steering, "read", lambda: {"content": private})
    result = _run(h)
    assert result["reason"] == "eligibility conflict: sponsorship"
    assert h.fake.requests == []  # no model invoked
    assert h.next.observed == []
    assert h.stores.tracking.row["status"] == Status.SKIPPED.value


@pytest.mark.parametrize("location,extra,private,eligible", [
    ("Remote - Toronto, Canada", "", "residence: only in canada", True),
    ("Remote - Hyderabad", "", "residence: only in india", True),
    ("Remote - New York", "", "residence: only in united states", True),
    ("Remote - Sydney", "", "residence: only in australia", True),
    ("", "\nLocation: Remote - Canada", "residence: only in canada", True),
    ("", "\nThis role is remote in Canada.", "residence: only in canada", True),
    ("Remote", "\nThis role is remote in Canada.", "residence: only in canada", True),
    ("Remote", "\nLocation: Remote - Canada", "residence: only in canada", True),
    ("Remote - Canada", "\nWork arrangement: on-site in Sydney.", "residence: only in canada", False),
    ("Remote - Canada", "\nThis role is onsite in Sydney.", "residence: only in canada", False),
    ("Remote - Canada", "\nWork location: Sydney", "residence: only in canada", False),
    ("Remote", "\nThis role is remote in Canada or Australia.", "residence: only in canada", False),
    ("Remote - Sydney", "\nThis role is remote in Canada.", "residence: only in canada", False),
    ("Remote - Canada", "\nWork arrangement: hybrid in Canada", "residence: only in canada", False),
    ("On-site - Sydney", "", "residence: only in canada", False),
    ("Remote - Sydney", "", "residence: only in canada", False),
    ("Remote", "", "residence: only in canada", False),
    ("Remote - Toronto / Sydney", "", "residence: only in canada", False),
    ("Remote - Canada", "", "residence: not resident in canada", False),
    ("Remote - Canada", "", "residence: only in canada or australia", False),
    ("Remote - Canada", "", "residence: only in canada\nresidence: only in australia", False),
    ("Remote - Canada (hybrid)", "", "residence: only in canada", False),
    ("", "\nThis role is not remote in Canada.", "residence: only in canada", False),
    ("", "\nThis role may be remote in Canada.", "residence: only in canada", False),
    ("", "\nRemote in Canada or Australia.", "residence: only in canada", False),
])
def test_remote_home_region_exception_before_tailoring(harness, monkeypatch, location, extra, private, eligible):
    from core import steering
    from tools import posting_guard as pg
    h = harness
    h.stores.tracking.row["location"] = location
    monkeypatch.setattr(steering, "read", lambda: {"content": "sponsorship: required\n" + private})
    async def read(_row):
        return pg.analyze(JD + " No visa sponsorship is available." + extra)
    monkeypatch.setattr(R, "_jd_meta", read)
    h.fake.scripted = [_response(_payload(score=9))]
    result = _run(h)
    if eligible:
        assert h.next.observed == [9]
    else:
        assert result["reason"] == "eligibility conflict: sponsorship"
        assert h.next.observed == h.fake.requests == []


@pytest.mark.parametrize("private", [
    "", "Please consider visa-friendly roles.", "sponsorship: not required",
    "not sponsorship: required", "sponsorship: maybe required",
    "sponsorship: required\nsponsorship: not required",
    "Review sponsorship options.",
])
def test_no_sponsorship_posting_preserved_without_unambiguous_private_need(harness, monkeypatch, private):
    from core import steering
    from discovery.watchlist import Preferences
    from tools import posting_guard as pg
    h = harness
    monkeypatch.setattr(steering, "read", lambda: {"content": private})
    # Tracked notes must not establish the private requirement.
    monkeypatch.setattr(R, "_effective_prefs", lambda company: Preferences(notes="sponsorship: required"))
    async def read(_row):
        return pg.analyze(JD + " No visa sponsorship is available.")
    monkeypatch.setattr(R, "_jd_meta", read)
    h.fake.scripted = [_response(_payload(score=9, kind="authorization", quote=AUTH_QUOTE))]
    result = _run(h)
    assert result.get("reason") != "eligibility conflict: sponsorship"
    assert h.next.observed == [9]


@pytest.mark.parametrize("family,private,posting,conflict", [
    ("work authorization", "work authorization: not authorized in example north",
     "Must already be authorized to work in example north.", True),
    ("work authorization", "work authorization: not authorized in example south",
     "Must already be authorized to work in example north.", False),
    ("work authorization", "work authorization: authorized only in example south",
     "Must already be authorized to work in example north.", True),
    ("work authorization", "work authorization: authorized only in example north",
     "Must already be authorized to work in example north.", False),
    ("residence", "residence: not resident in example north",
     "Must currently reside in example north.", True),
    ("residence", "residence: only in example south",
     "Must currently reside in example north.", True),
    ("residence", "residence: only in example north",
     "Must currently reside in example north.", False),
    ("residence", "", "Must currently reside in example north.", False),
    ("work authorization", "work authorization: not authorized in example north",
     "Applicants should review local right-to-work rules.", False),
    ("work authorization", "work authorization: not authorized in example north\nwork authorization: authorized only in example north",
     "Must already be authorized to work in example north.", False),
    ("work authorization", "work authorization: authorized only in example north",
     "Must already be authorized to work in example north or example south.", False),
    ("residence", "residence: only in example north and example south",
     "Must currently reside in example south.", False),
    ("residence", "residence: only in example south",
     "Must currently reside in example north unless relocating.", False),
    # Prose does not invalidate or establish a typed, runtime-only constraint.
    ("work authorization", "Review work authorization rules.\n  * work authorization: not authorized in example north",
     "Must already be authorized to work in example north.", True),
    ("residence", "Consider residence preferences.\n  • residence: only in example south",
     "Must currently reside in example north.", True),
    ("work authorization", "Review work authorization rules.",
     "Must already be authorized to work in example north.", False),
    ("residence", "Consider residence preferences.",
     "Must currently reside in example north.", False),
])
def test_private_and_posting_constraint_pair_before_any_tailor(harness, monkeypatch, family, private, posting, conflict):
    from core import steering
    from tools import posting_guard as pg
    h = harness
    monkeypatch.setattr(steering, "read", lambda: {"content": private})
    async def read(_row):
        return pg.analyze(JD + " " + posting)
    monkeypatch.setattr(R, "_jd_meta", read)
    h.stores.tracking.row["score_override"] = True
    h.fake.scripted = [_response(_payload(score=9))]
    result = _run(h)
    if conflict:
        assert result == {"result": "skipped", "pk": h.pk, "reason": "eligibility conflict: " + family}
        assert h.next.observed == h.fake.requests == []
    else:
        assert h.next.observed == [9]


@pytest.mark.parametrize("location,remote,locations,conflict", [
    ("", True, ["Example North"], False),
    ("Remote", True, ["Example North"], False),
    ("Hybrid, Example North", True, ["Example North"], True),
    ("On-site, Example North", True, [], True),
    ("United Kingdom", False, ["United States"], True),
    ("Example North", False, ["Example North"], False),
    ("Remote - United Kingdom", True, ["United States"], True),
    ("Remote - Example North", True, ["Example North"], False),
    ("Remote - Worldwide", True, ["Example North"], False),
    ("Remote - United States", True, ["California"], False),
    ("Remote - US", True, ["United States"], False),
    ("Remote - Example South", True, ["Example North"], False),
    ("France", False, ["flexible", "Canada", "Example City", "Germany"], True),
    ("Canada", False, ["flexible", "Canada", "Example City", "Germany"], False),
    ("Example City", False, ["flexible", "Canada", "Germany"], False),
    ("Paris, France", False, ["flexible", "Canada", "Germany"], False),
    ("Toronto", False, ["Germany"], True),
    ("Hyderabad, India", False, ["Germany"], True),
    ("New York, United States", False, ["Germany"], True),
    ("Sydney", False, ["Germany"], True),
    ("Toronto, Canada", False, ["Canada"], False),
    ("Toronto / New York", False, ["Germany"], False),
    ("Toronto, Example City", False, ["Germany"], False),
])
def test_typed_location_evidence_is_independent_of_model_score(harness, monkeypatch, location, remote, locations, conflict):
    from discovery.watchlist import Preferences
    h = harness
    h.stores.tracking.row.update(location=location, score_override=True)
    monkeypatch.setattr(R, "_effective_prefs", lambda co: Preferences(locations=locations, remote_only=remote))
    h.fake.scripted = [_response(_payload(score=9))]
    result = _run(h)
    assert (result.get("result") == "skipped") is conflict
    assert h.next.observed == ([] if conflict else [9])


@pytest.mark.parametrize("url", [
    "https://myworkdayjobs.com/login",
    "https://tenant.myworkdaysite.com/recruiting/test/jobs/account",
    "https://tenant.myworkdayjobs.com/candidateHome",
    "https://taleo.net/careersection/external/login.ftl",
    "https://tenant.taleo.net/careersection/2/register.ftl",
    "https://icims.com/profile", "https://tenant.icims.com/jobs/42/login",
    "https://successfactors.com/candidateprofile",
    "https://tenant.successfactors.eu/myprofile",
])
def test_account_portal_policy_precedes_throwing_reader(harness, monkeypatch, url):
    h = harness
    h.stores.tracking.row["jd_url"] = url
    async def forbidden(_row):
        pytest.fail("portal policy must precede the reader")
    monkeypatch.setattr(R, "_jd_meta", forbidden)
    assert _run(h) == {"result": "skipped", "pk": h.pk, "reason": "account portal"}
    assert h.stores.tracking.row["skip_reason"] == "account portal"
    assert h.next.observed == h.fake.requests == []


@pytest.mark.parametrize("url", [
    "https://tenant.myworkdayjobs.com/External/job/Example/REQ-42",
    "https://taleo.net/careersection/external/jobdetail.ftl?job=42",
    "https://tenant.icims.com/jobs/42/job",
    "https://successfactors.eu/career?career_job_req_id=42",
    "https://myworkdayjobs.com.example.test/login",
    "https://notmyworkdayjobs.com/login",
    "https://example.test/login", "http://myworkdayjobs.com/login",
    "https://myworkdayjobs.com/jobs/login-engineer",
])
def test_public_and_deceptive_portal_urls_keep_ordinary_pipeline(harness, url):
    h = harness
    h.stores.tracking.row["jd_url"] = url
    h.fake.scripted = [_response(_payload())]
    _run(h)
    assert h.next.observed == [8]


@pytest.mark.parametrize("overrides,conflict", [
    ({}, True),
    ({"locations": ["Example South"], "remote_only": False}, False),
])
def test_current_jd_location_uses_company_over_global_typed_constraints(harness, monkeypatch, overrides, conflict):
    from core import flags
    from discovery.watchlist import Preferences
    from tools import posting_guard as pg
    h = harness
    global_prefs = Preferences(locations=["Example North"], remote_only=True)
    monkeypatch.setattr(R, "_global_prefs", lambda: global_prefs)
    monkeypatch.setattr(R, "_effective_prefs", _effective_prefs)
    monkeypatch.setattr(flags, "company_pref", lambda company: overrides)
    h.stores.tracking.row.update(location="Unknown", score_override=True)
    async def current_jd(_row):
        return pg.analyze(JD + "\nLocation: Hybrid, Example South.")
    monkeypatch.setattr(R, "_jd_meta", current_jd)
    h.fake.scripted = [_response(_payload(score=9))]
    out = _run(h)
    assert (out["result"] == "skipped") is conflict
    assert h.next.observed == ([] if conflict else [9])
    assert global_prefs.locations == ["Example North"] and global_prefs.remote_only
    if not conflict:
        assert "Synthetic steering: no invented claims" in _material(h.fake.requests[0])


@pytest.mark.parametrize("steering,requirement,family", [
    ("work authorization: not authorized in example north",
     "Must already be authorized to work in example north", "work authorization"),
    ("residence: only in example south",
     "Must currently reside in example north", "residence"),
])
def test_punctuation_free_requirement_bullet_does_not_consume_next_line(harness, monkeypatch, steering, requirement, family):
    from tools import posting_guard as pg
    h = harness
    monkeypatch.setattr("core.steering.read", lambda: {"content": steering})
    async def source_jd(_row):
        return pg.analyze(JD + "\n- " + requirement + "\n- Experience with Python required")
    monkeypatch.setattr(R, "_jd_meta", source_jd)
    h.stores.tracking.row["score_override"] = True
    h.fake.scripted = [_response(_payload(score=9))]
    assert _run(h) == {"result": "skipped", "pk": h.pk,
                       "reason": "eligibility conflict: " + family}
    assert h.next.observed == h.fake.requests == []


@pytest.mark.parametrize("kind,expected_source", [
    (None, "location"), ("location", "location"),
    ("authorization", "location"), ("other", "location"),
])
def test_proven_out_of_class_wins_once_over_model_claim(
    harness, monkeypatch, caplog, kind, expected_source,
):
    h = harness
    jd = JD + "\nRemote — Japan-only"
    _fresh_scorer_state(monkeypatch, jd)
    h.fake.scripted = [_response(_payload(kind=kind, quote=GOOD_QUOTE))]
    with caplog.at_level(logging.WARNING):
        outcome = _run(h)
    assert outcome["result"] == "skipped"
    assert h.stores.tracking.row["match_score"] == 3
    applied = [r.getMessage() for r in caplog.records
               if "scorer_dealbreaker_cap_applied" in r.getMessage()]
    assert applied == [
        f"scorer_dealbreaker_cap_applied source={expected_source} raw=8 effective=3"
    ]
    assert all(JD not in m and "example-co" not in m and GOOD_QUOTE not in m
               for m in applied)


@pytest.mark.parametrize("posting", [
    "Remote — APAC; Office: Japan",
    "Remote; Location: US",
    "Remote worldwide; Remote US-only",
])
def test_in_class_or_unknown_ignores_location_quote(
    harness, monkeypatch, caplog, posting,
):
    h = harness
    jd = JD + "\n" + posting
    _fresh_scorer_state(monkeypatch, jd)
    h.fake.scripted = [_response(_payload(kind="location", quote=GOOD_QUOTE))]
    with caplog.at_level(logging.WARNING):
        _run(h)
    assert h.next.observed == [8]
    assert h.stores.tracking.row["match_score"] == 8
    assert not [r for r in caplog.records
                if "scorer_dealbreaker_cap_applied" in r.getMessage()]


@pytest.mark.parametrize("posting,expected", [
    ("On-site — APAC", 8),
    ("Hybrid — worldwide", 8),
    ("On-site — APAC or Japan", 8),
    ("Remote — worldwide except Japan", 8),
    ("Remote — worldwide except Vietnam; Remote worldwide", 8),
    ("Remote — worldwide except Vietnam", 3),
    ("On-site — India", 3),
    ("Hybrid — Thailand", 3),
    ("On-site — China", 3),
    ("Hybrid in South Korea", 3),
])
def test_bounded_site_and_exclusion_scopes_cap_only_proven_exclusions(
    harness, monkeypatch, caplog, posting, expected,
):
    # The scorer already admitted this row: only JD-side geographic proof
    # may reduce its intrinsic score, never a broad APAC/world worksite.
    h = harness
    _fresh_scorer_state(monkeypatch, JD + "\n" + posting)
    h.fake.scripted = [_response(_payload(score=8))]
    with caplog.at_level(logging.WARNING):
        outcome = _run(h)
    assert h.stores.tracking.row["match_score"] == expected
    assert h.next.observed == ([8] if expected == 8 else [])
    assert (outcome["result"] == "skipped") is (expected == 3)
    applied = [record.getMessage() for record in caplog.records
               if "scorer_dealbreaker_cap_applied" in record.getMessage()]
    assert applied == (["scorer_dealbreaker_cap_applied source=location raw=8 effective=3"]
                       if expected == 3 else [])


def test_other_qualified_quote_logs_one_applied_cap(harness, caplog):
    h = harness
    h.fake.scripted = [_response(_payload(kind="other", quote=GOOD_QUOTE))]
    with caplog.at_level(logging.WARNING):
        _run(h)
    applied = [r.getMessage() for r in caplog.records
               if "scorer_dealbreaker_cap_applied" in r.getMessage()]
    assert applied == ["scorer_dealbreaker_cap_applied source=other raw=8 effective=3"]


def test_nonreducing_cap_emits_no_applied_log(harness, monkeypatch, caplog):
    h = harness
    _fresh_scorer_state(monkeypatch, JD + "\nRemote — US-only")
    h.fake.scripted = [_response(_payload(score=2))]
    with caplog.at_level(logging.WARNING):
        _run(h)
    assert h.stores.tracking.row["match_score"] == 2
    assert not [r for r in caplog.records
                if "scorer_dealbreaker_cap_applied" in r.getMessage()]


def test_remote_only_onsite_pre_score_gate_prevents_any_scorer(harness, monkeypatch):
    from discovery.watchlist import Preferences
    from tools import posting_guard as pg

    h = harness
    monkeypatch.setattr(R, "_effective_prefs",
                        lambda _: Preferences(remote_only=True))
    h.stores.tracking.row["location"] = "On-site, Japan"
    async def onsite(_):
        # Reach the existing eligibility gate rather than the unreadable-JD floor.
        return pg.analyze(JD + "\nOn-site — Japan; worksite: Japan")
    monkeypatch.setattr(R, "_jd_meta", onsite)
    result = _run(h)
    assert result["result"] == "skipped"
    assert h.fake.requests == []
    assert h.next.observed == []
