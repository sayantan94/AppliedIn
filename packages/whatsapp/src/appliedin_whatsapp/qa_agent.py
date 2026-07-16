"""Free-text Q&A over the tracking table + S3 artifacts — strictly READ-ONLY.

The agent is bound ONLY to read tools (get / query / presign). It has no
writer in its toolset, so even a fully hijacked prompt cannot change state —
all mutations go through commands/buttons and the deterministic gate flow
(HLD invariant, enforced by test_qa_agent).

Strands and the Bedrock model are loaded lazily and are injectable, so tests
never import the SDK or touch the network.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from appliedin_core.models import Status
from appliedin_core.storage.artifacts import ArtifactStore
from appliedin_core.storage.tracking import TrackingStore

SYSTEM_PROMPT = (
    "You are AppliedIn's read-only assistant. Answer questions about tracked "
    "job applications using ONLY your tools: fetch a row by pk (company#job_id), "
    "list rows by status, or presign a stored artifact (JD snapshot, resume PDF, "
    "screenshot) so it can be linked in chat. You cannot change any state; if "
    "asked to, explain that state changes require a command or button."
)


def build_tools(tracking: TrackingStore, artifacts: ArtifactStore) -> list[Callable]:
    """The agent's complete toolset. Read-only by construction — keep it that way."""

    def get_application(pk: str) -> dict:
        """Fetch one tracked application row by its pk (company#job_id)."""
        return tracking.get(pk) or {}

    def list_applications_by_status(status: str) -> list[dict]:
        """List tracked application rows in the given lifecycle status."""
        return tracking.query_status(Status(status))

    def presign_artifact(key: str) -> str:
        """Return a temporary download URL for a stored S3 artifact key."""
        return artifacts.presign(key)

    return [get_application, list_applications_by_status, presign_artifact]


def answer(
    question: str,
    *,
    tracking: TrackingStore,
    artifacts: ArtifactStore,
    model: Any = None,
    agent_factory: Callable[..., Any] | None = None,
) -> str:
    """Answer a free-text question. Model and agent factory are injectable."""
    tools = build_tools(tracking, artifacts)
    if agent_factory is None:
        from strands import Agent  # lazy: never imported in tests

        agent_factory = Agent
    if model is None:
        from appliedin_core.llm.provider import get_model  # lazy: pulls strands

        model = get_model()
    agent = agent_factory(model=model, tools=tools, system_prompt=SYSTEM_PROMPT)
    return str(agent(question))
