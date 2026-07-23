"""Durable, human-readable memory of what the pipeline DID.

The live event feed lives in Redis and is ephemeral (trimmed, gone on flush).
This is the opposite: a plain-markdown log on disk (`<local_dir>/memory.md`)
that survives restarts and reads like a diary — one line per meaningful
outcome, grouped by day. It answers "what did AppliedIn actually do?" at a
glance, without the dashboard.

Only OUTCOMES are recorded (applied, needs-you, failed) — not process chatter
(every field fill, every discovered posting). One line per (job, outcome) per
day, so a retried job doesn't spam the log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import get_settings
from .logging import get_logger

log = get_logger(__name__)

# event kind -> how it reads in the diary. Kinds not here are NOT remembered.
_LABEL = {
    "applied": "✅ Applied",
    "applied_manual": "✅ Applied (by hand)",
    "gate": "⏸ Needs you",
    "failed": "❌ Failed",
    "error": "⚠️ Error",
}

_seen: set[str] = set()  # (day|pk|kind) already written this process — dedup retries


def _path() -> Path:
    return Path(get_settings().local_dir) / "memory.md"


def remember(kind: str, *, pk: str | None = None, detail: str = "",
             company: str = "", title: str = "", at: str = "", **_: Any) -> None:
    """Append one outcome to the markdown memory. No-op for non-outcome kinds
    and for a (job, outcome) already logged today. Never raises."""
    label = _LABEL.get(kind)
    if not label or (pk and str(pk).startswith("meta#")):
        return
    try:
        from datetime import datetime

        stamp = at or datetime.now().astimezone().isoformat()
        day, clock = stamp[:10], stamp[11:16]

        # Fill company/title from the tracking row when the event didn't carry them.
        if pk and (not company or not title):
            try:
                from .stores import make_stores
                row = make_stores().tracking.get(pk) or {}
                company = company or row.get("company", "")
                title = title or row.get("title", "")
            except Exception:  # noqa: BLE001
                pass

        dedup = f"{day}|{pk}|{kind}"
        if dedup in _seen:
            return
        _seen.add(dedup)

        who = " · ".join(x for x in (company, title) if x) or (pk or "")
        note = f" — {detail.strip()}" if detail.strip() else ""
        line = f"- **{clock}** {label} — {who}{note[:180]}\n"

        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (path.read_text() if path.exists()
                else "# AppliedIn — memory\n\nA running diary of what the pipeline did. "
                     "Newest day at the bottom.\n")
        header = f"## {day}\n"
        # Days are chronological and we only append, so today's section is last.
        if header not in text:
            text = text.rstrip("\n") + "\n\n" + header
        if not text.endswith("\n"):
            text += "\n"
        path.write_text(text + line)
    except Exception:  # memory is best-effort — never break the pipeline
        log.debug("remember failed", exc_info=True)


def read(limit_days: int = 0) -> str:
    """The memory markdown (whole file, or the last `limit_days` day-sections)."""
    path = _path()
    if not path.exists():
        return "# AppliedIn — memory\n\n_(nothing recorded yet)_\n"
    text = path.read_text()
    if limit_days <= 0:
        return text
    parts = text.split("\n## ")
    head, days = parts[0], parts[1:]
    kept = days[-limit_days:]
    return head + ("".join("\n## " + d for d in kept) if kept else "")
