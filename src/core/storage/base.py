"""Storage interfaces — the contracts both modes implement.

Cloud backends (DynamoDB / S3 / SQS) and local backends (Redis / filesystem)
each subclass these ABCs, so the pipeline depends on the abstraction, not the
implementation (dependency inversion). The factory in ``core.stores`` picks the
concrete set by mode; nothing downstream knows or cares which it got.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AnswerScope, JobRecord, Status


class AbstractTracking(ABC):
    """The application tracking store (cloud: DynamoDB `applications`)."""

    @abstractmethod
    def get(self, pk: str) -> dict | None: ...

    @abstractmethod
    def put_new(self, job: JobRecord, *, status: Status = Status.FOUND) -> bool:
        """Conditionally create; return False if pk already exists (dedup)."""

    @abstractmethod
    def set_status(self, pk: str, status: Status, **attrs: object) -> None: ...

    @abstractmethod
    def find_by_jd_hash(self, jd_hash: str) -> str | None: ...

    @abstractmethod
    def query_status(self, status: Status) -> list[dict]: ...

    @abstractmethod
    def all(self) -> list[dict]:
        """Every application row (excludes internal meta rows). For the UI."""

    @abstractmethod
    def try_increment_daily_cap(self, date_str: str, cap: int) -> bool:
        """Atomically reserve one submit slot; False when the cap is reached."""


class AbstractAnswerBank(ABC):
    """The two-scope answer bank (global facts + per-company answers)."""

    @abstractmethod
    def lookup(self, question: str, company: str) -> str | None: ...

    @abstractmethod
    def put(self, question: str, answer: str, scope: AnswerScope, *,
            company: str | None = None, source: str = "", approved_at: str = "") -> None: ...

    @abstractmethod
    def seed_global(self, entries: dict[str, str], *, source: str = "seed") -> None: ...

    @abstractmethod
    def all_facts(self, company: str) -> dict[str, str]:
        """Every approved answer available to this company (global + company),
        as question→answer — handed to the browser agent so it fills from
        approved data only."""


class AbstractArtifactStore(ABC):
    """Binary artifacts: JD snapshots, résumés, screenshots, field maps."""

    @abstractmethod
    def put(self, prefix: str, key: str, data: bytes, content_type: str) -> str: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def presign(self, key: str, *, expires: int = 3600) -> str: ...


class AbstractQueue(ABC):
    """Work queue (cloud: SQS; local: Redis list)."""

    @abstractmethod
    def enqueue(self, queue_url: str, body: dict) -> str: ...


class AbstractSecrets(ABC):
    """Credential store (cloud: Secrets Manager; local: an env-keyed JSON file)."""

    @abstractmethod
    def get_json(self, name: str) -> dict | None: ...

    @abstractmethod
    def put_json(self, name: str, obj: dict) -> None: ...
