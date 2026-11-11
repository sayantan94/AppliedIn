"""LOCAL-mode backends: Redis (tracking / answer-bank / queue) + filesystem
(artifacts). Same interfaces as the cloud stores so the exact same pipeline
runs on a Mac or on AWS — only the factory (core.stores) differs.

Redis mirrors DynamoDB+SQS closely and persists (run redis with appendonly).
Artifacts (PDFs) live on disk, not in Redis. Local mode is the real product.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC
from pathlib import Path
from typing import Any

from ..ids import is_internal_pk, normalize_label
from ..models import AnswerScope, JobRecord, Status
from .base import (
    AbstractAnswerBank,
    AbstractArtifactStore,
    AbstractQueue,
    AbstractSecrets,
    AbstractTracking,
)

_SECRETS_LOCK = threading.Lock()
_FACTS_LOCK = threading.Lock()
# A line break inside one answer, as it is stored in facts.md. The file is one
# entry per line and meant to be edited by hand, and <br> is what a person
# reading markdown would expect a line break to look like.
_BR = "<br>"


class FileArtifactStore(AbstractArtifactStore):
    """S3 ArtifactStore analogue, backed by a directory tree."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        """The file for `key`, refused if it lands outside the store.

        Keys reach here from a URL (/artifact/<key>) and from job ids read off
        employers' pages. `root / "../secrets.json"` and `root / "/etc/hosts"`
        are both valid Path arithmetic, so the containment has to be checked.
        """
        base = self.root.resolve()
        full = (base / key).resolve()
        if full == base or not full.is_relative_to(base):
            raise ValueError(f"artifact key escapes the store: {key!r}")
        return full

    def put(self, prefix: str, key: str, data: bytes, content_type: str) -> str:
        full = self._path(f"{prefix}/{key}")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return f"{prefix}/{key}"

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def presign(self, key: str, *, expires: int = 3600) -> str:
        return self._path(key).as_uri()

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).is_file()
        except ValueError:
            return False

    def version(self, key: str) -> str:
        try:
            return str(int(self._path(key).stat().st_mtime))
        except (OSError, ValueError):
            return ""


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
        # The status sets are an index of JOBS. Bookkeeping rows (watermarks, run
        # markers) carry a status too, and letting them in meant any count taken
        # from the sets was wrong by however many of them existed. Keeping them out
        # here is what lets /stats count with SCARD instead of reading every row.
        is_job = not is_internal_pk(pk)
        if prev_status and prev_status != status:
            pipe.srem(f"status:{prev_status}", pk)
        if status and is_job:
            pipe.sadd(f"status:{status}", pk)
        if row.get("jd_hash"):
            pipe.hset("jdhash", row["jd_hash"], pk)
        pipe.execute()

    def put_new(self, job: JobRecord, *, status: Status = Status.FOUND) -> bool:
        # SETNX gives the same "conditional create" dedup as DynamoDB's
        # attribute_not_exists(pk).
        if not self.r.set(f"app:{job.pk}", "{}", nx=True):
            return False
        from datetime import datetime
        row = {
            "pk": job.pk, "jd_hash": job.jd_hash, "status": status.value,
            "company": job.company, "job_id": job.job_id, "title": job.title,
            "jd_url": job.jd_url, "location": job.location, "ats": job.ats, "attempts": 0,
            "discovered_at": datetime.now(UTC).isoformat(),
            # When the EMPLOYER published it, when the source said. Distinct from
            # discovered_at, which only records when we looked: on a first sweep
            # every job was discovered seconds ago and none of them are new.
            "posted_at": getattr(job, "posted_at", "") or "",
            "discovery_source": job.discovery_source,
            "apply_requested_at": job.apply_requested_at,
            "jd_text": job.jd_text if job.discovery_source == "career_ops" else "",
        }
        self._write(job.pk, row, prev_status=None)
        return True

    def set_status(self, pk: str, status: Status, **attrs: Any) -> None:
        row = self.get(pk) or {"pk": pk}
        prev = row.get("status")
        val = status.value if hasattr(status, "value") else status
        row["status"] = val
        # When the résumé was written. The board groups the approval queue by this,
        # because "tailored on Monday" is the difference between a fresh document
        # and one written against a posting that has since changed. Stamped on the
        # TRANSITION so a later edit to the row does not move the date.
        if val == "tailored" and prev != "tailored" and not row.get("tailored_at"):
            from datetime import datetime

            row["tailored_at"] = datetime.now(UTC).isoformat()
        # And when it went out. Nothing recorded this, so "how many did I send last
        # Tuesday" could not be answered at all: an applied row knew only when it
        # was DISCOVERED, which for a job found in a sweep and applied to a week
        # later is a different week.
        if val in ("applied", "applied_manual") and not row.get("applied_at"):
            from datetime import datetime

            row["applied_at"] = datetime.now(UTC).isoformat()
        row.update(attrs)
        self._write(pk, row, prev_status=prev)

    def application_notes(self) -> dict:
        return {pk: json.loads(value) for pk, value in self.r.hgetall("application:notes").items()}

    def save_application_note(self, pk: str, note: dict) -> None:
        self.r.hset("application:notes", pk, json.dumps(note))

    def find_by_jd_hash(self, jd_hash: str) -> str | None:
        return self.r.hget("jdhash", jd_hash)

    def status_counts(self) -> dict[str, int]:
        """How many jobs sit at each status, without reading a single row.

        The obvious version walked every row: scan_iter over app:* plus a GET each,
        which at 1737 rows took 34 SECONDS. The dashboard polls this every three
        seconds, so the board spent its whole life waiting on a count and every
        click felt dead. The status sets are already maintained on write, so the
        same answer is a handful of SCARDs.
        """
        counts: dict[str, int] = {}
        pipe = self.r.pipeline()
        keys = list(self.r.scan_iter("status:*"))
        for k in keys:
            pipe.scard(k)
        for k, n in zip(keys, pipe.execute(), strict=True):
            if n:
                counts[str(k).split(":", 1)[1]] = int(n)
        return counts

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
        if scope is AnswerScope.GLOBAL:
            return "global"
        return f"company#{(company or '').strip().lower()}"

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
                    answer = body[end + 2:].lstrip(": ").strip()
                    sections[cur].append((body[2:end], answer.replace(_BR, "\n")))
        return sections

    def _write(self, data: dict[str, list[tuple[str, str]]]) -> None:
        out = ["# AppliedIn facts", "",
               "_Human-editable: add or fix answers; the agent reads them._", ""]
        for pk in ["global", *[p for p in data if p != "global"]]:
            if pk not in data:
                continue
            out.append(f"## {'global' if pk == 'global' else 'company: ' + pk.split('#', 1)[1]}")
            # One entry, one line: that is how the file is read back. A drafted
            # essay with paragraphs used to be written as it came, and on the next
            # load only its first line was the answer, so a later application got
            # a truncated essay. A multi-line question did not parse at all and
            # was erased by the next write, so the same gate was asked again.
            out += [f"- **{' '.join(str(q).split())}**: "
                    f"{str(a).strip().replace(chr(13), '').replace(chr(10), _BR)}"
                    for q, a in data[pk]]
            out.append("")
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text("\n".join(out))
        os.replace(tmp, self.path)

    def lookup(self, question: str, company: str) -> str | None:
        data, label = self._load(), normalize_label(question)
        for pk in (f"company#{company.strip().lower()}", "global"):
            for q, a in data.get(pk, []):
                if normalize_label(q) == label:
                    return a
        return None

    def put(self, question: str, answer: str, scope: AnswerScope, *,
            company: str | None = None, source: str = "local", approved_at: str = "") -> None:
        # Two applications finishing together each read the file, added their
        # own answer and wrote it back; the second write dropped the first's.
        with _FACTS_LOCK:
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
            self._write({})

    def _write(self, data: dict) -> None:
        # Temp file then rename, owner-only. This file holds portal logins: a
        # crash mid-write used to leave it truncated, after which every read
        # raised, and it was created readable by every local account.
        import os

        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2, default=str))
        os.replace(tmp, self.path)

    def get_json(self, name: str) -> dict | None:
        return json.loads(self.path.read_text() or "{}").get(name)

    def put_json(self, name: str, obj: dict) -> None:
        # Read, change, write as one step: up to six apply threads share this
        # file, and two interleaved writers each wrote back a copy missing the
        # other's entry.
        with _SECRETS_LOCK:
            data = json.loads(self.path.read_text() or "{}")
            data[name] = obj
            self._write(data)
