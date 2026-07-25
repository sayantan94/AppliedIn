"""Which model each stage runs, and how a fresh clone behaves.

The product ships OpenAI-first: one key, one model, every stage. Any stage can be
pointed elsewhere — LiteLLM takes the provider prefix — and this pins that the
override actually wins, because a silently-ignored model setting is the kind of
thing nobody notices until the bill arrives.
"""

from core.config import Settings

STAGES = ("scorer", "tailor", "critic", "writer", "relevance")


def test_a_fresh_clone_runs_openai_everywhere():
    s = Settings(mode="local")
    assert s.litellm_model == "openai/gpt-5-mini"
    for stage in STAGES:
        assert s.agent_model(stage) == "openai/gpt-5-mini", stage


def test_cloud_mode_reaches_the_model_through_bedrock():
    s = Settings(mode="cloud")
    assert s.litellm_model.startswith("bedrock/")


def test_the_orchestrator_setting_moves_every_stage():
    s = Settings(mode="local", orchestrator_model="anthropic/claude-haiku-4-5")
    assert s.litellm_model == "anthropic/claude-haiku-4-5"
    for stage in STAGES:
        assert s.agent_model(stage) == "anthropic/claude-haiku-4-5", stage


def test_a_per_stage_override_beats_the_orchestrator():
    s = Settings(mode="local",
                 orchestrator_model="openai/gpt-5-mini",
                 tailor_model="anthropic/claude-sonnet-4-6")
    assert s.agent_model("tailor") == "anthropic/claude-sonnet-4-6"
    assert s.agent_model("scorer") == "openai/gpt-5-mini"
