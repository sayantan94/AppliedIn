"""Deterministic due dates. Pixel-disabled threads take the seven-day branch."""
from __future__ import annotations
from datetime import UTC, datetime, timedelta


def due_for_band(sent_at: datetime, *, opens=(), tracked=False):
    if sent_at.tzinfo is None or any(x.tzinfo is None for x in opens):
        raise ValueError("aware_timestamps_required")
    sent = sent_at.astimezone(UTC)
    qualified = sorted(x.astimezone(UTC) for x in opens) if tracked else []
    if len(qualified) < 3:
        return sent + timedelta(days=7)
    if len(qualified) < 6:
        return max(sent + timedelta(hours=72),
                   min(sent + timedelta(days=7), qualified[2] + timedelta(hours=24)))
    return max(sent + timedelta(hours=48), qualified[5] + timedelta(hours=48))


def earliest_due(earned: datetime | None, calculated: datetime):
    """Late 6+ opens must not postpone an already-due but quota-held stage."""
    if calculated.tzinfo is None or (earned is not None and earned.tzinfo is None):
        raise ValueError("aware_timestamps_required")
    return min(earned, calculated) if earned else calculated


def within_stage_limit(stage, sent_count, *, expanded=False):
    return (isinstance(stage, int) and 1 <= stage <= 5
            and sent_count == stage - 1
            and (stage <= 2 or expanded))
