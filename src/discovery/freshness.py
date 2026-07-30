"""How old a posting is, and whether it is new enough to bother with.

Discovery finds a two year old listing and a one hour old one in the same second.
Only one of those is worth being early to: a role posted in the last day or two is
one where the pile of applications is still small, which is the whole reason to
run a scan rather than browse a careers page.

The date has to come from the EMPLOYER, not from us. `discovered_at` says when we
looked, so it makes every job on a first sweep look brand new, and every job on
the next sweep look stale.

Boards disagree about the field and the format: Greenhouse `first_published`,
Ashby `publishedAt`, Lever `createdAt` in epoch milliseconds, SmartRecruiters
`releasedDate`. The adapters normalise to an ISO string in `JobRecord.posted_at`;
this module is only about reading that and deciding.

A posting with NO date is kept. Roughly a third of sources publish none, and
silently dropping those would turn "only show me fresh roles" into "only show me
roles from boards that timestamp", which is a different and much worse filter.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone

from core.logging import get_logger

log = get_logger(__name__)


def age_hours(posted_at: str, *, now: datetime | None = None) -> float | None:
    """Hours since the employer published it, or None when unknown or unparseable."""
    if not posted_at:
        return None
    raw = str(posted_at).strip()
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        log.debug("could not read a posting date from %r", raw)
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (now or datetime.now(timezone.utc)) - when
    hours = delta.total_seconds() / 3600
    # A future date is a board with a clock problem or a scheduled publish. Treat
    # it as brand new rather than as an error, since it is certainly not stale.
    return max(0.0, hours)


def is_fresh(posted_at: str, max_age_hours: float | int | None,
             *, now: datetime | None = None) -> bool:
    """Whether a posting is new enough to keep.

    True when there is no limit set, and true when the posting carries no date:
    see the module docstring on why an unknown date is kept rather than dropped.
    """
    try:
        limit = float(max_age_hours or 0)
    except (TypeError, ValueError):
        return True
    if limit <= 0:
        return True
    age = age_hours(posted_at, now=now)
    if age is None:
        return True
    return age <= limit


def describe(posted_at: str, *, now: datetime | None = None) -> str:
    """Short human phrase for the board, e.g. "3h ago", "2d ago", "" if unknown."""
    age = age_hours(posted_at, now=now)
    if age is None:
        return ""
    if age < 1:
        return "just now"
    if age < 24:
        return f"{int(age)}h ago"
    days = int(age // 24)
    return "yesterday" if days == 1 else f"{days}d ago"


# The window for THIS run, chosen when the scan was triggered.
#
# A context variable rather than a flag, because it is a property of one scan and
# not of the company: "show me the last 24 hours at these four" must not quietly
# become their permanent setting. It beats the per company preference while the run
# is in flight and disappears with it. Set by run_discovery, read by the enqueue
# filter and by the crawl prompt.
_RUN_WINDOW: ContextVar[float] = ContextVar("discovery_run_window", default=0.0)


def set_run_window(hours: float | int | None) -> None:
    try:
        _RUN_WINDOW.set(max(0.0, float(hours or 0)))
    except (TypeError, ValueError):
        _RUN_WINDOW.set(0.0)


def run_window() -> float:
    """Hours this run is limited to, 0 meaning no limit."""
    return _RUN_WINDOW.get()
