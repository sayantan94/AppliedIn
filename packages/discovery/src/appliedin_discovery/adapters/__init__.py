"""ATS adapter protocol and registry.

Each adapter turns one company's public JSON feed into normalized JobRecords.
The registry lets the handler dispatch by ``company.ats`` without importing
every adapter directly.
"""

from __future__ import annotations

from typing import Protocol

import httpx
from appliedin_core.models import JobRecord

from ..watchlist import CompanyConfig


class ATSAdapter(Protocol):
    ats: str

    def fetch(self, company: CompanyConfig, client: httpx.Client) -> list[JobRecord]: ...


from .ashby import AshbyAdapter  # noqa: E402
from .greenhouse import GreenhouseAdapter  # noqa: E402
from .lever import LeverAdapter  # noqa: E402
from .smartrecruiters import SmartRecruitersAdapter  # noqa: E402
from .workday import WorkdayAdapter  # noqa: E402

ADAPTERS: dict[str, ATSAdapter] = {
    a.ats: a
    for a in (
        GreenhouseAdapter(),
        LeverAdapter(),
        AshbyAdapter(),
        SmartRecruitersAdapter(),
        WorkdayAdapter(),
    )
}

__all__ = ["ATSAdapter", "ADAPTERS"]
