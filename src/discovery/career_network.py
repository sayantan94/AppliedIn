"""Resumable reverse-ATS searches feeding AppliedIn's existing discovery inbox."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from discovery import career_ops as co

STOP = threading.Event()
SOURCES = ("greenhouse", "lever", "ashby", "workday", "icims")


def defaults() -> dict:
    prefs = co.search_preferences()
    titles = prefs.get("titles", [])
    # The scanner matches titles by substring, so a preference containing a
    # strictly shorter listed title finds nothing new; drop only those, in
    # original priority order, before the 100-title budget applies.
    folded = [title.casefold() for title in titles]
    positive = [
        title for index, title in enumerate(titles)
        if not any(
            len(other) < len(folded[index]) and other in folded[index]
            for other in folded
        )
    ]
    if len(positive) > 100:
        co.log.warning(
            "Career Ops network titles: %d survive coverage filtering; "
            "using the first 100.",
            len(positive),
        )
        positive = positive[:100]
    return {
        "positive": positive,
        "negative": prefs.get("exclude_keywords", [])[:50],
        "locations": prefs.get("locations", [])[:200],
        "ats": list(SOURCES[:4]),
        "days": 30,
        "include_undated": True,
        "limit": 150,
    }


def _saved_state(data: dict) -> dict:
    """Check persisted identity without normalizing or replacing saved filters."""
    state = data.get("network_search")
    if not isinstance(state, dict):
        raise ValueError("Saved network search is missing or invalid; keep its evidence.")
    filters = state.get("filters")
    if not isinstance(filters, dict):
        raise ValueError("Saved network search filters are missing or invalid.")
    for key, maximum in (("positive", 100), ("negative", 50), ("locations", 200)):
        values = filters.get(key)
        if (not isinstance(values, list) or len(values) > maximum
                or any(not isinstance(item, str) for item in values)):
            raise ValueError(f"Saved network {key} filters are invalid.")
    ats = filters.get("ats")
    if (not isinstance(ats, list) or not 1 <= len(ats) <= 5
            or any(not isinstance(source, str) or source not in SOURCES for source in ats)
            or len(set(ats)) != len(ats)):
        raise ValueError("Saved network ATS selection is invalid.")
    if (type(filters.get("days")) is not int or not 1 <= filters["days"] <= 365
            or type(filters.get("limit")) is not int or not 0 <= filters["limit"] <= 10000
            or type(filters.get("include_undated")) is not bool):
        raise ValueError("Saved network date, depth or undated filters are invalid.")
    if ("provider" in filters and not isinstance(filters["provider"], str)) or (
        "interests" in filters and (
            not isinstance(filters["interests"], str) or len(filters["interests"]) > 600
        )
    ):
        raise ValueError("Saved network provider or interests filters are invalid.")
    if not isinstance(state.get("id"), str) or not state["id"]:
        raise ValueError("Saved network search ID is missing.")
    if not isinstance(state.get("cutoff"), str):
        raise ValueError("Saved network cutoff is missing.")
    try:
        datetime.fromisoformat(state["cutoff"])
    except ValueError as exc:
        raise ValueError("Saved network cutoff is invalid.") from exc
    cursor = state.get("cursor")
    if not isinstance(cursor, dict) or any(
        not isinstance(entry, dict)
        or not isinstance(entry.get("hash"), str)
        or type(entry.get("next")) is not int or entry["next"] < 0
        for entry in cursor.values()
    ):
        raise ValueError("Saved network cursor is missing or invalid.")
    if not isinstance(state.get("coverage", {}), dict):
        raise ValueError("Saved network coverage is invalid.")
    last = data.get("last_network") or {}
    if last.get("search_id") and last["search_id"] != state["id"]:
        raise ValueError("Saved network search ID conflicts with its receipt.")
    if last.get("filters") and last["filters"] != filters:
        raise ValueError("Saved network filters conflict with their receipt.")
    return state


def scheduled_plan(data: dict) -> tuple[dict, bool]:
    """Exact stored filters; a finished successful directory begins a new search."""
    previous = data.get("network_search")
    last = data.get("last_network") or {}
    if previous is None:
        if last and not (
            last.get("finished_at") and last.get("complete")
            and not last.get("scan_error") and not last.get("stopped")
            and not last.get("prepare_error")
        ):
            raise ValueError("Unfinished network receipt has no saved search to resume.")
        return defaults(), False
    state = _saved_state(data)
    finished = bool(
        last.get("finished_at") and last.get("complete")
        and last.get("batch_success", True)
        and not last.get("stopped") and not last.get("scan_error")
        and not last.get("prepare_error")
    )
    return state["filters"], not finished


def stop() -> dict:
    # prepare() holds the board lock through tracking/queue/seen admissions.
    # Signal first, while that lock may be held, so its next per-row
    # continue_check can observe Stop. The scan reservation and active kind
    # exclude unrelated Career Ops work without a second lock.
    if not (co._SCAN.locked() and co._RUNNING
            and co._ACTIVE.get("kind") == "network"):
        return {"stopping": False}
    STOP.set()
    # Progress still uses the board lock; never block *signaling* on it.
    with co._LOCK:
        if co._RUNNING and co._ACTIVE.get("kind") == "network":
            co.report_progress(
                "Stopping scan; completed results are saved. You can continue later."
            )
    return {"stopping": True}


# A full directory scan including Workday (12,000+ tenants, one site each) runs
# for hours; at 30 minutes it stopped after 31 Workday companies.
SCAN_TIMEOUT_S = 5 * 60 * 60


def events(payload: dict, timeout: int = SCAN_TIMEOUT_S):
    """Drain output incrementally; timeout/stop cannot erase completed companies."""
    script = co.ROOT / "scripts/integrations/career-network.mjs"
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(
            ["node", str(script), str(co.checkout())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        selector = selectors.DefaultSelector()
        try:
            proc.stdin.write(json.dumps(payload).encode())
            proc.stdin.close()
            selector.register(proc.stdout, selectors.EVENT_READ)
            started, terminating, buffer = time.monotonic(), None, b""
            while selector.get_map():
                elapsed = time.monotonic() - started
                if terminating is None and (STOP.is_set() or elapsed > timeout):
                    terminating = time.monotonic()
                    proc.terminate()
                if terminating and time.monotonic() - terminating > 15 and proc.poll() is None:
                    proc.kill()
                for key, _ in selector.select(0.5):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line.strip():
                            yield json.loads(line)
            proc.wait(timeout=5)
            if terminating:
                yield {
                    "kind": "interrupted",
                    "message": "Scan stopped"
                    if STOP.is_set()
                    else f"Scan reached its {timeout // 3600}-hour time limit",
                }
            elif proc.returncode:
                errors.seek(0)
                detail = errors.read(1500).decode(errors="replace")
                raise ValueError(f"Career Ops scanner failed: {detail or proc.returncode}")
        finally:
            selector.close()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()


def run_reserved(filters: dict | None, resume: bool = False, *,
                 scheduled: bool = False) -> None:
    started = co.now()
    receipt = {
        "kind": "network",
        "started_at": started,
        "finished_at": None,
        "found": 0,
        "added": 0,
        "companies": 0,
        "sources": [],
        "errors": [],
        "coverage": {},
        "read": 0,
        "undated": 0,
        "old": 0,
        "filtered": 0,
        "unreachable": 0,
        "capped": 0,
        "filters": filters,
        "batch_success": False,
        "complete": False,
        "prepared": 0,
        "stopped": False,
    }
    state = {}
    done = False
    timed_out = False
    source_seen = set()
    try:
        with co._LOCK:
            data = co._read()
            previous = data.get("network_search")
            old = data.get("last_network") or {}
            # Preserve the logical batch identity before validation: an invalid
            # cursor, cutoff or saved filter can reject scheduled_plan/_saved_state.
            # A failed receipt must not lose pre-crash row membership.
            if scheduled or resume:
                if isinstance(previous, dict) and previous.get("id"):
                    receipt["search_id"] = previous["id"]
                if old and not (
                    old.get("finished_at") and old.get("batch_success")
                    and not old.get("stopped") and not old.get("scan_error")
                    and not old.get("prepare_error")
                ):
                    receipt["started_at"] = old.get("started_at") or started
            if scheduled:
                filters, resume = scheduled_plan(data)
            if not isinstance(filters, dict):
                raise ValueError("Network filters are missing.")
            receipt["filters"] = filters
            if resume:
                previous = _saved_state(data)
                if previous["filters"] != filters:
                    raise ValueError(
                        "Search filters changed. Start a new scan to use these filters."
                    )
                state = previous
                # A finalized successful partial starts a fresh logical batch.
                # An unfinalized crash, STOP/error, or failed handoff retains it.
                if not (
                    old.get("finished_at") and old.get("batch_success")
                    and not old.get("stopped") and not old.get("scan_error")
                    and not old.get("prepare_error")
                ):
                    receipt["started_at"] = old.get("started_at") or state["id"]
            else:
                state = {
                    "id": started,
                    "filters": filters,
                    "cursor": {},
                    "cutoff": (datetime.now(UTC) - timedelta(days=filters["days"])).isoformat(),
                }
            receipt["search_id"] = state["id"]
            receipt["coverage"] = dict(state.get("coverage", {}))
            data["network_search"] = state
            data["last_network"] = receipt
            # This unfinished receipt is durable before the scanner reads anything.
            co._write(data)
        payload = {
            **filters,
            "cursor": state["cursor"],
            "cutoff": state["cutoff"],
            "cache_dir": str(co._path().parent / "career-ops-cache"),
        }
        from discovery.watchlist import load_watchlist

        known = {
            c.name.casefold(): c.name
            for c in load_watchlist(Path(co.get_settings().config_dir) / "watchlist.yaml")
        }
        # The chosen client searches first; directory expansion then catches
        # postings that the search index missed. Both write to the same inbox.
        if not resume and filters.get("provider"):
            from functools import partial

            from discovery.interest_search import search

            prefs = {
                **co.search_preferences(),
                "titles": filters["positive"],
                "locations": filters["locations"],
                "exclude_keywords": filters["negative"],
            }
            try:
                if filters["provider"] == "codex":
                    from discovery.codex_search import run_search
                elif filters["provider"] == "claude":
                    from discovery.claude_search import run_search
                elif filters["provider"] == "opencode":
                    from discovery.opencode_search import run_search
                else:
                    raise ValueError("Choose Claude, Codex or OpenCode for AI search.")
                result = search(
                    prefs,
                    filters.get("interests", ""),
                    provider=filters["provider"],
                    ats=filters["ats"],
                    runner=partial(run_search, cancelled=STOP.is_set),
                    on_progress=co.report_progress,
                )
                grouped = {}
                for job in result["jobs"]:
                    company = known.get(job["company"].casefold(), job["company"])
                    grouped.setdefault(company, []).append({**job, "search_id": state["id"]})
                co._save_results(
                    [
                        {"company": company, "provider": "web_search", "jobs": jobs}
                        for company, jobs in grouped.items()
                    ],
                    receipt,
                )
                receipt["queries"] = result["queries"]
                receipt.update({key: result[key] for key in ("model", "api_searches") if key in result})
            except Exception as exc:
                if not STOP.is_set():
                    receipt["errors"].append({"company": filters["provider"], "error": str(exc)})
                    co.report_progress(
                        f"{filters['provider']} search unavailable: {exc}. "
                        "Continuing with job boards."
                    )
        if STOP.is_set():
            receipt["stopped"] = True
            return
        co.report_progress("Reading public ATS directories · no AI tokens used")
        for event in events(payload):
            kind = event["kind"]
            if kind == "source":
                name = event["source"]
                source_seen.add(name)
                receipt["coverage"][name] = {k: event[k] for k in ("total", "next", "status")}
                state["cursor"][name] = {"hash": event["hash"], "next": event["next"]}
                co.report_progress(
                    f"{name}: {event['total']:,} company boards · continuing at {event['next']:,}"
                )
                if event["status"] != "ok":
                    receipt["errors"].append(
                        {"company": name, "error": f"Directory {event['status']}"}
                    )
            elif kind == "company":
                event["company"] = known.get(event["company"].casefold(), event["company"])
                name = event["source"]
                state["cursor"][name] = event["cursor"]
                receipt["coverage"][name]["next"] = event["cursor"]["next"]
                for field in ("read", "undated", "old", "filtered", "capped"):
                    receipt[field] += event[field]
                if event["error"]:
                    receipt["unreachable"] += 1
                    # Keep the receipt readable on a directory with thousands of
                    # retired boards; the count still includes every failure.
                    if len(receipt["errors"]) < 12:
                        receipt["errors"].append(
                            {"company": event["company"], "error": event["error"]}
                        )
                if event["jobs"]:
                    for job in event["jobs"]:
                        job["search_id"] = state["id"]
                    co._save_results([event], receipt)
                else:
                    receipt["companies"] += 1
                co.report_progress(
                    f"{event['company']}: {event['read']} postings read "
                    f"· {len(event['jobs'])} matches"
                    + (f" · {event['error']}" if event["error"] else "")
                )
            elif kind in ("notice", "interrupted"):
                co.report_progress(event["message"] + "; completed matches saved.")
                if kind == "interrupted":
                    if STOP.is_set():
                        receipt["stopped"] = True
                    else:
                        # A scanner deadline is an unfinished batch, not a six-hour receipt.
                        timed_out = True
                        receipt["scan_error"] = event["message"]
            elif kind == "done":
                done = not event.get("stopped")
                receipt["stopped"] = bool(event.get("stopped"))
            state["coverage"] = receipt["coverage"]
            with co._LOCK:
                co._ACTIVE["coverage"] = receipt["coverage"]
                co._ACTIVE["found"] = receipt["found"]
                co._ACTIVE["read"] = receipt["read"]
                # Flush checkpoints periodically, and after matches. URL dedup
                # makes the small overlap after an abrupt restart harmless.
                if kind != "company" or event["jobs"] or receipt["companies"] % 10 == 0:
                    data = co._read()
                    data.update(network_search=state, last_network=receipt)
                    co._write(data)
        if STOP.is_set():
            receipt["stopped"] = True
        if not done and not receipt.get("stopped") and not receipt.get("scan_error"):
            receipt["scan_error"] = "Scanner finished without a successful done event."
        missing = set(filters["ats"]) - source_seen
        if missing and not receipt.get("scan_error"):
            receipt["scan_error"] = "Missing directory source: " + ", ".join(sorted(missing))
        unhealthy = [
            name for name in filters["ats"]
            if receipt["coverage"].get(name, {}).get("status") != "ok"
        ]
        if unhealthy and not receipt.get("scan_error"):
            receipt["scan_error"] = "Unhealthy directory source: " + ", ".join(unhealthy)
        receipt["batch_success"] = bool(
            done and not receipt["stopped"] and not receipt.get("scan_error")
        )
        # Directory coverage is a diagnostic, never a preparation prerequisite.
        receipt["complete"] = receipt["batch_success"] and all(
            receipt["coverage"][name]["next"] >= receipt["coverage"][name]["total"]
            for name in filters["ats"]
        )
    except TimeoutError as exc:
        # Preserve the batch cursor for the very next scheduled tick.
        timed_out = True
        receipt["scan_error"] = str(exc) or "Network scanner timed out"
        receipt["errors"].append({"company": "Scanner", "error": receipt["scan_error"]})
        co.log.warning("Career Ops network scan timed out: %s", receipt["scan_error"])
    except Exception as exc:
        co.log.exception("Career Ops network scan failed")
        receipt["scan_error"] = str(exc)
        receipt["errors"].append({"company": "Scanner", "error": str(exc)})
    finally:
        try:
            # Classify saved rows before admitting anything. Model-label failure
            # remains best-effort and never upgrades a failed scanner batch.
            if state.get("id"):
                try:
                    from discovery.career_fit import classify_search

                    classify_search(state["id"])
                except Exception:
                    co.log.exception("Career domain classification failed")

            if receipt["batch_success"]:
                try:
                    from core import flags

                    with co._LOCK:
                        enabled = bool(
                            (co._read().get("settings") or {}).get("auto_prepare")
                        )
                    if STOP.is_set():
                        receipt["stopped"] = True
                    elif enabled and not flags.paused():
                        stores = co.make_stores()
                        before = co._batch_tracked_urls(
                            stores, state["id"], receipt["started_at"]
                        )
                        try:
                            receipt["prepared"] = co.auto_prepare(
                                stores, receipt["started_at"], None,
                                search_id=state["id"],
                                continue_check=lambda: not STOP.is_set()
                                and not flags.paused(),
                            )
                        except Exception as exc:
                            receipt["prepare_error"] = str(exc)
                            co.log.exception("Career Ops network preparation failed")
                            # A put_new may have succeeded just before an exception.
                            try:
                                after = co._batch_tracked_urls(
                                    stores, state["id"], receipt["started_at"]
                                )
                                receipt["prepared"] = len(after - before)
                            except Exception:
                                co.log.exception("Could not count partial network admissions")
                        if not STOP.is_set() and flags.paused():
                            receipt["prepare_error"] = receipt.get(
                                "prepare_error", "Preparation paused during handoff."
                            )
                    if STOP.is_set():
                        receipt["stopped"] = True
                except Exception as exc:
                    receipt["prepare_error"] = str(exc)
                    co.log.exception("Career Ops network handoff failed")
            if receipt["stopped"]:
                # A STOP while preparing is not a completed handoff.
                receipt["batch_success"] = False
                receipt["complete"] = False
            # Only deadlines stay unfinalized; STOP and ordinary failures retain
            # the existing six-hour cooling period.
            receipt["finished_at"] = None if timed_out else co.now()
            co.report_progress(
                f"{receipt['found']} matches saved · {receipt['companies']} company boards checked"
                + (
                    f" · scan failed: {receipt['scan_error']}"
                    if receipt.get("scan_error")
                    else " · preparation failed"
                    if receipt.get("prepare_error")
                    else " · scan stopped"
                    if receipt["stopped"]
                    else " · directory scan finished"
                    if receipt["complete"]
                    else " · partial scan; continue to check more"
                )
            )
            receipt["progress"] = {
                **co.progress_snapshot(), "running": False, "active_search": {}
            }
            with co._LOCK:
                data = co._read()
                # Never replace orphaned/invalid saved search state with defaults.
                data["last_network"] = receipt
                if state:
                    data["network_search"] = state
                co._write(data)
        except Exception:
            co.log.exception("Could not finalize Career Ops network receipt")
        finally:
            with co._LOCK:
                co._RUNNING = False
                co._ACTIVE = {}
            co._SCAN.release()
