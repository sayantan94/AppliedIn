"""What a fresh clone gets, and that the environment can change it.

These pin the SHIPPED defaults — the experience of someone who clones the repo
and runs it — so a change to what the product does out of the box has to be
deliberate rather than incidental.
"""

from core.config import Settings


def test_shipped_defaults():
    s = Settings()
    assert s.mode == "local"                    # runs on your machine, not a cloud
    assert s.apply_engine == "scripted"         # deterministic pipeline, no LLM in the click loop
    assert s.browser_headless is False          # a real window is friendlier to bot-detection
    assert s.apply_mode == "gated"              # never applies to anything unasked
    assert s.litellm_model == "openai/gpt-5-mini"


def test_the_environment_overrides_a_default(monkeypatch):
    monkeypatch.setenv("APPLIEDIN_APPLY_ENGINE", "agent")
    monkeypatch.setenv("APPLIEDIN_BROWSER_HEADLESS", "true")
    s = Settings()
    assert s.apply_engine == "agent"
    assert s.browser_headless is True


def test_gated_is_the_default_so_nothing_applies_unasked():
    """The one default that must never drift: applying is opt-in."""
    assert Settings().apply_mode == "gated"
