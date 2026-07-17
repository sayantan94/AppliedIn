"""Match scoring: stubbed model injection, defensive parse, 0-10 clamp."""

from __future__ import annotations

from tailoring.scoring import score_match

PROFILE = {"skills": ["python", "aws"], "experience": []}


def test_stubbed_model_integer_reply():
    assert score_match("Backend engineer, Python", PROFILE, model=lambda p: "8") == 8


def test_garbage_reply_defaults_to_zero():
    assert score_match("jd", PROFILE, model=lambda p: "garbage") == 0


def test_reply_with_surrounding_text_is_parsed():
    assert score_match("jd", PROFILE, model=lambda p: "I would score this 7/10.") == 7


def test_out_of_range_reply_is_clamped():
    assert score_match("jd", PROFILE, model=lambda p: "42") == 10


def test_prompt_contains_jd_and_profile():
    seen = {}

    def stub(prompt):
        seen["prompt"] = prompt
        return "5"

    score_match("Distributed systems role", PROFILE, model=stub)
    assert "Distributed systems role" in seen["prompt"]
    assert "python" in seen["prompt"]
