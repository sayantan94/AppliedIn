"""Environment-driven settings.

Every deployable reads configuration from ``APPLIEDIN_*`` env vars set by the
CDK stack. Nothing here is hardcoded to a resource name so the same code runs
in tests (moto) and prod unchanged.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into the environment so both APPLIEDIN_* settings and the raw
# provider keys (read by LiteLLM) are available from one file.
_OFFLINE_TEST_ROOT = (os.environ.get("APPLIEDIN_OFFLINE_TEST_ROOT")
                      or getattr(sys, "_appliedin_offline_test_root", None))
if not _OFFLINE_TEST_ROOT:
    load_dotenv()

_SETUP_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _offline_test_root():
    return getattr(sys, "_appliedin_offline_test_root", _OFFLINE_TEST_ROOT)


def _declared_worktree_root():
    # The setup file belongs to this checkout. An exported marker alone cannot
    # identify it: dotenv intentionally leaves empty/stale exports untouched.
    if _offline_test_root():
        return None
    values = dotenv_values(_SETUP_ENV_FILE, interpolate=False)
    declared = None
    if "APPLIEDIN_WORKTREE_ROOT" in values:
        value = values["APPLIEDIN_WORKTREE_ROOT"]
        if not value or Path(value).resolve() != _SETUP_ENV_FILE.parent.resolve():
            raise ValueError("Invalid worktree setup declaration; rerun Orca setup")
        declared = Path(value).resolve()
    worktree = os.environ.get("ORCA_WORKTREE_PATH")
    primary = os.environ.get("ORCA_ROOT_PATH")
    if worktree and worktree != primary:
        actual = Path(worktree).resolve()
        if declared is not None and declared != actual:
            raise ValueError("Orca context conflicts with worktree setup; rerun Orca setup")
        return actual
    return declared

# This product runs on the owner's machine and says so. Any library that reads
# this at import time gets the answer the product already gives: no phoning home.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

# Let LiteLLM silently drop sampling params a model rejects instead of 400ing.
# Reasoning models (gpt-5*, o-series) only accept temperature=1 and reject
# frequency_penalty/top_p; ADK + browser-use send those defaults. This one flag
# covers every completion() call and the ADK LiteLlm path alike.
try:
    import litellm

    litellm.drop_params = True
    # A token-per-minute limit is a queue, not a verdict: OpenAI answers "try
    # again in 59ms" and we were treating it as a failed screen, degrading to the
    # keyword filter and telling the owner that scoring and applying would fail.
    # litellm honours the retry-after it is given, so the wait is the API's, not
    # a guess of ours. Set here because it covers every completion() call and the
    # ADK path alike, the same way drop_params does.
    litellm.num_retries = 4
    # Each SDK HTTP attempt is bounded, including ADK's LiteLlm path. With
    # four LiteLLM retries and OpenAI's two SDK retries, the request-time ceiling
    # is 5 * 3 * 120s = 1800s. Retry-after/backoff and shared throttle waits are
    # additional queue time; other providers retain their adapter retry policy.
    litellm.request_timeout = 120

    # chatgpt/* only returns the completion on the stream. Without stream=True,
    # LiteLLM 1.92 raises APIConnectionError from transform_response
    # ("Unknown items in responses API response: []"), including with
    # response_format json_object. Relevance, the crawler, field mapping, the
    # writer, and ADK LiteLlm all call without stream=True.
    from .chatgpt_stream import install as _install_chatgpt_stream

    _install_chatgpt_stream()

    # And a brake shared by every caller. Retrying inside one call does not help
    # against a per-minute budget the whole process shares: four lanes each
    # retrying independently hit the same ceiling four times over. A rate limit
    # anywhere now pauses everyone, and the pause doubles while limits keep
    # arriving and decays once they stop.
    from .throttle import install as _install_throttle

    _install_throttle()
except Exception:  # litellm always present in practice; never block startup on it
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APPLIEDIN_", extra="ignore")

    # ONE product, two modes. Same code; the factory (core.stores) swaps backends:
    #   local -> Redis (tracking/queue) + filesystem (artifacts)
    #   cloud -> DynamoDB + SQS + S3
    mode: str = "local"  # local | cloud

    # Local-mode backends
    redis_url: str = Field(default_factory=lambda: (
        "redis://offline.invalid:1/0" if _offline_test_root()
        else "redis://localhost:6379/0"
    ))
    local_dir: str = Field(default_factory=lambda: (
        _offline_test_root() or ".local"
    ))
    worktree_root: str = Field(default_factory=lambda: (
        str(_declared_worktree_root() or "")
    ))

    @model_validator(mode="after")
    def explicit_worktree_storage(self):
        # Persisted by setup: later CLI shells need not retain Orca's own env.
        # dotenv does not override inherited primary settings, and --port can
        # select a reserved DB, so validate before stores or --fresh can act.
        offline = _offline_test_root()
        local = Path(self.local_dir).resolve()
        if offline:
            if not local.is_relative_to(Path(offline).resolve()):
                raise ValueError("Offline local_dir must stay inside its test temporary root")
            return self
        if self.mode != "local":
            return self
        root = _declared_worktree_root()
        if root is not None or self.worktree_root:
            from redis.connection import parse_url

            if (root is None or not self.worktree_root
                    or Path(self.worktree_root).resolve() != root):
                raise ValueError("Worktree root conflicts with Orca/setup identity; rerun Orca setup")
            if self.local_dir == ".local" or local == root or not local.is_relative_to(root):
                raise ValueError("Worktrees require an explicit APPLIEDIN_LOCAL_DIR inside "
                                 "APPLIEDIN_WORKTREE_ROOT; rerun Orca setup")
            # Use redis-py's parser: a query-string db overrides the URL path.
            if parse_url(self.redis_url).get("db", 0) in {0, 14}:
                raise ValueError("Worktrees cannot use reserved Redis databases 0 or 14; "
                                 "rerun Orca setup or choose another --port")
        return self

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
    search_model: str = "openai/gpt-6.1-sol"
    # Cloud mode reaches the same class of model through Bedrock.
    bedrock_model: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    # Structured-output model for the stages that read a page and must return
    # parseable JSON. Orchestration models are set by orchestrator_model / the
    # per-agent overrides.
    browser_model: str = "gpt-5-mini"
    # Which Claude model drives the browser. Orchestration — discovery, scoring,
    # tailoring, the writer — stays on OpenAI; this is only the hands, and the
    # hands are the part that has to read a form nobody wrote a rule for.
    chrome_model: str = "claude-sonnet-5"
    # "chrome" is the only apply engine: the owner's own Chrome, driven by a
    # `claude --chrome` subprocess (needs Claude Code + a direct Anthropic plan).
    # The earlier headless engines were deleted — a driven browser has no history
    # and no sessions, so portals challenge it and a perfectly filled form still
    # does not go out. This setting stays because it caps apply concurrency: one
    # lane, since every apply shares the single real browser.
    apply_engine: str = "chrome"
    # ASSIST: when a CAPTCHA (or a human-only step) blocks an otherwise-filled
    # form, keep the window open and wait for YOU to solve it and submit — the
    # automation just detects the real confirmation. Never solves a CAPTCHA
    # itself. Seconds to wait for the human before giving up.
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

    # #386: a separate, explicitly local and default-off funding discovery lane.
    # The SEC identity is provided in private APPLIEDIN_FUNDING_SEC_USER_AGENT,
    # never committed to config or serialized into the funding state.
    funding_enabled: bool = False
    funding_poll_hours: int = 5
    funding_sec_user_agent: str = ""
    funding_eligible_days: int = 30
    funding_daily_extraction_calls: int = 8
    funding_weekly_extraction_tokens: int = 100_000
    funding_extract_max_output_tokens: int = 1200
    funding_daily_new_companies: int = 10
    funding_daily_http: int = 120
    funding_max_http_per_pass: int = 60
    funding_max_candidates_per_pass: int = 20
    funding_max_postings_per_pass: int = 12
    funding_daily_relevance_calls: int = 12
    funding_recheck_days: int = 7
    funding_pending_days: int = 60
    funding_active_days: int = 90

    # #387 slice A: private, local, opt-in email only. No pixel/manual/ATS lane.
    outreach_enabled: bool = False
    outreach_timezone: str = "UTC"
    outreach_sender: str = ""
    outreach_sender_authorized: bool = False
    outreach_inbound_verified: bool = False
    outreach_linkedin_path: str = ""
    outreach_linkedin_fields: str = ""
    outreach_approvals_file: str = "outreach-approved-templates.json"
    outreach_resume_manifest: str = "outreach-resume.json"
    outreach_smtp_host: str = ""
    outreach_smtp_port: int = 465
    outreach_smtp_username: str = ""
    outreach_smtp_password: str = ""
    outreach_dsn_sender_addresses: str = ""
    outreach_provider_dsn_verified: bool = False
    outreach_followup_variant_id: str = ""
    outreach_followups_auto: bool = False
    outreach_followups_expanded: bool = False

    aws_region: str = "us-east-1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
