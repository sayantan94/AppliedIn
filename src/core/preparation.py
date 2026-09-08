"""Measured progress for manual preparation, independent of global run flags."""
from contextlib import contextmanager
from threading import Lock
from time import time
from uuid import uuid4

_LOCK = Lock()
_BATCHES: dict[str, dict] = {}


def snapshot() -> list[dict]:
    with _LOCK:
        return [dict(b) for b in _BATCHES.values()
                if b["active"] or time() - b["finished_at"] < 300]


class Batch:
    def __init__(self, rows: list[dict]):
        self.key = uuid4().hex
        with _LOCK:
            # Keep recent receipts without accumulating old runs for the daemon's lifetime.
            for key, batch in list(_BATCHES.items()):
                if not batch["active"] and time() - batch["finished_at"] >= 300:
                    del _BATCHES[key]
            _BATCHES[self.key] = {
                "id": self.key, "total": len(rows), "completed": 0, "prepared": 0,
                "skipped": 0, "failed": 0, "active": True, "finished_at": 0,
                "company": ", ".join(dict.fromkeys(r.get("company", "") for r in rows)),
                "stage": "Reading job descriptions", "current": "",
            }

    def start(self, row: dict):
        with _LOCK:
            _BATCHES[self.key].update(stage="Scoring and tailoring", current=row.get("title", ""))

    def complete(self, status: str):
        with _LOCK:
            batch = _BATCHES[self.key]
            batch["completed"] += 1
            if status == "tailored":
                batch["prepared"] += 1
            elif status == "skipped":
                batch["skipped"] += 1
            elif status in {"failed", "error"}:
                batch["failed"] += 1

    def finish(self):
        with _LOCK:
            batch = _BATCHES[self.key]
            batch.update(active=False, finished_at=time(), current="",
                         stage="Finished" if batch["completed"] == batch["total"] else "Stopped")


@contextmanager
def track(rows: list[dict]):
    batch = Batch(rows)
    try:
        yield batch
    finally:
        batch.finish()
