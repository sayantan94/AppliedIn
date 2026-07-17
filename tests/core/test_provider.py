"""LLM provider follows the mode: Anthropic (local) / Bedrock (cloud)."""

from core.config import Settings


def test_local_mode_uses_anthropic():
    s = Settings(mode="local")
    assert s.llm_provider == "anthropic"
    assert s.llm_model == s.anthropic_model
    assert s.litellm_model.startswith("anthropic/")


def test_cloud_mode_uses_bedrock():
    s = Settings(mode="cloud")
    assert s.llm_provider == "bedrock"
    assert s.llm_model == s.bedrock_model
    assert s.litellm_model.startswith("bedrock/")
