"""Store factory — the one place the two modes diverge.

Everything else (pipeline, agents, discovery) takes a Stores bundle and never
knows whether it's talking to Redis+filesystem (local) or DynamoDB+SQS+S3
(cloud). Switch with APPLIEDIN_MODE=local|cloud.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Settings, get_settings
from .storage.base import (
    AbstractAnswerBank,
    AbstractArtifactStore,
    AbstractQueue,
    AbstractSecrets,
    AbstractTracking,
)


@dataclass
class Stores:
    """A mode-agnostic bundle of storage backends (all interface-typed)."""

    tracking: AbstractTracking
    answer_bank: AbstractAnswerBank
    artifacts: AbstractArtifactStore
    queue: AbstractQueue
    secrets: AbstractSecrets
    tailor_queue: str
    apply_queue: str


def make_stores(settings: Settings | None = None) -> Stores:
    s = settings or get_settings()
    if s.mode == "local":
        import redis

        from .storage.local import (
            FileArtifactStore,
            FileSecrets,
            MarkdownAnswerBank,
            RedisQueue,
            RedisTracking,
        )

        client = redis.Redis.from_url(s.redis_url, decode_responses=True)
        return Stores(
            tracking=RedisTracking(client),
            answer_bank=MarkdownAnswerBank(Path(s.local_dir) / "facts.md"),
            artifacts=FileArtifactStore(Path(s.local_dir) / "artifacts"),
            queue=RedisQueue(client),
            secrets=FileSecrets(Path(s.local_dir) / "secrets.json"),
            tailor_queue=s.tailor_queue_url,
            apply_queue=s.apply_queue_url,
        )

    from .storage.answer_bank import AnswerBank
    from .storage.artifacts import ArtifactStore
    from .storage.queue import Queue
    from .storage.secrets import SecretsClient
    from .storage.tracking import TrackingStore

    return Stores(
        tracking=TrackingStore(s.applications_table, region=s.aws_region),
        answer_bank=AnswerBank(s.answer_bank_table, region=s.aws_region),
        artifacts=ArtifactStore(s.artifacts_bucket, region=s.aws_region),
        queue=Queue(region=s.aws_region),
        secrets=SecretsClient(region=s.aws_region),
        tailor_queue=s.tailor_queue_url,
        apply_queue=s.apply_queue_url,
    )
