"""LOCAL-mode backends: Redis (tracking / answer-bank / queue) + filesystem
(artifacts). Same interfaces as the cloud stores so the exact same pipeline
runs on a Mac or on AWS — only the factory (core.stores) differs.

Redis mirrors DynamoDB+SQS closely and persists (run redis with appendonly).
Artifacts (PDFs) live on disk, not in Redis. Local mode is the real product.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..ids import normalize_label
from ..models import AnswerScope, JobRecord, Status
from .base import (
    AbstractAnswerBank,
    AbstractArtifactStore,
    AbstractQueue,
    AbstractSecrets,
    AbstractTracking,
)


class FileArtifactStore(AbstractArtifactStore):
    """S3 ArtifactStore analogue, backed by a directory tree."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, prefix: str, key: str, data: bytes, content_type: str) -> str:
        full = self.root / prefix / key
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return f"{prefix}/{key}"

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def presign(self, key: str, *, expires: int = 3600) -> str:
        return (self.root / key).resolve().as_uri()


class RedisTracking(AbstractTracking):
    """DynamoDB TrackingStore analogue on Redis.

    Row JSON at ``app:{pk}``; a set per status for ``query_status``; a hash for
    the jd_hash GSI; an atomic counter for the daily cap.
    """

    def __init__(self, client: Any) -> None:
        self.r = client

    def get(self, pk: str) -> dict | None:
        raw = self.r.get(f"app:{pk}")
        return json.loads(raw) if raw else None

    def _write(self, pk: str, row: dict, prev_status: str | None) -> None:
        pipe = self.r.pipeline()
        pipe.set(f"app:{pk}", json.dumps(row, default=str))
        status = row.get("status")
        if prev_status and prev_status != status:
            pipe.srem(f"status:{prev_status}", pk)
        if status:
            pipe.sadd(f"status:{status}", pk)
        if row.get("jd_hash"):
            pipe.hset("jdhash", row["jd_hash"], pk)
        pipe.execute()

    def put_new(self, job: JobRecord, *, status: Status = Status.FOUND) -> bool:
        # SETNX gives the same "conditional create" dedup as DynamoDB's
        # attribute_not_exists(pk).
        if not self.r.set(f"app:{job.pk}", "{}", nx=True):
            return False
        from datetime import datetime, timezone
        row = {
            "pk": job.pk, "jd_hash": job.jd_hash, "status": status.value,
            "company": job.company, "job_id": job.job_id, "title": job.title,
            "jd_url": job.jd_url, "location": job.location, "ats": job.ats, "attempts": 0,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(job.pk, row, prev_status=None)
        return True

    def set_status(self, pk: str, status: Status, **attrs: Any) -> None:
        row = self.get(pk) or {"pk": pk}
        prev = row.get("status")
        row["status"] = status.value if hasattr(status, "value") else status
        row.update(attrs)
        self._write(pk, row, prev_status=prev)

    def find_by_jd_hash(self, jd_hash: str) -> str | None:
        return self.r.hget("jdhash", jd_hash)

    def query_status(self, status: Status) -> list[dict]:
        val = status.value if hasattr(status, "value") else status
        pks = self.r.smembers(f"status:{val}")
        return [row for pk in pks if (row := self.get(pk))]

    def all(self) -> list[dict]:
        rows = []
        for key in self.r.scan_iter("app:*"):
            raw = self.r.get(key)
            if raw:
                rows.append(json.loads(raw))
        return rows

    def try_increment_daily_cap(self, date_str: str, cap: int) -> bool:
        n = self.r.incr(f"cap:{date_str}")
        if n > cap:
            self.r.decr(f"cap:{date_str}")
            return False
        return True


class MarkdownAnswerBank(AbstractAnswerBank):
    """The answer bank as a human-editable Markdown file (local mode).

    Facts live in `facts.md` grouped by scope, one `- **Question**: Answer` per
    line, so you can read and edit them by hand. When you answer a gate in the
    UI, the answer is appended here as a fact and never asked again.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("# AppliedIn facts\n\n"
                                 "_Human-editable: add or fix answers; the agent reads them._\n")

    def _scope_pk(self, scope: AnswerScope, company: str | None) -> str:
        return "global" if scope is AnswerScope.GLOBAL else f"company#{(company or '').strip().lower()}"

    def _load(self) -> dict[str, list[tuple[str, str]]]:
        sections: dict[str, list[tuple[str, str]]] = {}
        cur = None
        for line in self.path.read_text().splitlines():
            if line.startswith("## "):
                title = line[3:].strip()
                cur = ("global" if title == "global"
                       else f"company#{title.split(':', 1)[1].strip().lower()}"
                       if title.startswith("company:") else None)
                if cur:
                    sections.setdefault(cur, [])
            elif cur and line.strip().startswith("- **"):
                body = line.strip()[2:]
                end = body.find("**", 2)
                if end != -1:
                    sections[cur].append((body[2:end], body[end + 2:].lstrip(": ").strip()))
        return sections

    def _write(self, data: dict[str, list[tuple[str, str]]]) -> None:
        out = ["# AppliedIn facts", "",
               "_Human-editable: add or fix answers; the agent reads them._", ""]
        for pk in ["global", *[p for p in data if p != "global"]]:
            if pk not in data:
                continue
            out.append(f"## {'global' if pk == 'global' else 'company: ' + pk.split('#', 1)[1]}")
            out += [f"- **{q}**: {a}" for q, a in data[pk]]
            out.append("")
        self.path.write_text("\n".join(out))

    def lookup(self, question: str, company: str) -> str | None:
        data, label = self._load(), normalize_label(question)
        for pk in (f"company#{company.strip().lower()}", "global"):
            for q, a in data.get(pk, []):
                if normalize_label(q) == label:
                    return a
        return None

    def put(self, question: str, answer: str, scope: AnswerScope, *,
            company: str | None = None, source: str = "local", approved_at: str = "") -> None:
        data = self._load()
        entries = data.setdefault(self._scope_pk(scope, company), [])
        label = normalize_label(question)
        for i, (q, _) in enumerate(entries):
            if normalize_label(q) == label:
                entries[i] = (question, answer)
                break
        else:
            entries.append((question, answer))
        self._write(data)

    def seed_global(self, entries: dict[str, str], *, source: str = "seed") -> None:
        for q, a in entries.items():
            self.put(q, a, AnswerScope.GLOBAL, source=source)

    def all_facts(self, company: str) -> dict[str, str]:
        data = self._load()
        facts: dict[str, str] = {}
        for pk in ("global", f"company#{company.strip().lower()}"):
            for q, a in data.get(pk, []):
                facts[q] = a  # company entries override global
        return facts


class RedisQueue(AbstractQueue):
    """SQS Queue analogue on Redis lists. ``queue_url`` is the queue name."""

    def __init__(self, client: Any) -> None:
        self.r = client

    def enqueue(self, queue_url: str, body: dict) -> str:
        return str(self.r.rpush(f"queue:{queue_url}", json.dumps(body)))

    def drain(self, queue_url: str) -> list[dict]:
        items = []
        while (raw := self.r.lpop(f"queue:{queue_url}")) is not None:
            items.append(json.loads(raw))
        return items


class FileSecrets(AbstractSecrets):
    """Secrets Manager analogue, backed by one local JSON file (git-ignored)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}")

    def get_json(self, name: str) -> dict | None:
        return json.loads(self.path.read_text() or "{}").get(name)

    def put_json(self, name: str, obj: dict) -> None:
        data = json.loads(self.path.read_text() or "{}")
        data[name] = obj
        self.path.write_text(json.dumps(data, indent=2, default=str))
