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


def test_possible_submit_state_is_a_company_wide_stop_even_without_applied_status():
    """The normal unconfirmed Submit is never a safe invitation for outreach."""
    lead = SimpleNamespace(name="Example Co", aliases=(), domain="example-co.com")
    for uncertain in ({"status":"needs_human", "gate_reason":"submit_uncertain"},
                      {"status":"needs_human", "fail_kind":"uncertain"}):
        row = {"company":"Example Co", **uncertain}
        stores = SimpleNamespace(tracking=SimpleNamespace(
            all=lambda: [row], query_status=lambda _status: []))
        assert not company_application_clear(stores, lead)


def test_applied_on_verified_employer_domain_stops_different_display_name():
    """An exact official employer-domain posting joins without guessing name aliases."""
    lead = SimpleNamespace(name="Example Co Incorporated", aliases=(),
                           domain="example-co.com")
    row = {"company":"Example Co", "status":"applied",
           "jd_url":"https://careers.example-co.com/jobs/123"}
    stores = SimpleNamespace(tracking=SimpleNamespace(
        all=lambda: [row], query_status=lambda _status: []))
    assert not company_application_clear(stores, lead)


def test_ats_tenant_label_matching_domain_is_unresolved_not_assumed_distinct():
    """A vendor-hosted tenant may be the employer, but tenant text cannot prove it."""
    lead = SimpleNamespace(name="Example Co", aliases=(), domain="example-co.com")
    row = {"company":"example-co", "status":"applied",
           "jd_url":"https://jobs.ashbyhq.com/example-co/123"}
    stores = SimpleNamespace(tracking=SimpleNamespace(
        all=lambda: [row], query_status=lambda _status: []))
    with pytest.raises(OutreachHold, match="application_employer_join_requires_reconciliation"):
        company_application_clear(stores, lead)
    # A distinct other employer with no verified join does not cause a global hold.
    row["company"] = "Unrelated Business"
    assert company_application_clear(stores, lead)


def test_enum_submit_uncertain_and_status_set_only_are_still_stops():
    from core.models import GateReason
    lead = SimpleNamespace(name="Example Co", aliases=(), domain="example-co.com")
    uncertain = {"company":"Example Co", "status":"needs_human",
                 "gate_reason":GateReason.SUBMIT_UNCERTAIN}
    source = SimpleNamespace(tracking=SimpleNamespace(
        all=lambda: [uncertain], query_status=lambda _status: []))
    assert not company_application_clear(source, lead)
    # An explicit applied-status index is authoritative even if the row's
    # legacy projection omitted the status field itself.
    tracked = {"company":"Example Co", "jd_url":"https://example-co.com/jobs/42"}
    source = SimpleNamespace(tracking=SimpleNamespace(
        all=lambda: [],
        query_status=lambda status: [tracked] if status == Status.APPLIED else []))
    assert not company_application_clear(source, lead)
