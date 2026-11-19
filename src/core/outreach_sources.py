"""Read-only #386 adapter: only a fresh, actually proved no-opening result."""
from __future__ import annotations
from dataclasses import dataclass
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from discovery.funding import FundingRefusal, State, registrable


class SourceHold(ValueError):
    """No send: repair the specific source evidence before retrying."""


def _date(raw):
    if not isinstance(raw, str):
        raise SourceHold("missing_funding_recheck_time")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SourceHold("invalid_funding_recheck_time") from exc
    if value.tzinfo is None:
        raise SourceHold("funding_recheck_timezone_missing")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class FundedNoOpening:
    company_id: str
    name: str
    domain: str
    homepage: str
    aliases: tuple[str, ...]
    source_id: str
    verified_at: datetime
    reason: str


def read_no_opening(settings, company_id, *, now=None, require_fresh=True):
    """#386 has no last-successful-pending-recheck field, so age from seen_at.

    Its pending bucket also contains source/consumer failures, which are *not*
    no-opening proof. A failed recheck may advance next_check but never grants
    this reader seven more days of SMTP eligibility.
    """
    if (getattr(settings, "mode", None) != "local"
            or not getattr(settings, "funding_enabled", False)
            or not getattr(settings, "outreach_enabled", False)):
        raise SourceHold("local_outreach_or_funding_off")
    if (not isinstance(company_id, str) or len(company_id) != 32
            or any(c not in "0123456789abcdef" for c in company_id)):
        raise SourceHold("funded_company_id_unverified")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise SourceHold("timezone_required")
    now = now.astimezone(UTC)
    path = Path(settings.local_dir) / "funded-watchlist.yaml"
    if not path.is_file() or path.is_symlink():
        raise SourceHold("funded_source_missing")
    try:
        state = State(path).data  # validated #386 state, no write
    except (OSError, ValueError, FundingRefusal) as exc:
        raise SourceHold("funded_source_unreadable") from exc
    row, deal = state["pending"].get(company_id), state["deals"].get(company_id)
    if (not isinstance(row, dict) or not isinstance(deal, dict)
            or deal.get("status") != "pending"
            or row.get("reason") not in {"no_openings", "no_relevant_openings"}
            or deal.get("reason") != row["reason"]):
        raise SourceHold("not_proved_no_matching_opening")
    seen, expires = _date(row.get("seen_at")), _date(row.get("expires"))
    if seen > now or expires <= now:
        raise SourceHold("funded_source_not_current")
    if require_fresh and now - seen > timedelta(days=7):
        raise SourceHold("fresh_successful_funding_recheck_required")
    name, domain, homepage = row.get("name"), row.get("employer_domain"), row.get("homepage")
    if (not isinstance(name, str) or not 2 <= len(name.strip()) <= 150
            or not isinstance(domain, str) or not isinstance(homepage, str)
            or registrable(homepage) != domain):
        raise SourceHold("verified_employer_domain_missing")
    aliases = row.get("aliases", [])
    if (not isinstance(aliases, list) or len(aliases) > 8
            or any(not isinstance(a, str) or not a.strip() or len(a) > 150 for a in aliases)):
        raise SourceHold("employer_aliases_invalid")
    names = {name.strip().casefold()} | {a.strip().casefold() for a in aliases}
    for bucket in ("pending", "active"):
        for key, other in state[bucket].items():
            if key == company_id:
                continue
            if (other.get("employer_domain") == domain
                    or names.intersection({str(other.get("name", "")).strip().casefold()}
                                          | {str(a).strip().casefold() for a in other.get("aliases", [])})):
                raise SourceHold("funded_employer_identity_collision")
    return FundedNoOpening(company_id, name.strip(), domain, homepage, tuple(aliases),
                           str(row.get("source") or ""), seen, row["reason"])


_SUPPORTED_APPLICATION_ATS = frozenset({
    "greenhouse", "ashby", "lever", "workday", "workable",
    "smartrecruiters", "eightfold", "personio",
})
_GENERAL_ROLE = re.compile(r"\b(?:general application|open application|talent pool)\b", re.I)
_CUSTOM_FORM = re.compile(r"\b(?:submit|send|upload|attach|apply with)\b.{0,90}\b(?:cv|resume|résumé)\b", re.I | re.S)


def verify_funded_application_route(settings, company_id, identity, url, meta, *, now=None):
    """Only a real source-verified general posting reaches existing apply-role.

    This changes the JobRecord's discovery source, never the scorer or any
    final-Submit path. If the present reader cannot prove a custom CV form,
    leave it to the ordinary existing operator route, not a fabricated PK.
    """
    if (getattr(settings, "mode", None) != "local"
            or not getattr(settings, "outreach_enabled", False)
            or not getattr(settings, "outreach_application_handoff_enabled", False)):
        raise SourceHold("funded_application_handoff_off")
    lead = read_no_opening(settings, company_id, now=now, require_fresh=False)
    if not isinstance(identity, dict) or not isinstance(meta, dict):
        raise SourceHold("funded_application_identity_unverified")
    ats, company = identity.get("ats"), identity.get("company")
    if (not isinstance(company, str) or company.casefold() not in
            {lead.name.casefold(), *(a.casefold() for a in lead.aliases)}):
        raise SourceHold("funded_application_employer_mismatch")
    title = meta.get("title")
    source_text = meta.get("text")
    if (not isinstance(title, str) or not _GENERAL_ROLE.search(title)
            or not isinstance(source_text, str) or not source_text.strip()):
        raise SourceHold("funded_general_posting_not_proven")
    if ats == "custom":
        if registrable(url) != lead.domain or not _CUSTOM_FORM.search(source_text[:12000]):
            raise SourceHold("funded_official_custom_cv_form_unverified")
    elif ats not in _SUPPORTED_APPLICATION_ATS:
        raise SourceHold("funded_application_ats_not_supported")
    return lead
