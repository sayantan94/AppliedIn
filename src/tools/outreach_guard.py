"""Email-only entry gate; never an application/form/Submit guard."""
from __future__ import annotations

from core.models import Status
from core.outreach_ledger import OutreachHold


def company_application_clear(stores, lead):
    """Read existing application status; reject any missing/unreadable witness.

    This doesn't infer rejection from a scraped title or override the scorer.
    An operator-recorded rejection also belongs in the outreach ledger stop.
    """
    if stores is None or not hasattr(stores, "tracking"):
        raise OutreachHold("application_tracker_unavailable")
    known = {lead.name.casefold(), *(a.casefold() for a in lead.aliases)}
    try:
        for status in (Status.APPLIED, Status.APPLIED_MANUAL, Status.SUBMITTING):
            for row in stores.tracking.query_status(status):
                if str(row.get("company", "")).strip().casefold() in known:
                    return False
        # A rejection may be a recorded employer disposition, not a member of
        # Status (which models the application lifecycle, not reply outcomes).
        # Reject only explicit recorded negatives; do not infer rejection
        # from a skipped posting or a low relevance score.
        for row in stores.tracking.all():
            if not isinstance(row, dict) or str(
                    row.get("company", "")).strip().casefold() not in known:
                continue
            if str(row.get("status", "")).casefold() in {
                    "applied", "applied_manual", "submitting", "rejected", "declined"}:
                return False
            if row.get("rejected_at") or row.get("employer_rejection_at"):
                return False
    except Exception as exc:
        raise OutreachHold("application_tracker_unreadable") from exc
    return True
