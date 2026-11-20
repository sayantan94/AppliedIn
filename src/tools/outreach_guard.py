"""Email-only entry gate; never an application/form/Submit guard."""
from __future__ import annotations

from core.models import Status
from core.outreach_ledger import OutreachHold
from discovery.funding import registrable


def _company_join(row, lead, names):
    """Only official employer-hosted posting URLs establish a second exact join.

    A vendor ATS tenant/display label resembling the verified employer domain
    is *not* independent evidence. Hold such a possible join for reconciliation
    rather than assume two employers are distinct or claim they are the same.
    """
    company = str(row.get("company") or "").strip().casefold()
    if company in names:
        return "matched"
    domain = getattr(lead, "domain", "")
    if not isinstance(domain, str) or not domain:
        return "unrelated"
    url = row.get("jd_url")
    if isinstance(url, str) and registrable(url) == domain:
        return "matched"
    if company and company == domain.split(".", 1)[0]:
        return "unresolved"
    return "unrelated"


def _blocking_application(row):
    def normalized(value):
        return str(getattr(value, "value", value) or "").strip().casefold()

    status = normalized(row.get("status"))
    gate_reason = normalized(row.get("gate_reason"))
    fail_kind = normalized(row.get("fail_kind"))
    return (status in {"applied", "applied_manual", "submitting", "rejected", "declined"}
            or gate_reason == "submit_uncertain"
            or (status in {"needs_human", "error", "failed"} and fail_kind == "uncertain")
            or bool(row.get("rejected_at") or row.get("employer_rejection_at")))


def company_application_clear(stores, lead):
    """Deny known applications, possible Submit and unresolved employer joins.

    Applied/uncertain rows can be stored under a parsed ATS tenant label rather
    than the independently verified #386 employer name. Do not equate tenant
    names on string similarity or let a possibly submitted application send
    outreach through another channel.
    """
    if stores is None or not hasattr(stores, "tracking"):
        raise OutreachHold("application_tracker_unavailable")
    names = {lead.name.strip().casefold(), *(a.strip().casefold() for a in lead.aliases)}
    try:
        # Some tracker adapters do not include all active statuses in all().
        # Query explicit committing states as well, preserving the old stop.
        rows = list(stores.tracking.all())
        for status in (Status.APPLIED, Status.APPLIED_MANUAL, Status.SUBMITTING):
            for row in stores.tracking.query_status(status):
                if not isinstance(row, dict):
                    raise OutreachHold("application_tracker_row_unreadable")
                # Query membership is status evidence even when a legacy row
                # omits its status field in the returned projection.
                rows.append({**row, "status": status.value})
        for row in rows:
            if not isinstance(row, dict):
                raise OutreachHold("application_tracker_row_unreadable")
            if not _blocking_application(row):
                continue
            relation = _company_join(row, lead, names)
            if relation == "matched":
                return False
            if relation == "unresolved":
                raise OutreachHold("application_employer_join_requires_reconciliation")
    except OutreachHold:
        raise
    except Exception as exc:
        raise OutreachHold("application_tracker_unreadable") from exc
    return True
