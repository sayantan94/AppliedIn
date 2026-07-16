"""Environment-driven settings.

Every deployable reads configuration from ``APPLIEDIN_*`` env vars set by the
CDK stack. Nothing here is hardcoded to a resource name so the same code runs
in tests (moto) and prod unchanged.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APPLIEDIN_", extra="ignore")

    # Resource names / URLs (injected by CDK)
    applications_table: str = "applications"
    answer_bank_table: str = "answer_bank"
    artifacts_bucket: str = "appliedin-artifacts"
    tailor_queue_url: str = ""
    apply_queue_url: str = ""

    # LLM provider — model is a config value, never an architecture decision.
    llm_provider: str = "bedrock"  # bedrock | muse_spark
    llm_model: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    # Guardrails
    daily_cap: int = 5
    max_attempts: int = 2

    aws_region: str = "us-east-1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
