"""Per-company scan evidence and cancellation, isolated across concurrent runs."""
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event


@dataclass
class Scan:
    company: str
    stage: str = "Waiting to start"
    started: float = 0
    finished: bool = False
    found: int | None = None
    relevant: int | None = None
    note: str = ""
    stopped: Event = field(default_factory=Event)


current: ContextVar[Scan | None] = ContextVar("discovery_progress", default=None)


def record(**values) -> None:
    if scan := current.get():
        for key, value in values.items():
            setattr(scan, key, value)


def cancelled() -> bool:
    scan = current.get()
    return bool(scan and scan.stopped.is_set())
