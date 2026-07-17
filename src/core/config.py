"""Environment-driven settings.

Every deployable reads configuration from ``APPLIEDIN_*`` env vars set by the
CDK stack. Nothing here is hardcoded to a resource name so the same code runs
in tests (moto) and prod unchanged.
"""

from __future__ import annotations

from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into the environment so both APPLIEDIN_* settings and the raw
# ANTHROPIC_API_KEY (read by LiteLLM) are available from one file.
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APPLIEDIN_", extra="ignore")

    # ONE product, two modes. Same code; the factory (core.stores) swaps backends:
    #   local -> Redis (tracking/queue) + filesystem (artifacts)
    #   cloud -> DynamoDB + SQS + S3
    mode: str = "local"  # local | cloud

    # Local-mode backends
    redis_url: str = "redis://localhost:6379/0"
    local_dir: str = ".local"  # artifacts on disk

    # Resource names / URLs (cloud mode; injected by CDK)
    applications_table: str = "applications"
    answer_bank_table: str = "answer_bank"
    artifacts_bucket: str = "appliedin-artifacts"
    tailor_queue_url: str = "tailor"
    apply_queue_url: str = "apply"

    # LLM provider follows the mode (same as the data stores):
    #   local -> Anthropic API (ANTHROPIC_API_KEY)
    #   cloud -> Amazon Bedrock
    # Both point at the same Claude Haiku; only the transport differs.
    anthropic_model: str = "claude-haiku-4-5-20251001"
    bedrock_model: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    # browser-use (crawl + apply) drives a real browser and needs a model that
    # reliably emits structured tool actions — Haiku doesn't, so it uses Sonnet.
    # Only the browser-use calls use this; all orchestration stays on Haiku.
    browser_model: str = "claude-sonnet-4-6"

    @property
    def llm_provider(self) -> str:
        return "anthropic" if self.mode == "local" else "bedrock"

    @property
    def llm_model(self) -> str:
        return self.anthropic_model if self.mode == "local" else self.bedrock_model

    @property
    def litellm_model(self) -> str:
        """Model string for ADK/LiteLLM (``anthropic/...`` or ``bedrock/...``)."""
        return f"{self.llm_provider}/{self.llm_model}"

    # Guardrails
    daily_cap: int = 5
    max_attempts: int = 2

    # Filesystem: where bundled config (watchlist/preferences) lives at runtime.
    config_dir: str = "config"

    aws_region: str = "us-east-1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
