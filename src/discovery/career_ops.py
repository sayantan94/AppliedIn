"""Career Ops feed discovery, staged separately from approved applications.

The upstream checkout is optional and pinned. Only its HTTP provider modules run;
its prompts, title preferences and submission workflow never own AppliedIn state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from core.config import get_settings
from core.models import JobRecord
from core.stores import make_stores
from discovery.career_ops_setup import REVISION
from tools.ats import detect_ats

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "scripts/integrations/career-ops.mjs"
_LOCK = threading.RLock()
_SCAN = threading.Lock()
_RUNNING = False
_ACTIVE = {}
_PROGRESS = {"run_id": "", "events": []}
log = logging.getLogger(__name__)


def now() -> str:
    return datetime.now(UTC).isoformat()


def _path() -> Path:
    return Path(get_settings().local_dir) / "career-ops-board.json"


def checkout() -> Path:
    return Path(get_settings().local_dir).resolve() / "integrations/career-ops"


def _read() -> dict:
    path = _path()
    if not path.exists():
        return {
            "jobs": {},
            "settings": {"companies": None, "scheduled": True, "auto_prepare": False},
            "last_scan": None,
        }
    return json.loads(path.read_text())


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


def _bridge(payload: dict, timeout: int = 600) -> list:
    try:
        proc = subprocess.run(
            ["node", str(BRIDGE), str(checkout())],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = proc.stdout
        failure = (
            f"Career Ops scanner exited with code {proc.returncode}." if proc.returncode else ""
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        failure = "Search timed out; completed companies were saved. Retry the remaining sources."
    except FileNotFoundError as exc:
        raise ValueError("Node.js 18 or newer is required for Career Ops.") from exc
    if payload.get("catalog"):
        if failure:
            raise ValueError(failure)
        return json.loads(output)
    results = []
    for line in output.splitlines():
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a timeout may cut the last line midway through a result
    if failure:
        results.append({"company": "Scanner", "error": failure})
    return results


@lru_cache(maxsize=4)
def _catalog_at(root: str, config_dir: str) -> tuple:
    repo = Path(root)
    if not (repo / "providers/_http.mjs").exists():
        port = os.environ.get("APPLIEDIN_WEB_PORT", "8787")
        raise ValueError(
            f"Career Ops is not installed for this instance. "
            f"Run ./appliedin start --port {port} to install it."
        )
    revision = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if revision != REVISION:
        raise ValueError(
            f"Career Ops version changed. Restore the pinned revision: {REVISION}."
        )
    from discovery.watchlist import load_watchlist

    watchlist = load_watchlist(Path(config_dir) / "watchlist.yaml")
    known = {c.name.casefold(): c.name for c in watchlist}
    data = yaml.safe_load((repo / "templates/portals.example.yml").read_text())
    entries = {}
    for entry in data.get("tracked_companies", []):
        if entry.get("enabled") is False or entry.get("parser"):
            continue
        name = known.get(entry["name"].casefold(), entry["name"])
        entries[name.casefold()] = {
            k: entry[k] for k in ("api", "careers_url", "provider") if k in entry
        }
        entries[name.casefold()]["name"] = name
    # Direct ATS URLs on our watchlist can fill gaps in the upstream directory.
    for c in watchlist:
        if c.name.casefold() not in entries:
            entries[c.name.casefold()] = {"name": c.name, "careers_url": c.careers_url}
    overrides = Path(config_dir) / "career_ops_sources.yaml"
    if overrides.exists():
        for entry in (yaml.safe_load(overrides.read_text()) or {}).get("companies", []):
            name = known.get(entry["name"].casefold(), entry["name"])
            entries[name.casefold()] = {
                k: entry[k] for k in ("api", "careers_url", "provider") if k in entry
            }
            entries[name.casefold()]["name"] = name
    catalog = _bridge({"entries": list(entries.values()), "catalog": True}, timeout=30)
    for entry in catalog:
        entry["tracked"] = entry["name"].casefold() in known
    return tuple(sorted(catalog, key=lambda c: c["name"].casefold()))


def catalog() -> list[dict]:
    return list(_catalog_at(str(checkout()), str(Path(get_settings().config_dir).resolve())))


def _settings(data: dict, sources: list[dict]) -> dict:
    settings = dict(data["settings"])
    if settings["companies"] is None:
        settings["companies"] = [c["name"] for c in sources if c["tracked"]]
    return settings


def canonical(url: str) -> str:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        host = "job-boards.greenhouse.io"
    # Preserve identifying query parameters; strip marketing attribution only.
    query = sorted(
        (k, v)
        for k, v in parse_qsl(p.query)
        if not k.lower().startswith("utm_") and k.lower() not in ("gh_src", "source", "ref")
    )
    return urlunsplit(("https", host, p.path.rstrip("/"), urlencode(query), ""))


def _query_job_id(url: str) -> str:
    values = [value.strip() for key, value in parse_qsl(urlsplit(url).query)
              if key.casefold() in {"gh_jid", "jobid", "job_id", "jid", "requisitionid", "reqid"}
              and value.strip()]
    return values[0] if len(values) == 1 else ""


def normalize(raw: dict, company: str, provider: str, *,
              directory_event: bool = False) -> dict | None:
    url, title = str(raw.get("url") or ""), str(raw.get("title") or "").strip()
    if urlsplit(url).scheme != "https" or not urlsplit(url).hostname or not title:
        return None
    url = canonical(url)
    from discovery.adapters.ashby import _iso

    from tools import posting_guard as pg

    identity = hashlib.sha256(url.encode()).hexdigest()[:24]
    description = (
        "" if provider == "oraclecloud"
        or (provider == "web_search" and raw.get("verification") != "verified")
        else str(raw.get("description") or "")[:100000]
    )
    incoming = raw.get("posting_guard")
    # Directory provider results are flattened before crossing the pinned
    # Node scanner. Even a forged or accidentally copied "clean" annotation
    # cannot recreate the missing original HTML witness.
    locally_verified = (provider == "web_search"
                        and raw.get("verification") == "verified"
                        and pg.valid(description, incoming))
    if locally_verified and not directory_event:
        guarded = {"text": description, "posting_guard": incoming}
    else:
        guarded = pg.analyze(description, source_required=directory_event)
        if not directory_event:
            # Other board/index/snippet data has no original-source witness.
            guarded = pg.projection(guarded["text"], guarded["posting_guard"])
    title = pg.analyze(title)["text"]
    return {
        "id": identity,
        "job_id": str(
            raw.get("id")
            or _query_job_id(url)
            or urlsplit(url).path.rstrip("/").split("/")[-1]
            or identity
        ),
        "company": company,
        "title": title,
        "url": url,
        "location": str(raw.get("location") or ""),
        "provider": provider,
        "description": guarded["text"],
        "posting_guard": guarded["posting_guard"],
        "posted_at": _iso(raw.get("postedAt")),
        "first_seen": now(),
        "last_seen": now(),
        "state": "new",
        "search_id": raw.get("search_id", ""),
        "why": str(raw.get("why") or ""),
        "verification": raw.get("verification", "feed"),
    }


def snapshot() -> dict:
    from core import flags

    try:
        sources, error = catalog(), ""
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        sources, error = [], str(exc)
    with _LOCK:
        data = _read()
    # Keep full descriptions private on disk; list polling should stay small.
    # fit_score still reads the description before it is dropped.
    from discovery.career_fit import public_rows
    from discovery.career_progress import attach_progress

    prefs = search_preferences()
    rows = public_rows(data["jobs"], prefs.get("include_keywords"))
    attach_progress(rows, make_stores())
    return {
        "sources": sources,
        "settings": _settings(data, sources),
        "jobs": rows,
        "last_scan": data.get("last_scan"),
        "last_search": data.get("last_search"),
        "last_network": data.get("last_network"),
        "network_filters": (data.get("network_search") or {}).get("filters"),
        "active_search": dict(_ACTIVE),
        "progress": progress_snapshot(),
        "preferences": prefs,
        "running": _RUNNING,
        "paused": flags.paused(),
        "error": error,
        "revision": REVISION[:12],
        "capabilities": {"apply_selected": True},
    }


def configure(values: dict) -> dict:
    sources = catalog()
    names = {c["name"] for c in sources}
    companies = values.get("companies")
    if not isinstance(companies, list) or any(
        not isinstance(n, str) or n not in names for n in companies
    ):
        raise ValueError("Choose companies from the available sources.")
    if any(type(values.get(k)) is not bool for k in ("scheduled", "auto_prepare")):
        raise ValueError("Search and preparation settings must be on or off.")
    with _LOCK:
        data = _read()
        data["settings"] = {
            "companies": list(dict.fromkeys(companies)),
            "scheduled": values["scheduled"],
            "auto_prepare": values["auto_prepare"],
        }
        for key in ("interests", "scheduled_interests", "search_provider"):
            if key in values:
                data["settings"][key] = values[key]
        _write(data)
    return data["settings"]


def progress_snapshot() -> dict:
    with _LOCK:
        return {
            "run_id": _PROGRESS["run_id"],
            "events": list(_PROGRESS["events"]),
            "running": _RUNNING,
            "active_search": dict(_ACTIVE),
        }


def report_progress(message: str) -> None:
    with _LOCK:
        events = _PROGRESS["events"]
        events.append(
            {
                "seq": events[-1]["seq"] + 1 if events else 1,
                "at": now(),
                "message": str(message)[:700],
            }
        )
        del events[:-80]


def reserve_scan(
    kind: str = "feeds",
    company: str = "",
    message: str = "",
    *,
    clear_network_stop: bool = False,
) -> bool:
    global _RUNNING, _ACTIVE
    if not _SCAN.acquire(blocking=False):
        return False
    if kind == "network" and clear_network_stop:
        from discovery import career_network

        # Clear stale process-local state before the active run becomes stoppable.
        career_network.STOP.clear()
    with _LOCK:
        _RUNNING = True
        _ACTIVE = {"kind": kind, "company": company, "started_at": now()}
        _PROGRESS.update(run_id=_ACTIVE["started_at"], events=[])
        report_progress(message or ("Search started" + (f" · {company}" if company else "")))
    return True


def _save_results(receipts: list[dict], receipt: dict):
    from discovery.career_fit import kept_on_rescan
    from tools import posting_guard as pg

    stores = make_stores()
    tracked = {canonical(r["jd_url"]): r["pk"] for r in stores.tracking.all() if r.get("jd_url")}
    from tools import seen

    handled = {canonical(u) for u in seen.load()}
    for result in receipts:
        if result.get("error"):
            receipt["errors"].append({"company": result["company"], "error": result["error"]})
            continue
        receipt["companies"] += 1
        receipt["sources"].append(
            {"company": result["company"], "found": len(result.get("jobs", []))}
        )
        with _LOCK:
            data = _read()
            for raw in result.get("jobs", []):
                row = normalize(raw, result["company"], result["provider"],
                                directory_event=(receipt.get("kind") == "network"
                                                 and result.get("kind") == "company"))
                if row is None:
                    continue
                receipt["found"] += 1
                old = data["jobs"].get(row["id"])
                if old:
                    from tools import posting_guard as pg

                    # This is a board rescan, not an authoritative full-JD
                    # re-read: retain existing detail text and bind new findings.
                    merged = pg.merge(
                        old.get("description") or "", row["posting_guard"],
                        old.get("posting_guard"),
                        source_witness=bool(row["posting_guard"].get("source_verified")))
                    row.update(description=merged["text"], posting_guard=merged["posting_guard"])
                    row.update(
                        {
                            k: old[k]
                            for k in (
                                "state",
                                "pk",
                                "first_seen",
                                "job_id",
                                # The tier is paid for once; a rescan must not wipe it.
                                *kept_on_rescan(),
                            )
                            if k in old
                        }
                    )
                    if result["provider"] != "web_search" and not raw.get("search_id"):
                        row.update({k: old[k] for k in ("search_id", "why") if k in old})
                else:
                    receipt["added"] += 1
                if row["url"] in tracked:
                    row.update(state="pipeline", pk=tracked[row["url"]])
                    # New source findings on an already queued job invalidate
                    # any old review without another enqueue or status reopen.
                    try:
                        current = stores.tracking.get_current(row["pk"])
                        if (current and current.get("jd_url") == row["url"]
                                and current.get("company") == row["company"]):
                            witness = bool(row["posting_guard"].get("source_verified"))
                            evidence = pg.merge(
                                current.get("jd_text") or "", row["posting_guard"],
                                current.get("posting_guard"), source_witness=witness)
                            stores.tracking.update_posting_guard(
                                row["pk"], current, evidence, source_witness=witness)
                    except Exception:
                        log.exception("could not persist rescan guard evidence")
                elif row["url"] in handled:
                    row["state"] = "handled"
                data["jobs"][row["id"]] = row
            _write(data)
    return stores


def scan_reserved(company: str = "") -> None:
    global _RUNNING, _ACTIVE
    started = now()
    receipt = {
        "started_at": started,
        "finished_at": None,
        "found": 0,
        "added": 0,
        "companies": 0,
        "errors": [],
        "sources": [],
    }
    try:
        sources = catalog()
        with _LOCK:
            settings = _settings(_read(), sources)
        targets = (
            [c for c in sources if c["name"] == company]
            if company
            else [c for c in sources if c["name"] in settings["companies"]]
        )
        if not targets:
            raise ValueError("Select at least one company in Sources & automation.")
        receipts = _bridge({"entries": targets})
        stores = _save_results(receipts, receipt)
        missing = {c["name"] for c in targets} - {r["company"] for r in receipts}
        receipt["errors"].extend(
            {"company": c, "error": "No completed result. Retry this source."}
            for c in sorted(missing)
        )
        if settings["auto_prepare"]:
            receipt["prepared"] = auto_prepare(stores, started, {c["name"] for c in targets})
    except Exception as exc:
        log.exception("Career Ops search failed")
        receipt["errors"].append({"company": "Search", "error": str(exc)})
    finally:
        report_progress(
            "Search failed — " + receipt["errors"][-1]["error"]
            if receipt["errors"]
            else f"Search complete · {receipt['found']} roles · {receipt['companies']} companies"
        )
        receipt["progress"] = {**progress_snapshot(), "running": False, "active_search": {}}
        receipt["finished_at"] = now()
        try:
            with _LOCK:
                data = _read()
                data["last_scan"] = receipt
                _write(data)
        finally:
            with _LOCK:
                _RUNNING = False
                _ACTIVE = {}
            _SCAN.release()


def search_preferences(company: str = "") -> dict:
    from core import flags
    from discovery.watchlist import load_preferences

    prefs = load_preferences(Path(get_settings().config_dir) / "preferences.yaml")
    if company:
        prefs = flags.effective_prefs(company, prefs)
    return {k: v for k, v in prefs.model_dump().items() if k != "github"}


def search_reserved(
    company: str = "", interests: str | None = None, provider: str | None = None
) -> None:
    global _RUNNING, _ACTIVE
    from discovery.interest_search import search

    started = now()
    receipt = {
        "started_at": started,
        "finished_at": None,
        "found": 0,
        "added": 0,
        "companies": 0,
        "sources": [],
        "errors": [],
        "kind": "web",
        "company": company,
        "summary": "",
        "queries": [],
    }
    try:
        with _LOCK:
            settings = _read()["settings"]
        interests = settings.get("interests", "") if interests is None else interests
        provider = provider or settings.get("search_provider", "claude")
        with _LOCK:
            data = _read()
            data["settings"]["search_provider"] = provider
            _write(data)
        receipt["provider"] = provider
        receipt["interests"] = interests
        result = search(
            search_preferences(company),
            interests,
            company,
            on_progress=report_progress,
            provider=provider,
        )
        receipt.update(summary=result["summary"], queries=result["queries"])
        receipt.update({key: result[key] for key in ("model", "api_searches") if key in result})
        grouped = {}
        from discovery.watchlist import load_watchlist

        known = {
            c.name.casefold(): c.name
            for c in load_watchlist(Path(get_settings().config_dir) / "watchlist.yaml")
        }
        for job in result["jobs"]:
            name = known.get(job["company"].strip().casefold(), job["company"].strip())
            grouped.setdefault(name, []).append({**job, "search_id": started})
        report_progress(f"Saving {len(result['jobs'])} matches to your board…")
        stores = _save_results(
            [{"company": c, "provider": "web_search", "jobs": jobs} for c, jobs in grouped.items()],
            receipt,
        )
        if settings.get("auto_prepare"):
            receipt["prepared"] = auto_prepare(stores, started, set(grouped))
    except Exception as exc:
        log.exception("Career Ops interest search failed")
        receipt["errors"].append({"company": company or "Web search", "error": str(exc)})
    finally:
        report_progress(
            "Search failed — " + receipt["errors"][-1]["error"]
            if receipt["errors"]
            else f"Search complete · {receipt['found']} roles · {receipt['companies']} companies"
        )
        receipt["progress"] = {**progress_snapshot(), "running": False, "active_search": {}}
        receipt["finished_at"] = now()
        try:
            with _LOCK:
                data = _read()
                data["last_search"] = receipt
                _write(data)
        finally:
            with _LOCK:
                _RUNNING = False
                _ACTIVE = {}
            _SCAN.release()


def _index_without_job_id(url: str) -> bool:
    """An invented result ID or last path segment cannot turn an index into a JD."""
    parts = urlsplit(url)
    if _query_job_id(url):
        return False
    path = parts.path.strip("/").casefold()
    leaf = path.rsplit("/", 1)[-1]
    if not path or leaf in {"careers", "career", "jobs", "job", "positions", "openings",
                            "opportunities", "index", "index.html", "search", "job-search",
                            "open-positions", "open-roles", "job-openings", "all-jobs"}:
        return True
    return parts.hostname in {"jobs.ashbyhq.com", "jobs.lever.co", "boards.greenhouse.io",
                              "job-boards.greenhouse.io"} and "/" not in path


def funded_apply_role_handoff(settings, company_id: str, identity: dict,
                              source_url: str, meta: dict, *, now=None) -> dict:
    """Use the existing apply-role admission, not Career Ops auto-prepare.

    A general ATS role must have a real posting identity (not a board index);
    the separately proven official employer CV-form route can use an exact
    employer-hosted form URL. Both need #386 and fresh JD source evidence at
    the existing POST boundary, whose scorer makes the fit decision.
    """
    from core.outreach_sources import SourceHold
    from discovery.handler import funded_apply_role_payload

    if not isinstance(identity, dict) or not isinstance(source_url, str):
        raise SourceHold("funded_application_identity_unverified")
    if identity.get("ats") != "custom" and _index_without_job_id(source_url):
        raise SourceHold("funded_general_posting_identity_missing")
    # Do not trust a provider label or job ID supplied alongside a URL.
    # Use the existing apply-role URL parser, just as the actual POST does.
    # This binds the employer tenant and posting ID to the same witness.
    from server import _manual_role_identity

    parsed = _manual_role_identity(source_url)
    if (not parsed or parsed.get("ats") != identity.get("ats")
            or parsed.get("company") != identity.get("company")
            or (identity.get("job_id") and
                parsed.get("job_id") != identity["job_id"])):
        raise SourceHold("funded_application_identity_unverified")
    return funded_apply_role_payload(
        settings, company_id, parsed, source_url, meta, now=now)


def prepare(ids: list[str], stores=None, *, apply_requested: bool = False,
            continue_check=None) -> dict:
    """Idempotent handoff; only an explicit Apply action authorizes submission."""
    stores = stores or make_stores()
    from core.events import emit
    from core import flags
    from tools import seen

    with _LOCK:
        data = _read()
        if not ids or len(ids) > 50 or any(i not in data["jobs"] for i in ids):
            raise ValueError("Select between 1 and 50 jobs from this board.")
        tracked = {
            canonical(r["jd_url"]): r["pk"] for r in stores.tracking.all() if r.get("jd_url")
        }
        handled = {canonical(u) for u in seen.load()}
        added, duplicates = [], 0
        for identity in dict.fromkeys(ids):
            row = data["jobs"][identity]
            url = row["url"]
            if _index_without_job_id(url):
                continue
            if row["state"] != "new" or url in tracked or url in handled:
                duplicates += 1
                if url in tracked:
                    row.update(state="pipeline", pk=tracked[url])
                elif url in handled:
                    row["state"] = "handled"
                continue
            # These providers' posting URLs carry the same ID as our native ATS
            # adapters, so racing discovery still hits the conditional-create guard.
            path_id = urlsplit(url).path.rstrip("/").split("/")[-1]
            job = JobRecord(
                company=row["company"],
                job_id=_query_job_id(url) or row.get("job_id") or path_id or identity,
                title=row["title"],
                jd_url=url,
                jd_text=row["description"],
                posting_guard=row.get("posting_guard") or {},
                location=row["location"],
                # Cosign is an index, not an ATS; the posting URL names the real one.
                ats=detect_ats(url) if row["provider"] == "cosign" else row["provider"],
                posted_at=row["posted_at"],
                discovery_source="career_ops",
                apply_requested_at=now() if apply_requested else "",
            )
            # Both manual and scheduled Career Ops honor the latest exclusion at
            # the final per-row tracking-create boundary, not just at selection.
            if continue_check is not None and not continue_check():
                break
            if row["company"].strip().lower() in flags.skipped_companies():
                continue
            if stores.tracking.put_new(job):
                # Resume preparation can now happen automatically, even after a
                # restart. The source marker is persisted in the FIRST row write.
                stores.queue.enqueue(stores.tailor_queue, {"pk": job.pk})
                seen.mark([job])
                added.append(job.pk)
                emit(
                    "discovered",
                    pk=job.pk,
                    detail=f"{'Cosign network' if row['provider'] == 'cosign' else 'Career Ops'}: "
                    f"{job.title} @ {job.company}",
                    url=url,
                )
            else:
                duplicates += 1
            row.update(state="pipeline", pk=job.pk)
        _write(data)
    return {"prepared": len(added), "duplicates": duplicates, "pks": added}


def dismiss(ids: list[str]) -> dict:
    with _LOCK:
        data = _read()
        if not ids or len(ids) > 50 or any(i not in data["jobs"] for i in ids):
            raise ValueError("Select between 1 and 50 jobs from this board.")
        for identity in ids:
            if data["jobs"][identity]["state"] == "new":
                data["jobs"][identity]["state"] = "dismissed"
        _write(data)
    return {"ok": True}


def _batch_tracked_urls(stores, search_id: str, since: str) -> set[str]:
    """Existing canonical tracking admissions among persisted logical-batch rows.

    The board can still say "new" after put_new succeeds but before its write.
    Such rows count against the same batch's 50 slots on retry.
    """
    with _LOCK:
        rows = [
            r for r in _read()["jobs"].values()
            if r.get("search_id") == search_id and r.get("last_seen", "") >= since
        ]
    tracked = {
        canonical(r["jd_url"]) for r in stores.tracking.all() if r.get("jd_url")
    }
    return {canonical(r["url"]) for r in rows if canonical(r["url"]) in tracked}


def auto_prepare(stores, since: str, companies: set[str] | None, *,
                 search_id: str | None = None, continue_check=None) -> int:
    from core import flags
    from discovery.crawler import _age_limit
    from discovery.freshness import is_fresh
    from discovery.relevance import relevant
    from discovery.watchlist import load_preferences

    if flags.paused():
        return 0
    with _LOCK:
        data = _read()
        batch_rows = [
            r for r in data["jobs"].values()
            if r.get("last_seen", "") >= since
            and (
                r.get("search_id") == search_id if search_id is not None
                else r["company"] in companies
            )
        ]
    # Keep this check ahead of relevance, which may call a model. prepare()
    # independently checks each company's current skip at conditional creation.
    skipped = flags.skipped_companies()
    rows = [
        r for r in batch_rows
        if r["state"] == "new" and r["company"].strip().lower() not in skipped
    ]
    allowance = 50
    if search_id is not None:
        allowance -= len(_batch_tracked_urls(stores, search_id, since))
    if allowance <= 0:
        return 0

    base = load_preferences(Path(get_settings().config_dir) / "preferences.yaml")
    ids = []
    for company in sorted({r["company"] for r in rows}):
        if continue_check is not None and not continue_check():
            return 0
        age_limit = _age_limit(company)
        keywords = flags.company_filter(company)
        candidates = [
            r
            for r in rows
            if r["company"] == company
            and (not age_limit or is_fresh(r["posted_at"], age_limit))
            and (not keywords or flags.title_matches_filter(r["title"], keywords))
        ]
        prefs = flags.effective_prefs(company, base)
        jobs = [
            JobRecord(
                company=company,
                job_id=r["id"],
                title=r["title"],
                jd_url=r["url"],
                jd_text=r["description"],
                posting_guard=r.get("posting_guard") or {},
                location=r["location"],
            )
            for r in candidates
        ]
        matches = relevant(jobs, prefs)
        ids.extend(j.job_id for j in matches[: prefs.max_new_per_run or 50])
        if len(ids) >= allowance:
            break
    if not ids or (continue_check is not None and not continue_check()):
        return 0
    return prepare(
        ids[:allowance], stores, apply_requested=False, continue_check=continue_check
    )["prepared"]


def scheduled_tick() -> None:
    global _RUNNING, _ACTIVE
    from core import flags

    if flags.paused() or not checkout().exists():
        return
    with _LOCK:
        data = _read()
    settings = data.get("settings") or {}
    current = datetime.fromisoformat(now())
    due = []

    def finished(stamp: str) -> datetime:
        value = datetime.fromisoformat(stamp)
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def elapsed(stamp: str) -> bool:
        return (current - finished(stamp)).total_seconds() >= 6 * 3600

    # Retain the existing first-run policies for interests and watchlist feeds.
    search_last = (data.get("last_search") or {}).get("finished_at")
    if settings.get("scheduled_interests") and search_last and elapsed(search_last):
        due.append((finished(search_last), 1, "interests"))
    if settings.get("scheduled"):
        feed_last = (data.get("last_scan") or {}).get("finished_at")
        if feed_last and elapsed(feed_last):
            due.append((finished(feed_last), 2, "feeds"))
        network_last = (data.get("last_network") or {}).get("finished_at")
        # Missing receipt or an unfinalized crash is immediately eligible. All
        # finalized outcomes, including STOP and errors, wait the full six hours.
        if not network_last:
            due.append((datetime.min.replace(tzinfo=UTC), 0, "network"))
        elif elapsed(network_last):
            due.append((finished(network_last), 0, "network"))

    # An available reservation services one oldest due item. No timestamp is
    # consumed when acquisition or thread start fails.
    for _, _, kind in sorted(due):
        if not reserve_scan(kind, clear_network_stop=(kind == "network")):
            return
        try:
            if kind == "network":
                from discovery import career_network

                worker = threading.Thread(
                    target=career_network.run_reserved,
                    args=(None,),
                    kwargs={"scheduled": True},
                    daemon=True,
                    name="career-network",
                )
            elif kind == "interests":
                worker = threading.Thread(
                    target=search_reserved, daemon=True, name="interest-search"
                )
            else:
                worker = threading.Thread(
                    target=scan_reserved, daemon=True, name="career-ops"
                )
            worker.start()
            return
        except Exception:
            log.exception("Career Ops scheduled %s thread could not start", kind)
            with _LOCK:
                _RUNNING = False
                _ACTIVE = {}
            _SCAN.release()
