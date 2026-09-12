"""Career Ops feed discovery, staged separately from approved applications.

The upstream checkout is optional and pinned. Only its HTTP provider modules run;
its prompts, title preferences and submission workflow never own AppliedIn state.
"""

from __future__ import annotations

import hashlib
import json
import logging
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
        raise ValueError("Career Ops is not installed. Run ./appliedin start to install it.")
    revision = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if revision != REVISION:
        raise ValueError(
            "Career Ops version changed. Restore the pinned revision in docs/career-ops.md."
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


def normalize(raw: dict, company: str, provider: str) -> dict | None:
    url, title = str(raw.get("url") or ""), str(raw.get("title") or "").strip()
    if urlsplit(url).scheme != "https" or not urlsplit(url).hostname or not title:
        return None
    url = canonical(url)
    from discovery.adapters.ashby import _iso

    identity = hashlib.sha256(url.encode()).hexdigest()[:24]
    return {
        "id": identity,
        "job_id": str(
            raw.get("id")
            or dict(parse_qsl(urlsplit(url).query)).get("gh_jid")
            or urlsplit(url).path.rstrip("/").split("/")[-1]
            or identity
        ),
        "company": company,
        "title": title,
        "url": url,
        "location": str(raw.get("location") or ""),
        "provider": provider,
        "description": ""
        if provider == "oraclecloud"
        or (provider == "web_search" and raw.get("verification") != "verified")
        else str(raw.get("description") or "")[:100000],
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
    rows = [{k: v for k, v in j.items() if k != "description"} for j in data["jobs"].values()]
    return {
        "sources": sources,
        "settings": _settings(data, sources),
        "jobs": rows,
        "last_scan": data.get("last_scan"),
        "last_search": data.get("last_search"),
        "active_search": dict(_ACTIVE),
        "progress": progress_snapshot(),
        "preferences": search_preferences(),
        "running": _RUNNING,
        "paused": flags.paused(),
        "error": error,
        "revision": REVISION[:12],
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
        for key in ("interests", "scheduled_interests"):
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


def reserve_scan(kind: str = "feeds", company: str = "") -> bool:
    global _RUNNING, _ACTIVE
    if not _SCAN.acquire(blocking=False):
        return False
    with _LOCK:
        _RUNNING = True
        _ACTIVE = {"kind": kind, "company": company, "started_at": now()}
        _PROGRESS.update(run_id=_ACTIVE["started_at"], events=[])
        report_progress("Search started" + (f" · {company}" if company else ""))
    return True


def _save_results(receipts: list[dict], receipt: dict):
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
                row = normalize(raw, result["company"], result["provider"])
                if row is None:
                    continue
                receipt["found"] += 1
                old = data["jobs"].get(row["id"])
                if old:
                    row.update(
                        {k: old[k] for k in ("state", "pk", "first_seen", "job_id") if k in old}
                    )
                    if result["provider"] != "web_search":
                        row.update({k: old[k] for k in ("search_id", "why") if k in old})
                else:
                    receipt["added"] += 1
                if row["url"] in tracked:
                    row.update(state="pipeline", pk=tracked[row["url"]])
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


def search_reserved(company: str = "", interests: str | None = None) -> None:
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
        receipt["interests"] = interests
        result = search(
            search_preferences(company), interests, company, on_progress=report_progress
        )
        receipt.update(summary=result["summary"], queries=result["queries"])
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


def prepare(ids: list[str], stores=None) -> dict:
    """Idempotent handoff to scoring/tailoring. Approval remains mandatory."""
    stores = stores or make_stores()
    from core.events import emit
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
                job_id=row.get("job_id") or path_id or identity,
                title=row["title"],
                jd_url=url,
                jd_text=row["description"],
                location=row["location"],
                ats=row["provider"],
                posted_at=row["posted_at"],
                discovery_source="career_ops",
            )
            if stores.tracking.put_new(job):
                # Resume preparation can now happen automatically, even after a
                # restart. The source marker is persisted in the FIRST row write.
                stores.queue.enqueue(stores.tailor_queue, {"pk": job.pk})
                seen.mark([job])
                added.append(job.pk)
                emit(
                    "discovered",
                    pk=job.pk,
                    detail=f"Career Ops: {job.title} @ {job.company}",
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


def auto_prepare(stores, since: str, companies: set[str]) -> int:
    from core import flags
    from discovery.crawler import _age_limit
    from discovery.freshness import is_fresh
    from discovery.relevance import relevant
    from discovery.watchlist import load_preferences

    if flags.paused():
        return 0
    with _LOCK:
        data = _read()
        rows = [
            r
            for r in data["jobs"].values()
            if r["state"] == "new" and r["company"] in companies and r["last_seen"] >= since
        ]
    base = load_preferences(Path(get_settings().config_dir) / "preferences.yaml")
    ids = []
    for company in sorted({r["company"] for r in rows}):
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
                location=r["location"],
            )
            for r in candidates
        ]
        matches = relevant(jobs, prefs)
        ids.extend(j.job_id for j in matches[: prefs.max_new_per_run or 50])
        if len(ids) >= 50:
            break
    return prepare(ids[:50], stores)["prepared"] if ids else 0


def scheduled_tick() -> None:
    from core import flags

    if flags.paused() or not checkout().exists():
        return
    with _LOCK:
        data = _read()
    search_last = (data.get("last_search") or {}).get("finished_at")
    if (
        data["settings"].get("scheduled_interests")
        and search_last
        and (datetime.now(UTC) - datetime.fromisoformat(search_last)).total_seconds() >= 6 * 3600
    ):
        if reserve_scan("interests"):
            threading.Thread(target=search_reserved, daemon=True, name="interest-search").start()
        return
    if not data["settings"]["scheduled"]:
        return
    last = (data.get("last_scan") or {}).get("finished_at")
    # Like native discovery, the first automatic scan waits for its schedule.
    if not last or (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() < 6 * 3600:
        return
    if reserve_scan():
        threading.Thread(target=scan_reserved, daemon=True, name="career-ops").start()
