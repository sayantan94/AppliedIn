"""No pixel is a supported, deterministic seven-day schedule."""
from datetime import UTC, datetime, timedelta

from core.outreach_followups import due_for_band, earliest_due, within_stage_limit


def test_pixel_less_and_late_six_opens_do_not_postpone_earned_due():
    sent = datetime(2026, 11, 20, 8, tzinfo=UTC)
    first = due_for_band(sent, tracked=False)
    assert first == sent + timedelta(days=7)
    late = [sent + timedelta(days=6, hours=i) for i in range(6)]
    new = due_for_band(sent, opens=late, tracked=True)
    assert earliest_due(first, new) == first


def test_three_to_five_open_band_and_stage_cap():
    sent = datetime(2026, 11, 20, 8, tzinfo=UTC)
    opens = tuple(sent + timedelta(days=1, hours=i) for i in range(3))
    assert due_for_band(sent, opens=opens, tracked=True) == sent + timedelta(days=3)
    assert within_stage_limit(1, 0)
    assert within_stage_limit(2, 1)
    assert not within_stage_limit(3, 2)
    assert within_stage_limit(3, 2, expanded=True)
    assert not within_stage_limit(6, 5, expanded=True)
