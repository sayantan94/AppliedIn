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

# Let LiteLLM silently drop sampling params a model rejects instead of 400ing.
# Reasoning models (gpt-5*, o-series) only accept temperature=1 and reject
# frequency_penalty/top_p; ADK + browser-use send those defaults. This one flag
# covers every completion() call and the ADK LiteLlm path alike.
try:
    import litellm

    litellm.drop_params = True
except Exception:  # litellm always present in practice; never block startup on it
    pass


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

    # LLM. Every stage runs OpenAI by default — one key to get started, and one
    # model that is cheap enough to score and tailor a whole backlog. Any stage
    # can be pointed elsewhere with its own env var (see agent_model below);
    # LiteLLM handles the provider prefix, so "anthropic/claude-…" or
    # "openrouter/…" work without code changes.
    openai_model: str = "openai/gpt-5-mini"
    # Cloud mode reaches the same class of model through Bedrock.
    bedrock_model: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    # browser-use (crawl + apply) drives a real browser and needs a model that
    # reliably emits structured tool actions. Only the browser-use calls use this;
    # orchestration models are set by orchestrator_model / the per-agent overrides.
    browser_model: str = "gpt-5-mini"
    # Apply engine: "agent" = browser-use (LLM drives, using our deterministic
    # helper tools) — swap browser_model to change the driver. "scripted" = pure
    # Playwright pipeline (no LLM in the click loop).
    apply_engine: str = "scripted"  # deterministic pipeline; agent = browser-use fallback
    # Run the browser without a visible window (APPLIEDIN_BROWSER_HEADLESS=true).
    # Headed is friendlier to bot-detection; headless suits overnight/Docker runs.
    browser_headless: bool = False
    # Use REAL Google Chrome (channel "chrome") + a persistent profile instead of
    # bundled headless Chromium. A real browser with accumulated cookies triggers
    # far fewer CAPTCHAs, and you can pre-log-in to portals in this profile once.
    browser_channel: str = "chrome"
    browser_profile_dir: str = ".local/chrome-profile"
    # ASSIST: when a CAPTCHA (or a human-only step) blocks an otherwise-filled
    # form in a VISIBLE browser, keep the window open and wait for YOU to solve it
    # and submit — the automation just detects the real confirmation. Never solves
    # a CAPTCHA itself. Seconds to wait for the human before giving up.
    assist_captcha: bool = True
    assist_wait_seconds: int = 21600  # hold the human-handoff window ~6h — closing it releases

    @property
    def llm_model(self) -> str:
        """The mode's default model, already a full LiteLLM string."""
        return self.openai_model if self.mode == "local" else f"bedrock/{self.bedrock_model}"

    @property
    def litellm_model(self) -> str:
        """Base model for ADK/LiteLLM. The configured orchestrator model wins, so
        EVERY path — the agent fallback and direct users like the discovery
        crawler — follows one setting; the mode default is only the fallback."""
        return self.orchestrator_model or self.llm_model

    # Base model for ALL orchestration agents (a full LiteLLM string, e.g.
    # "openai/gpt-4.1-mini"). Empty => the mode default (litellm_model). The
    # per-agent overrides below win over this. Env: APPLIEDIN_ORCHESTRATOR_MODEL.
    orchestrator_model: str = ""

    # Per-agent model overrides — mix & match, since each component has different
    # needs. Full LiteLLM strings (e.g. "openai/gpt-4.1-mini",
    # "anthropic/claude-haiku-4-5-20251001"). Empty => the orchestrator base.
    # Env: APPLIEDIN_SCORER_MODEL / _TAILOR_MODEL / _CRITIC_MODEL / _RELEVANCE_MODEL.
    scorer_model: str = ""
    tailor_model: str = ""
    critic_model: str = ""
    relevance_model: str = ""
    # The "writer": drafts free-text application answers (why this role, essays)
    # from the résumé + GitHub + JD. Its own knob so you can spend on writing.
    writer_model: str = ""

    def agent_model(self, agent: str) -> str:
        """The LiteLLM model string for one orchestration agent: its own override,
        else the orchestrator base, else the mode default. `agent` is
        scorer | tailor | critic | relevance | writer (empty = applier / default)."""
        override = {
            "scorer": self.scorer_model, "tailor": self.tailor_model,
            "critic": self.critic_model, "relevance": self.relevance_model,
            "writer": self.writer_model,
        }.get(agent, "")
        return override or self.orchestrator_model or self.litellm_model

    # Guardrails
    max_attempts: int = 2

    # Apply mode default (runtime-overridable from the dashboard — core.flags):
    # "gated" = approve each apply; "auto" = jobs scoring ≥ auto_min_score apply
    # themselves (find-and-apply while you sleep).
    apply_mode: str = "gated"
    auto_min_score: int = 8

    # Filesystem: where bundled config (watchlist/preferences) lives at runtime.
    config_dir: str = "config"

    aws_region: str = "us-east-1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
