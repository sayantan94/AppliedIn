"""Offline email-only application/rejection stop guard."""
from types import SimpleNamespace
import pytest

from core.models import Status
from core.outreach_ledger import OutreachHold
from tools.outreach_guard import company_application_clear


def fixture(*, rows=(), reject=False):
    lead = SimpleNamespace(name="Example Co", aliases=("Example Company",))
    mapping = {Status.APPLIED: [], Status.APPLIED_MANUAL: [], Status.SUBMITTING: []}
    if rows:
        mapping[Status.APPLIED] = list(rows)
    tracker = SimpleNamespace(query_status=lambda status: mapping[status],
                              all=lambda: [{"company":"Example Company",
                                             "status":"found", **(
                                                 {"employer_rejection_at":"2026-11-20"}
                                                 if reject else {})}])
    return SimpleNamespace(tracking=tracker), lead


def test_existing_application_or_recorded_rejection_holds_email_only():
    stores, lead = fixture(rows=[{"company":"Example Co", "status":"applied"}])
    assert not company_application_clear(stores, lead)
    stores, lead = fixture(reject=True)
    assert not company_application_clear(stores, lead)
    stores, lead = fixture()
    assert company_application_clear(stores, lead)


def test_missing_or_unreadable_tracker_never_authorizes_email():
    _, lead = fixture()
    with pytest.raises(OutreachHold, match="application_tracker_unavailable"):
        company_application_clear(None, lead)
    broken = SimpleNamespace(tracking=SimpleNamespace(
        query_status=lambda _status: [], all=lambda: (_ for _ in ()).throw(OSError("down"))))
    with pytest.raises(OutreachHold, match="application_tracker_unreadable"):
        company_application_clear(broken, lead)
