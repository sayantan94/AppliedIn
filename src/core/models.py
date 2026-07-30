"""Domain models and enums shared across every AppliedIn service.

These are the vocabulary of the pipeline: a JobRecord flows discovery ->
tailoring -> apply, and its lifecycle is tracked by Status / GateReason in
DynamoDB. Keep this module free of AWS or framework imports so every
package can depend on it cheaply.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, computed_field

from .ids import jd_hash, make_pk


class Status(str, Enum):
    """Lifecycle of a single application row in the ``applications`` table."""

    FOUND = "found"
    TAILORING = "tailoring"  # in-progress: score/tailor running (UI shows it working)
    TAILORED = "tailored"
    SKIPPED = "skipped"
    SUBMITTING = "submitting"  # idempotency marker; set immediately before submit
    APPLIED = "applied"
    APPLIED_MANUAL = "applied_manual"
    NEEDS_HUMAN = "needs_human"
    CAPPED = "capped"
    JOB_GONE = "job_gone"
    FAILED = "failed"  # apply ran but couldn't confirm a submit (dead posting, no form)
    ERROR = "error"


class GateReason(str, Enum):
    """Why an application stopped and requires a human (on ``needs_human``)."""

    APPROVAL = "approval"  # ready to submit — waiting for your go-ahead
    CAPTCHA = "captcha"
    NO_ACCOUNT = "no_account"  # auto-signup was blocked (CAPTCHA/SMS-2FA)
    UNKNOWN_FIELD = "unknown_field"
    LOW_CONFIDENCE = "low_confidence"
    GATED_MODE = "gated_mode"
    FORM_DRIFT = "form_drift"
    SUSPECTED_REPOST = "suspected_repost"
    SUBMIT_UNCERTAIN = "submit_uncertain"


class DiscoveryMode(str, Enum):
    FEED = "feed"
    CRAWL = "crawl"
    # Always read this careers page in a real browser, skipping the plain fetch.
    # For a client-rendered search UI (jobs.apple.com/search and friends) the
    # cheap tier returns a fraction of the listing and, because it returns SOME
    # matches, never escalates — so a rescan keeps finding the same short window
    # and genuinely new roles below it stay invisible. Naming the site is honest;
    # guessing from the result count is not.
    BROWSER = "browser"


class ApplyMode(str, Enum):
    AUTO = "auto"
    GATED = "gated"
    ASSIST = "assist"


class AnswerScope(str, Enum):
    GLOBAL = "global"
    COMPANY = "company"


class JobRecord(BaseModel):
    """A normalized posting, identical whether it came from a feed or the crawler."""

    company: str
    job_id: str
    title: str
    jd_url: str
    jd_text: str
    location: str = ""
    ats: str = ""
    # Set when the posting was read by an agent that judged fit while it was
    # already looking at it. Saves scoring the same text twice; None means the
    # scorer still has to run.
    crawl_score: int | None = None
    crawl_why: str = ""
    # When the EMPLOYER first published the role, from the board's own feed or read
    # off the posting. Not when we found it: a sweep discovers a two year old
    # listing and a one hour old one in the same second, and only one of those is
    # worth being early to. Empty when the source does not say.
    posted_at: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pk(self) -> str:
        return make_pk(self.company, self.job_id)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def jd_hash(self) -> str:
        return jd_hash(self.jd_text)
