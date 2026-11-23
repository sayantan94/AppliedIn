"""LOCAL-mode backends: Redis (tracking / answer-bank / queue) + filesystem
(artifacts). Same interfaces as the cloud stores so the exact same pipeline
runs on a Mac or on AWS — only the factory (core.stores) differs.

Redis mirrors DynamoDB+SQS closely and persists (run redis with appendonly).
Artifacts (PDFs) live on disk, not in Redis. Local mode is the real product.
"""

from __future__ import annotations

import json
import re
from redis.exceptions import WatchError
from datetime import UTC
from pathlib import Path
from typing import Any

from ..ids import is_internal_pk, normalize_label, strip_label_decoration
from ..models import AnswerScope, JobRecord, Status
from .base import (
    AbstractAnswerBank,
    AbstractArtifactStore,
    AbstractQueue,
    AbstractSecrets,
    AbstractTracking,
    legacy_repair_row_allowed,
    tenant_pin_row_allowed,
    tenant_pin_same_witness,
    guard_observation_matches,
    guard_patch,
    canonical_repair_row_allowed, canonical_repair_same_witness,
    canonical_repair_patch, direct_gate_patch, grants_replace_patch,
)

# Full-store walk paging (RedisTracking.all).
_ALL_SCAN_COUNT = 1000
_ALL_MGET_BATCH = 500

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

    def exists(self, key: str) -> bool:
        return (self.root / key).is_file()

    def version(self, key: str) -> str:
        try:
            return str(int((self.root / key).stat().st_mtime))
        except OSError:
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
        # Build the full identity first: SET NX must never expose a temporary
        # placeholder that another preflight might read as a persisted job.
        from datetime import datetime
        from tools import posting_guard as pg

        # First-write trust cannot be inferred from a flattened/legacy JD.
        guarded = ({"text": job.jd_text, "posting_guard": job.posting_guard}
                   if pg.valid(job.jd_text, job.posting_guard)
                   else pg.projection(job.jd_text, job.posting_guard))
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
            "jd_text": guarded["text"], "posting_guard": guarded["posting_guard"],
        }
        if job.greenhouse_board:
            row["greenhouse_board"] = job.greenhouse_board
        if not self.r.set(f"app:{job.pk}", json.dumps(row, default=str), nx=True):
            return False
        # Only indexes follow the atomic first row. Never write the row again:
        # a concurrent update after SET NX must not be overwritten by indexing.
        pipe = self.r.pipeline()
        if not is_internal_pk(job.pk):
            pipe.sadd(f"status:{status.value}", job.pk)
        if row.get("jd_hash"):
            pipe.hset("jdhash", row["jd_hash"], job.pk)
        pipe.execute()
        return True

    def set_status(self, pk: str, status: Status, **attrs: Any) -> None:
        """Merge onto the WATCHed row and reject unbound posting-guard writes.

        Status changes preserve concurrent tenant pins, while guard fields may
        only change through their generation-bound compare-and-update path.
        """
        from datetime import datetime, timezone

        key = f"app:{pk}"
        if set(attrs) & {"posting_guard", "reviewed_guard_generation", "jd_text"}:
            raise ValueError("Use update_posting_guard for bound JD/guard changes")
        val = getattr(status, "value", status)
        for _ in range(16):
            try:
                with self.r.pipeline() as pipe:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    # Preserve the pre-guard status-only upsert for legacy
                    # bookkeeping rows. A missing row has no JD or guard to
                    # certify; protected fields are rejected above.
                    row = json.loads(raw) if raw is not None else {"pk": pk}
                    prev = row.get("status")
                    row["status"] = val
                    if val == "tailored" and prev != "tailored" and not row.get("tailored_at"):
                        row["tailored_at"] = datetime.now(timezone.utc).isoformat()
                    if val in ("applied", "applied_manual") and not row.get("applied_at"):
                        row["applied_at"] = datetime.now(timezone.utc).isoformat()
                    row.update(attrs)
                    pipe.multi()
                    pipe.set(key, json.dumps(row, default=str))
                    if prev and prev != row.get("status"):
                        pipe.srem(f"status:{prev}", pk)
                    if row.get("status") and not is_internal_pk(pk):
                        pipe.sadd(f"status:{row['status']}", pk)
                    if row.get("jd_hash"):
                        pipe.hset("jdhash", row["jd_hash"], pk)
                    pipe.execute()
                    return
            except WatchError:
                continue
        raise RuntimeError("Tracking status update exhausted Redis WATCH contention")

    def get_current(self, pk: str) -> dict | None:
        return self.get(pk)

    def update_posting_guard(
        self, pk: str, observed: dict, inspected: dict | None = None, *,
        reviewed_generation: str | None = None, source_witness: bool = False,
        settle_refusal: bool = False,
    ) -> bool:
        key, hold = f"app:{pk}", f"submit_hold:{pk}"
        for _ in range(16):
            try:
                with self.r.pipeline() as pipe:
                    pipe.watch(key, hold)
                    raw = pipe.get(key)
                    if not raw:
                        pipe.unwatch()
                        return False
                    current = json.loads(raw)
                    if not guard_observation_matches(current, observed):
                        pipe.unwatch()
                        return False
                    # A separate durable hold takes priority over review.
                    if (reviewed_generation is not None or settle_refusal) and pipe.get(hold) is not None:
                        pipe.unwatch()
                        return False
                    patch = guard_patch(current, inspected,
                                        reviewed_generation=reviewed_generation,
                                        source_witness=source_witness,
                                        settle_refusal=settle_refusal)
                    if patch is None:
                        pipe.unwatch()
                        return False
                    prev_status = current.get("status")
                    current.update(patch)
                    pipe.multi()
                    pipe.set(key, json.dumps(current, default=str))
                    if current.get("status") != prev_status and not is_internal_pk(pk):
                        if prev_status:
                            pipe.srem(f"status:{prev_status}", pk)
                        pipe.sadd(f"status:{current['status']}", pk)
                    pipe.execute()
                    return True
            except WatchError:
                continue
        raise RuntimeError("Concurrent posting-guard update did not settle")

    def update_if_status(self, pk: str, expected_status: Status | str,
                         updates: dict, *, expected_reader: dict) -> bool:
        """Atomic current-row partial merge with exact status/reader witness.

        WATCH catches intervening writes before the transaction executes; a
        same-status reader generation mismatch is a conflict, not contention.
        """
        if set(updates) & {"jd_text", "posting_guard", "reviewed_guard_generation"}:
            raise ValueError("Use update_posting_guard for bound JD/guard changes")
        fixed = ("jd_read_attempts", "jd_read_retry_at", "jd_read_revision",
                 "jd_text", "jd_read_prepare_only")
        if set(expected_reader) - set(fixed):
            raise ValueError("Unexpected reader witness key")
        wanted_status = getattr(expected_status, "value", expected_status)
        key = f"app:{pk}"
        for _ in range(16):
            try:
                with self.r.pipeline() as pipe:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if raw is None:
                        pipe.unwatch()
                        return False
                    row = json.loads(raw)
                    if row.get("status") != wanted_status or any(
                            (field in row) != (field in expected_reader)
                            or (field in expected_reader
                                and row[field] != expected_reader[field])
                            for field in fixed):
                        pipe.unwatch()
                        return False
                    previous = row.get("status")
                    patch = {k: getattr(v, "value", v) if k == "status" else v
                             for k, v in updates.items()}
                    row.update(patch)
                    status = row.get("status")
                    if status == "tailored" and previous != "tailored" and not row.get("tailored_at"):
                        from datetime import datetime
                        row["tailored_at"] = datetime.now(UTC).isoformat()
                    pipe.multi()
                    pipe.set(key, json.dumps(row, default=str))
                    if not is_internal_pk(pk):
                        if previous and previous != status:
                            pipe.srem(f"status:{previous}", pk)
                        if status:
                            pipe.sadd(f"status:{status}", pk)
                    if row.get("jd_hash"):
                        pipe.hset("jdhash", row["jd_hash"], pk)
                    pipe.execute()
                    return True
            except WatchError:
                # Keep the caller's ORIGINAL expected status and witness.
                continue
        raise RuntimeError("Tracking row could not be updated after WATCH contention")

    def replace_grants_if_unchanged(self, pk: str, expected: dict, new: dict) -> bool:
        """WATCH the row and its durable hold; rewrite only the `new` grant keys."""
        key, hold = f"app:{pk}", f"submit_hold:{pk}"
        for _ in range(16):
            try:
                with self.r.pipeline() as pipe:
                    pipe.watch(key, hold)
                    raw = pipe.get(key)
                    if raw is None or pipe.get(hold) is not None:
                        pipe.unwatch()
                        return False
                    row = json.loads(raw)
                    merged = grants_replace_patch(row, expected, new)
                    if merged is None:
                        pipe.unwatch()
                        return False
                    row["human_approved_answers"] = merged
                    pipe.multi()
                    pipe.set(key, json.dumps(row, default=str))
                    pipe.execute()
                    return True
            except WatchError:
                continue
        raise RuntimeError("Grant replacement could not settle after WATCH contention")

    def consume_direct_form_gate(
        self, pk: str, *, expected_pending: dict, disposition: dict,
        grant: tuple[str, dict] | None = None,
        skip: tuple[str, dict] | None = None,
    ) -> bool:
        """WATCH the row and its durable hold; spend the gate in one MULTI.

        A concurrent reply that commits first changes the row, so the loser's
        WATCH fires and its retry sees a cleared gate and returns False.
        """
        key, hold = f"app:{pk}", f"submit_hold:{pk}"
        for _ in range(16):
            try:
                with self.r.pipeline() as pipe:
                    pipe.watch(key, hold)
                    raw = pipe.get(key)
                    if raw is None or pipe.get(hold) is not None:
                        pipe.unwatch()
                        return False
                    row = json.loads(raw)
                    patch = direct_gate_patch(row, expected_pending, disposition,
                                              grant, skip)
                    if patch is None:
                        pipe.unwatch()
                        return False
                    previous = row.get("status")
                    row.update(patch)
                    if row["status"] == "tailored" and not row.get("tailored_at"):
                        from datetime import datetime
                        row["tailored_at"] = datetime.now(UTC).isoformat()
                    pipe.multi()
                    pipe.set(key, json.dumps(row, default=str))
                    if not is_internal_pk(pk):
                        if previous and previous != row["status"]:
                            pipe.srem(f"status:{previous}", pk)
                        pipe.sadd(f"status:{row['status']}", pk)
                    pipe.execute()
                    return True
            except WatchError:
                continue
        raise RuntimeError("Direct owner gate could not be consumed after WATCH contention")

    def repair_legacy_jd_url(
        self, pk: str, company: str, job_id: str, old_url: str,
        new_url: str, *, expected_status: Status | str, expected_reader: dict,
    ) -> bool:
        """WATCH both the row and the independent durable submit hold.

        Re-read and merge the current row under WATCH; a writer changing an
        unrelated field is retained on retry, but the caller's original
        URL/status/reader witness is never rebased after contention.
        """
        key, hold_key = f"app:{pk}", f"submit_hold:{pk}"
        for _ in range(16):
            try:
                with self.r.pipeline() as pipe:
                    pipe.watch(key, hold_key)
                    raw = pipe.get(key)
                    # Even an empty/corrupt hold value is not evidence of absence.
                    hold = pipe.get(hold_key)
                    if raw is None or hold is not None:
                        pipe.unwatch()
                        return False
                    row = json.loads(raw)
                    if not legacy_repair_row_allowed(
                            row, pk, company, job_id, old_url,
                            expected_status, expected_reader):
                        pipe.unwatch()
                        return False
                    previous = row["status"]
                    row["jd_url"] = new_url
                    row["status"] = Status.FOUND.value
                    row["jd_read_attempts"] = 0
                    row["jd_read_revision"] = int(row.get("jd_read_revision", 0)) + 1
                    if previous == Status.ERROR.value:
                        row.pop("fail_kind")  # guaranteed exhausted by the guard
                    pipe.multi()
                    pipe.set(key, json.dumps(row, default=str))
                    if not is_internal_pk(pk):
                        if previous != Status.FOUND.value:
                            pipe.srem(f"status:{previous}", pk)
                        pipe.sadd("status:found", pk)
                    pipe.execute()
                    return True
            except WatchError:
                # Retry against current data, never a new expected witness.
                continue
        raise RuntimeError("Legacy URL repair exhausted Redis WATCH contention")

    def replace_noncanonical_jd_url(
        self, pk: str, new_url: str, *, expected_row: dict,
    ) -> bool:
        """Watch original job and independent durable hold; never rebase proof."""
        if not canonical_repair_row_allowed(expected_row, pk):
            return False
        key, hold_key = f"app:{pk}", f"submit_hold:{pk}"
        for _ in range(16):
            try:
                with self.r.pipeline() as pipe:
                    pipe.watch(key, hold_key)
                    raw, hold = pipe.get(key), pipe.get(hold_key)
                    if raw is None or hold is not None:
                        pipe.unwatch()
                        return False
                    row = json.loads(raw)
                    if (not canonical_repair_same_witness(row, expected_row)
                            or not canonical_repair_row_allowed(row, pk)):
                        pipe.unwatch()
                        return False
                    patch, removes = canonical_repair_patch(row, new_url)
                    previous = row["status"]
                    row.update(patch)
                    for field in removes:
                        row.pop(field, None)
                    pipe.multi()
                    pipe.set(key, json.dumps(row, default=str))
                    if previous != "found" and not is_internal_pk(pk):
                        pipe.srem(f"status:{previous}", pk)
                        pipe.sadd("status:found", pk)
                    pipe.execute()
                    return True
            except WatchError:
                continue
        raise RuntimeError("Canonical URL repair exhausted Redis WATCH contention")

    def add_missing_greenhouse_pin(
        self, pk: str, board: str, *, expected_row: dict,
    ) -> bool:
        """One exact original witness, row+independent hold WATCH, no row reset."""
        if not tenant_pin_row_allowed(expected_row, pk, board):
            return False
        key, hold_key = f"app:{pk}", f"submit_hold:{pk}"
        for _ in range(16):
            try:
                with self.r.pipeline() as pipe:
                    pipe.watch(key, hold_key)
                    raw = pipe.get(key)
                    hold = pipe.get(hold_key)
                    if raw is None or hold is not None:
                        pipe.unwatch()
                        return False
                    current = json.loads(raw)
                    if (not tenant_pin_same_witness(current, expected_row)
                            or not tenant_pin_row_allowed(current, pk, board)):
                        pipe.unwatch()
                        return False
                    current["greenhouse_board"] = board
                    pipe.multi()
                    # Preserve status, every history field and all index keys.
                    pipe.set(key, json.dumps(current, default=str))
                    pipe.execute()
                    return True
            except WatchError:
                # Never rebase the original protected witness after contention.
                continue
        raise RuntimeError("Greenhouse pin exhausted Redis WATCH contention")

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
        for k, n in zip(keys, pipe.execute()):
            if n:
                counts[str(k).split(":", 1)[1]] = int(n)
        return counts

    def query_status(self, status: Status) -> list[dict]:
        val = status.value if hasattr(status, "value") else status
        pks = self.r.smembers(f"status:{val}")
        return [row for pk in pks if (row := self.get(pk))]

    def all(self) -> list[dict]:
        # A full walk runs on many request paths at once (board list, apply-role
        # duplicate checks, sweeps). One GET per row with SCAN's default page of
        # 10 made each walk ~16k round trips over ~15k rows, enough to saturate
        # the daemon's interpreter. Page keys widely and read them in batches.
        rows = []
        keys = list(self.r.scan_iter("app:*", count=_ALL_SCAN_COUNT))
        for start in range(0, len(keys), _ALL_MGET_BATCH):
            for raw in self.r.mget(keys[start:start + _ALL_MGET_BATCH]):
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
            elif cur and line.lstrip().startswith("- **"):
                body = line.lstrip()[2:]
                # The FIRST unescaped **: closes the question. The trailing
                # star in an old raw key (***:) belongs to that key; a **:
                # later in the answer is not another question delimiter.
                end = body.find("**:", 2)
                if end != -1:
                    escaped = body[2:end]
                    question = re.sub(
                        r"\*\*(\\+):",
                        lambda match: "**" + "\\" * (len(match.group(1)) - 1) + ":",
                        escaped,
                    )
                    # Only the single separator space belongs to the format.
                    # Leading spaces and literal stars in approved values survive.
                    answer = body[end + 3:]
                    sections[cur].append((
                        question, answer[1:] if answer.startswith(" ") else answer,
                    ))
        return sections

    @staticmethod
    def _escape_question(question: str) -> str:
        """Escape a question's literal **: without changing the Markdown row."""
        return re.sub(
            r"\*\*(\\*):",
            lambda match: "**" + "\\" * (len(match.group(1)) + 1) + ":",
            question,
        )

    def _write(self, data: dict[str, list[tuple[str, str]]]) -> None:
        out = ["# AppliedIn facts", "",
               "_Human-editable: add or fix answers; the agent reads them._", ""]
        for pk in ["global", *[p for p in data if p != "global"]]:
            if pk not in data:
                continue
            out.append(f"## {'global' if pk == 'global' else 'company: ' + pk.split('#', 1)[1]}")
            out += [f"- **{self._escape_question(q)}**: {a}" for q, a in data[pk]]
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
                entries[i] = (q, answer)  # historical raw key remains stable
                break
        else:
            entries.append((strip_label_decoration(question), answer))
        self._write(data)

    def seed_global(self, entries: dict[str, str], *, source: str = "seed") -> None:
        for q, a in entries.items():
            self.put(q, a, AnswerScope.GLOBAL, source=source)

    def all_facts(self, company: str) -> dict[str, str]:
        data = self._load()
        # Shadow by semantic comparison, not by raw question spelling.
        # Return only each winning row's actual stored key.
        selected: dict[str, tuple[str, str]] = {}
        for pk in ("global", f"company#{company.strip().lower()}"):
            for q, a in data.get(pk, []):
                selected[normalize_label(q)] = (q, a)
        return {q: a for q, a in selected.values()}


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
