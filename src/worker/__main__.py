"""Container entrypoint: ``python -m worker``.

Selects the task by ``APPLIEDIN_TASK_MODE``:
  apply -> run_apply for the job in ``APPLIEDIN_JOB_PK`` (set by the dispatcher
           Lambda's RunTask override)
  crawl -> run_crawl for the company JSON in ``APPLIEDIN_CRAWL_COMPANY``
           (e.g. '{"name": "Acme", "careers_url": "https://...", "ats": "custom"}')

Real AWS clients + a real Playwright page are wired here and ONLY here; all
library code takes injected deps.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import yaml
from core.config import get_settings
from core.logging import get_logger
from core.models import ApplyMode, GateReason, JobRecord
from core.storage.answer_bank import AnswerBank
from core.storage.artifacts import ArtifactStore
from core.storage.queue import Queue
from core.storage.secrets import SecretsClient
from core.storage.tracking import TrackingStore

from .apply_main import ApplyDeps, PortalConfig, run_apply
from .crawl_main import CrawlDeps, llm_extract, run_crawl
from .gmail import fetch_code

log = get_logger(__name__)


class LogNotifier:
    """Placeholder notifier until the whatsapp package lands: gate/receipt
    events surface as structured logs (and thence CloudWatch)."""

    def send_gate(
        self, pk: str, reason: GateReason, *, fieldmap_key: str, screenshot_keys: list[str]
    ) -> None:
        log.info("GATE %s reason=%s fieldmap=%s shots=%s", pk, reason.value, fieldmap_key,
                 screenshot_keys)

    def send_receipt(self, pk: str, confirmation_id: str | None) -> None:
        log.info("RECEIPT %s confirmation=%s", pk, confirmation_id)


def _load_portals(config_dir: str) -> dict[str, PortalConfig]:
    path = Path(config_dir) / "watchlist.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {
        str(c["name"]).lower(): PortalConfig(
            mode=ApplyMode(c.get("mode", "gated")),
            login_secret=str(c.get("login_secret", "")),
        )
        for c in data.get("companies", [])
    }


def _keyword_matcher(config_dir: str):
    """Minimal stage-1 predicate from preferences.yaml (the full filter lives
    in the discovery package, which this container does not bundle)."""
    path = Path(config_dir) / "preferences.yaml"
    prefs = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    include = [k.lower() for k in prefs.get("include_keywords", [])]
    exclude = [k.lower() for k in prefs.get("exclude_keywords", [])]

    def matches(job: JobRecord) -> bool:
        text = f"{job.title} {job.jd_text}".lower()
        if any(k in text for k in exclude):
            return False
        return not include or any(k in text for k in include)

    return matches


def main() -> int:
    settings = get_settings()
    region = settings.aws_region
    tracking = TrackingStore(settings.applications_table, region=region)
    secrets = SecretsClient(region=region)
    mode = os.environ.get("APPLIEDIN_TASK_MODE", "apply")

    if mode == "apply":
        pk = os.environ["APPLIEDIN_JOB_PK"]
        deps = ApplyDeps(
            tracking=tracking,
            answer_bank=AnswerBank(settings.answer_bank_table, region=region),
            artifacts=ArtifactStore(settings.artifacts_bucket, region=region),
            secrets=secrets,
            notifier=LogNotifier(),
            settings=settings,
            identity=json.loads(os.environ.get("APPLIEDIN_IDENTITY_JSON", "{}")),
            portals=_load_portals(settings.config_dir),
            gmail_fetch=lambda query: fetch_code(query, secrets),
        )
        status = asyncio.run(run_apply(pk, deps=deps))
        log.info("apply finished pk=%s status=%s", pk, status)
        return 0

    if mode == "crawl":
        company_cfg = json.loads(os.environ["APPLIEDIN_CRAWL_COMPANY"])
        deps = CrawlDeps(
            tracking=tracking,
            queue=Queue(region=region),
            settings=settings,
            extractor=llm_extract,
            matches=_keyword_matcher(settings.config_dir),
        )
        enqueued = asyncio.run(run_crawl(company_cfg, deps=deps))
        log.info("crawl finished company=%s enqueued=%d", company_cfg.get("name"), enqueued)
        return 0

    log.error("unknown APPLIEDIN_TASK_MODE=%r (expected apply|crawl)", mode)
    return 2


if __name__ == "__main__":
    sys.exit(main())
