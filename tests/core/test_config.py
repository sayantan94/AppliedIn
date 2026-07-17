from core.config import Settings


def test_defaults():
    s = Settings()
    assert s.daily_cap == 5
    assert s.llm_provider == "bedrock"


def test_env_override(monkeypatch):
    monkeypatch.setenv("APPLIEDIN_DAILY_CAP", "7")
    assert Settings().daily_cap == 7
