"""Persistent record of jobs already handled — so re-running discovery doesn't
process the same posting again.

Keyed by JD URL (absolute), with a little metadata so the file is human-readable:

    {
      "https://jobs.apple.com/en-us/details/…": {
        "company": "Apple", "title": "Senior Software Engineer, AI", "seen": "2026-07-17T…"
      }
    }

`load()` returns just the URL set (for the dedup check). Survives Redis resets;
the dashboard Reset clears it. Tolerates the old flat-list format on read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger

log = get_logger(__name__)


def _path() -> Path:
    return Path(get_settings().local_dir) / "seen.json"


def _load_raw() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {}
    if isinstance(data, list):  # migrate the old flat-list format
        return {u: {} for u in data if u}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    ordered = dict(sorted(  # group by company then title so it reads nicely
        data.items(), key=lambda kv: (kv[1].get("company", ""), kv[1].get("title", ""))))
    p.write_text(json.dumps(ordered, indent=2))


def load() -> set[str]:
    """The set of JD URLs already seen (for the dedup check)."""
    return set(_load_raw())


def mark(jobs: list) -> None:
    """Record these JobRecords as seen: url -> {company, title, seen}."""
    data = _load_raw()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for j in jobs:
        url = getattr(j, "jd_url", "") or ""
        if url and url not in data:
            data[url] = {"company": getattr(j, "company", "") or "",
                         "title": getattr(j, "title", "") or "", "seen": now}
    _write(data)


def clear() -> None:
    _path().unlink(missing_ok=True)



