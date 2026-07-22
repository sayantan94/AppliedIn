"""Local web server — serves the dashboard UI + a data API over the store.

In local mode the daemon starts this: it serves the static `web/` dashboard on
localhost and exposes the same endpoints the dashboard calls (`/applications`,
`/stats`, `/actions/*`), reading the Redis-backed tracking store. In cloud the
dashboard is on Vercel and hits an API Gateway with the same routes over
DynamoDB — same contract, different host.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.config import get_settings
from core.models import Status
from core.stores import make_stores

_WEB = Path(__file__).resolve().parents[1] / "web"

# In-process flags so the dashboard can show whether a manual run is active and
# grey out its button. Background tasks run in this same process, so a plain dict
# is enough — no need to round-trip through Redis for a transient UI hint.
_RUNNING = {"discover": False, "process": False}


def _closed_reason(row: dict) -> str | None:
    """A plain sentence for why a job is in a terminal state — so the UI can say
    WHY something closed instead of just showing a grey tag."""
    status = row.get("status", "")
    if status == "skipped":
        reason, score = row.get("skip_reason"), row.get("match_score")
        if reason == "low_score":
            return f"Match score {score}/10 was below the bar — skipped before tailoring."
        if reason == "user_skipped":
            return "You skipped this one."
        return f"Skipped: {reason}." if reason else "Skipped."
    if status == "job_gone":
        return "The posting was taken down before we could apply."
    if status == "failed":
        return row.get("fail_reason") or "The application could not be submitted."
    if status == "error":
        return row.get("error") or "Hit an error during the pipeline (see the logs)."
    if status == "capped":
        return "Daily application cap reached — parked for the next run."
    return None


def _is_job_row(row: dict) -> bool:
    """Real application rows only. Internal bookkeeping (crawl watermarks,
    `meta#…`) shares the tracking table but isn't a job — hide it from the board
    so it doesn't render as a blank card or inflate the counts."""
    return not str(row.get("pk") or "").startswith("meta#")


def _to_ui(row: dict, artifacts) -> dict:
    """Map a tracking row to the shape the dashboard expects."""
    def link(key):
        # Serve artifacts same-origin (/artifact/<key>) so the browser can load
        # them — a local file:// presign can't be shown from an http page. Keys
        # contain '#' (pk = company#job_id), so URL-encode it (# starts a
        # fragment) while keeping the path slashes.
        from urllib.parse import quote

        k = row.get(key)
        return f"/artifact/{quote(k, safe='/')}" if k else None

    events = row.get("events") or []
    return {
        "pk": row.get("pk"),
        "company": row.get("company", ""),
        "title": row.get("title", ""),
        "status": row.get("status", ""),
        "match_score": row.get("match_score"),
        "gate_reason": row.get("gate_reason"),
        "fail_kind": row.get("fail_kind") or "",
        "gate_question": (row.get("gate_pending") or {}).get("question"),
        "skip_reason": row.get("skip_reason"),
        "closed_reason": _closed_reason(row),
        "resume_version": row.get("resume_version"),
        "resume_url": link("resume_s3_key"),
        "has_diff": bool(row.get("resume_tex_key")),
        "jd_url": row.get("jd_url"),
        "screenshot_url": link("screenshot_s3_key"),
        "confirmation_id": row.get("confirmation_id"),
        "discovered_at": row.get("discovered_at") or (events[0]["at"] if events else None),
        "updated_at": (events[-1]["at"] if events else None),
        "timeline": [{"label": e.get("detail") or e.get("step"), "at": e.get("at"), "done": True}
                     for e in events],
        "fields": row.get("fields") or [],
    }


def create_app() -> FastAPI:
    app = FastAPI(title="AppliedIn")
    settings = get_settings()

    @app.get("/config.js")
    def config_js():
        # Live config: same-origin API, real data (not demo). Overrides web/config.js.
        return Response('window.APPLIEDIN_CONFIG = {demo:false, env:"local", apiUrl:""};\n',
                        media_type="application/javascript")

    @app.get("/applications")
    def applications():
        stores = make_stores(settings)
        rows = [r for r in stores.tracking.all() if _is_job_row(r)]
        return {"items": [_to_ui(r, stores.artifacts) for r in rows]}

    @app.get("/stats")
    def stats():
        stores = make_stores(settings)
        counts: dict[str, int] = {}
        for r in stores.tracking.all():
            if not _is_job_row(r):
                continue
            counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
        applied = counts.get("applied", 0) + counts.get("applied_manual", 0)
        from core import flags
        return {"today_submitted": applied,
                "llm_error": flags.llm_error(),
                "queue_age_seconds": None, "paused": flags.paused(),
                "apply_mode": flags.apply_mode(),
                "auto_min_score": settings.auto_min_score,
                "counts_by_status": counts,
                "discovering": _RUNNING["discover"], "processing": _RUNNING["process"],
                # `found` = discovered but not yet processed; the number the
                # 'Process applications' button will act on.
                "found_waiting": counts.get("found", 0)}

    @app.get("/companies")
    def companies():
        """The watchlist company names — so the dashboard can offer a picker and
        run discovery for just the selected companies instead of all of them."""
        from core import flags
        from discovery.handler import list_watchlist_companies
        return {"companies": list_watchlist_companies(),
                "skipped": sorted(flags.skipped_companies()),
                "filters": flags.company_filters()}

    @app.post("/actions/clear-llm-error")
    def clear_llm_error():
        """Dismiss the top-level LLM-failure banner."""
        from core import flags
        flags.set_flag("llm_error", "")
        return {"ok": True}

    @app.post("/actions/skip-company")
    def skip_company(body: dict):
        """Toggle a company's skip state. Skipped companies sit out un-scoped
        Discover/Process runs; explicitly picking one in the UI overrides."""
        from core import flags
        name = ((body or {}).get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        skipped = flags.set_company_skip(name, bool((body or {}).get("skip")))
        return {"ok": True, "skipped": sorted(skipped)}

    @app.post("/actions/run-company")
    def run_company(body: dict, background: BackgroundTasks):
        """One-company end-to-end workflow: (add to watchlist if new) → discover
        just that company → score + tailor its findings. In gated apply mode the
        run STOPS at tailored/approval — nothing is submitted."""
        name = ((body or {}).get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        if _RUNNING["discover"] or _RUNNING["process"]:
            return {"ok": False, "status": "already_running"}
        careers_url = ((body or {}).get("careers_url") or "").strip()
        from discovery.handler import add_watchlist_company, list_watchlist_companies
        if name.lower() not in (c.lower() for c in list_watchlist_companies()):
            added = add_watchlist_company(name, careers_url)
            if not added.get("ok"):
                return added

        def _run() -> None:
            import logging
            from core.events import emit
            from daemon import process_backlog_once
            from discovery.handler import run_discovery
            _RUNNING["discover"] = True
            try:
                emit("running", agent="workflow", company=name,
                     pk=f"meta#run#{name.lower()}",
                     detail=f"▶ {name}: one-company run — discovering…")
                found = run_discovery(only=[name])
                n_new = (found.get("enqueued") or 0) + (found.get("crawled") or 0)
                emit("running", agent="workflow", company=name,
                     pk=f"meta#run#{name.lower()}",
                     detail=f"{name}: discovery finished — {n_new} new posting(s); "
                            f"scoring + tailoring the backlog…")
            except Exception:
                logging.getLogger("server").exception("run-company discover failed")
            finally:
                _RUNNING["discover"] = False
            _RUNNING["process"] = True
            try:
                process_backlog_once(companies=[name])
                emit("applied", agent="workflow", company=name,
                     pk=f"meta#run#{name.lower()}",
                     detail=f"{name}: one-company run complete — tailored jobs "
                            f"await your approval on the board")
            except Exception:
                logging.getLogger("server").exception("run-company process failed")
            finally:
                _RUNNING["process"] = False

        background.add_task(_run)
        return {"ok": True, "status": "running", "company": name}

    @app.post("/actions/company-filter")
    def company_filter(body: dict):
        """Set a company's title filter (e.g. Rivian -> only 'Staff' titles).
        `titles` may be a list or a comma/newline-separated string; empty clears
        it. Retroactively skips already-found jobs that no longer match, and
        un-skips ones that do — so the board reflects the filter immediately."""
        from core import flags
        name = ((body or {}).get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        raw = (body or {}).get("titles", [])
        kws = ([k for k in raw] if isinstance(raw, list)
               else [k for k in raw.replace("\n", ",").split(",")])
        kws = [k.strip() for k in kws if k and k.strip()]
        flags.set_company_filter(name, kws)

        # Reconcile the current backlog for this company.
        stores = make_stores(settings)
        low = name.lower()
        moved = 0
        for r in stores.tracking.all():
            if (r.get("company") or "").lower() != low:
                continue
            st, title = r.get("status"), r.get("title") or ""
            ok = flags.title_matches_filter(title, kws)
            if st == "found" and not ok:
                stores.tracking.set_status(r["pk"], Status.SKIPPED,
                                           skip_reason="title_filter")
                moved += 1
            elif st == "skipped" and r.get("skip_reason") == "title_filter" and ok:
                stores.tracking.set_status(r["pk"], Status.FOUND, skip_reason="")
                moved += 1
        return {"ok": True, "filters": flags.company_filters(), "reconciled": moved}

    @app.post("/actions/run-job/{pk}")
    def run_one_job(pk: str, background: BackgroundTasks):
        """Run ONE found/tailored job through score + tailor right now (the card's
        'Run now'). Gated apply mode stops it at ready-to-apply."""
        row = make_stores(settings).tracking.get(pk)
        if not row:
            return {"ok": False, "error": "unknown job"}

        def _run() -> None:
            import logging
            from agent.run import run_job
            try:
                run_job(pk, make_stores(settings))
            except Exception:
                logging.getLogger("server").exception("run-job failed for %s", pk)

        background.add_task(_run)
        return {"ok": True, "status": "running", "pk": pk}

    @app.post("/actions/apply-role")
    def apply_role(body: dict, background: BackgroundTasks):
        """Single-role workflow: paste a job URL (Greenhouse/Lever/Ashby/…) — we
        create the row, fetch the JD, score + tailor a résumé to it, and (in
        gated mode) stop at 'ready to apply' so you approve the submit. No
        discovery, no watchlist needed — one role, straight to a tailored résumé."""
        import hashlib
        from urllib.parse import urlparse

        url = ((body or {}).get("url") or "").strip()
        if url and not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if not url:
            return {"ok": False, "error": "a job URL is required"}
        company = ((body or {}).get("company") or "").strip()
        title = ((body or {}).get("title") or "").strip() or "Role from URL"
        if not company:  # infer from the host (jobs.ashbyhq.com/rivian → rivian)
            host = urlparse(url).hostname or ""
            parts = [p for p in urlparse(url).path.split("/") if p]
            company = (parts[0] if "ashbyhq" in host or "greenhouse" in host or "lever" in host
                       else host.split(".")[-2] if "." in host else host) or "Company"
            company = company.replace("-", " ").title()

        from core.models import JobRecord
        job_id = hashlib.sha1(url.encode()).hexdigest()[:12]
        stores = make_stores(settings)
        job = JobRecord(company=company, job_id=job_id, title=title,
                        jd_url=url, jd_text=title, ats="custom")
        pk = job.pk
        is_new = stores.tracking.put_new(job)
        if not is_new:  # already tracked — re-tailor it from scratch
            stores.tracking.set_status(pk, Status.FOUND, skip_reason="", fail_kind="")

        def _run() -> None:
            import logging
            from agent.run import run_job
            from core.events import emit
            _RUNNING["process"] = True
            try:
                emit("running", agent="workflow", company=company, pk=pk,
                     detail=f"▶ single-role: scoring + tailoring for {title} @ {company}…")
                run_job(pk, stores)
                emit("applied", agent="workflow", company=company, pk=pk,
                     detail=f"{company}: résumé tailored — approve on the board to apply")
            except Exception:
                logging.getLogger("server").exception("apply-role failed")
            finally:
                _RUNNING["process"] = False

        background.add_task(_run)
        return {"ok": True, "status": "running", "pk": pk, "company": company, "title": title}

    @app.post("/actions/watchlist")
    def watchlist_add(body: dict):
        """Add a company to the watchlist (name + optional careers URL). The
        finder auto-resolves its ATS/board on the first discovery run."""
        from discovery.handler import add_watchlist_company
        return add_watchlist_company((body or {}).get("name", ""),
                                     (body or {}).get("careers_url", ""))

    @app.post("/actions/discover")
    def discover(background: BackgroundTasks, body: dict | None = None):
        """Run ONE discovery cycle now — find + enqueue new jobs as `found`.
        Body may carry `{"companies": ["Ramp", ...]}` to scope the run to the
        picked companies; omit/empty = the whole watchlist. Discover-only: it does
        not score, tailor, or apply (that's /actions/process)."""
        if _RUNNING["discover"]:
            return {"ok": False, "status": "already_running"}
        only = [c for c in ((body or {}).get("companies") or []) if isinstance(c, str)]

        def _run() -> None:
            from daemon import run_discovery_once
            _RUNNING["discover"] = True
            try:
                run_discovery_once(only=only or None)
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger("server").exception("manual discovery failed")
            finally:
                _RUNNING["discover"] = False

        background.add_task(_run)
        return {"ok": True, "status": "discovering",
                "companies": only or "all"}

    @app.post("/actions/process")
    def process(background: BackgroundTasks, body: dict | None = None):
        """Process the discovered backlog now — score + tailor every `found` job,
        then apply the ones that qualify (up to the daily cap). One full pass.
        Body may carry {companies:[names]} to run the pass on JUST those
        companies' jobs — the rest of the backlog stays untouched for later."""
        if _RUNNING["process"]:
            return {"ok": False, "status": "already_running"}
        companies = [c for c in ((body or {}).get("companies") or [])
                     if isinstance(c, str) and c.strip()]

        def _run() -> None:
            from daemon import process_backlog_once
            _RUNNING["process"] = True
            try:
                process_backlog_once(companies=companies)
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger("server").exception("manual process failed")
            finally:
                _RUNNING["process"] = False

        background.add_task(_run)
        return {"ok": True, "status": "processing", "companies": companies}

    @app.post("/actions/resume/{pk}")
    def resume(pk: str, body: dict, background: BackgroundTasks):
        # Continue the gated run in the background so the click returns instantly;
        # the pipeline's next steps stream to the Logs view as they happen.
        from agent.run import resume_job
        background.add_task(resume_job, pk, body.get("answer", ""))
        return {"ok": True, "status": "resuming"}

    @app.post("/actions/mark-applied/{pk}")
    def mark_applied(pk: str, body: dict | None = None):
        """Human confirms an application went through out-of-band (got the email
        / saw the ACK). Marks it applied and clears any gate — the safe fix for a
        mis-detected submit, and it prevents a resubmit."""
        note = ((body or {}).get("note") or "").strip() or "Confirmed by you (email / on-screen ACK)."
        make_stores(settings).tracking.set_status(
            pk, Status.APPLIED, confirmation_id=note, gate_reason="", gate_pending=None)
        from core.events import emit
        emit("applied", pk=pk, agent="applier", detail="Marked applied by you — no resubmit.")
        return {"ok": True}

    @app.post("/actions/skip/{pk}")
    def skip(pk: str):
        make_stores(settings).tracking.set_status(pk, Status.SKIPPED, skip_reason="user_skipped")
        return {"ok": True}

    @app.post("/actions/retry/{pk}")
    def retry(pk: str, background: BackgroundTasks):
        """Re-run a failed/errored job from scratch (clean session, current KB)."""
        from agent.run import retry_job
        background.add_task(retry_job, pk)
        return {"ok": True, "status": "retrying"}

    @app.post("/actions/mode")
    def set_mode(body: dict):
        """Flip apply mode: 'gated' (approve each) / 'auto' (apply while asleep)."""
        from core import flags
        mode = (body.get("mode") or "").lower()
        if mode not in ("gated", "auto"):
            return {"ok": False, "note": "mode must be 'gated' or 'auto'"}
        flags.set_flag("apply_mode", mode)
        return {"ok": True, "apply_mode": mode}

    @app.post("/actions/approve-all")
    def approve_all(body: dict, background: BackgroundTasks):
        """Approve every job waiting only for your go-ahead (gate 'approval'),
        optionally scoped to one company. They run 5 AT A TIME (parallel batches)
        for speed — each apply gets its own Chrome profile so the windows don't
        collide (Chrome locks a profile to one process)."""
        from agent.run import resume_job
        company = (body.get("company") or "").strip().lower()
        stores = make_stores(settings)
        pks = []
        # Approval gates live on TAILORED rows (tailoring done, awaiting the
        # go-ahead); stragglers from the old flow may still sit in needs_human.
        for r in [*stores.tracking.query_status(Status.TAILORED),
                  *stores.tracking.query_status(Status.NEEDS_HUMAN)]:
            if company and company != "__all__" and (r.get("company") or "").lower() != company:
                continue
            q = (r.get("gate_pending") or {}).get("question", "")
            if (r.get("status") == "tailored"
                    or r.get("gate_reason") == "approval"
                    or q.startswith("Ready to apply")):
                pks.append(r["pk"])

        _LANES = 5  # concurrent applies (also the number of Chrome profiles)

        def _run_all(items: list) -> None:
            import asyncio

            from agent.run import _apply_direct
            from core.logging import get_logger
            from core.stores import make_stores as _mk
            from tools.browser_apply import set_profile_override
            lg = get_logger("approve_all")

            # Run every apply as a coroutine in ONE event loop (never one
            # asyncio.run per thread — browser-use shares async objects that then
            # cross loops and crash). A queue of distinct profile dirs both caps
            # concurrency at _LANES and hands each apply its own Chrome profile
            # (Chrome locks a user_data_dir to one process). Lane 0 is the real
            # logged-in profile; 1..N are fresh (fine for public ATS forms).
            base = (getattr(settings, "browser_profile_dir", "") or "").strip()

            async def _driver() -> None:
                lanes: "asyncio.Queue[str]" = asyncio.Queue()
                for i in range(_LANES):
                    lanes.put_nowait(base if i == 0 else (f"{base}-{i}" if base else ""))

                async def _one(pk: str) -> None:
                    lane = await lanes.get()  # blocks until a lane frees → caps at _LANES
                    try:
                        set_profile_override(lane)  # task-local contextvar → this apply
                        await _apply_direct(pk, _mk())
                    except Exception:
                        lg.exception("approve-all failed for %s", pk)
                    finally:
                        lanes.put_nowait(lane)

                await asyncio.gather(*(_one(pk) for pk in items))

            asyncio.run(_driver())

        background.add_task(_run_all, pks)
        return {"ok": True, "approving": len(pks), "lanes": _LANES}

    @app.post("/actions/pause")
    def set_pause(body: dict):
        """Freeze/unfreeze the worker + discovery without stopping the daemon."""
        from core import flags
        flags.set_flag("paused", "yes" if body.get("paused") else "no")
        return {"ok": True, "paused": flags.paused()}

    @app.post("/actions/reset")
    def reset():
        """Clear the pipeline — tracking rows, queue, live events, and stored
        résumés/screenshots. Keeps your facts + saved logins. Local mode only."""
        import shutil

        if settings.mode != "local":
            return {"ok": False, "note": "reset is local-mode only"}
        import redis

        from tools import seen

        redis.Redis.from_url(settings.redis_url).flushdb()  # tracking + queue + events
        art = Path(settings.local_dir) / "artifacts"
        if art.exists():
            shutil.rmtree(art, ignore_errors=True)
        seen.clear()  # so a fresh start re-discovers everything
        return {"ok": True, "status": "reset"}

    @app.get("/actions/diff/{pk}")
    def diff(pk: str):
        """What the tailor changed on this résumé (base vs tailored bullets)."""
        from agent.run import _base_latex
        from tools.diffing import resume_diff

        stores = make_stores(settings)
        row = stores.tracking.get(pk) or {}
        tex_key = row.get("resume_tex_key")
        if not tex_key:
            return {"changes": [], "note": "no tailored résumé yet"}
        try:
            tailored = stores.artifacts.get(tex_key).decode()
        except Exception:
            return {"changes": [], "note": "tailored résumé unavailable"}
        return {"changes": resume_diff(_base_latex(), tailored)}

    @app.get("/artifact/{key:path}")
    def artifact(key: str):
        """Serve a stored artifact (résumé PDF, screenshot) to the dashboard.

        In local mode the artifact is a real file on disk — serve it with
        FileResponse so the browser gets Range support (HTTP 206), a Content-Length
        and an `inline` disposition. Chrome's embedded PDF viewer (PDFium) probes
        with a Range request and renders BLANK in an <iframe> when the server
        ignores it and returns a plain 200 — which is exactly why the tailored
        résumé wouldn't show. FileResponse fixes that; cloud falls back to bytes."""
        import mimetypes

        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        disp = "inline" if ctype in ("application/pdf",) or ctype.startswith("image/") \
            else "attachment"
        if settings.mode == "local":
            from fastapi.responses import FileResponse
            base = (Path(settings.local_dir) / "artifacts").resolve()
            p = (base / key).resolve()
            if p.is_file() and str(p).startswith(str(base) + "/"):  # no path traversal
                return FileResponse(str(p), media_type=ctype, headers={
                    "Content-Disposition": f'{disp}; filename="{p.name}"'})
        try:
            data = make_stores(settings).artifacts.get(key)
        except Exception:
            return Response(status_code=404)
        return Response(data, media_type=ctype,
                        headers={"Content-Disposition": disp})

    @app.get("/events")
    async def events():
        """Live activity stream (SSE): every agent step's input/response, gates,
        discoveries — pushed the instant they happen."""
        import redis.asyncio as aredis

        from core.events import CHANNEL, recent

        async def gen():
            for ev in reversed(recent(400)):  # deep history first — keep runs visible
                yield f"data: {json.dumps(ev)}\n\n"
            r = aredis.from_url(settings.redis_url, decode_responses=True)
            ps = r.pubsub()
            await ps.subscribe(CHANNEL)
            try:
                async for m in ps.listen():
                    if m and m.get("type") == "message":
                        yield f"data: {m['data']}\n\n"
            finally:
                await ps.aclose()
                await r.aclose()

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Static dashboard (index.html, app.js, styles.css, …) at the root, last so
    # the explicit API routes above win.
    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")
    return app


def _recover_stuck(settings) -> None:  # noqa: ANN001
    """Self-heal orphaned applies on startup. A job left in `submitting` means an
    apply was killed mid-flight (a prior run, a crash, a kill) and never reached a
    terminal state — so it hangs forever in the 'Submitting' lane. Reset it to
    `tailored` so it's un-stuck and re-appliable. Reset only (no auto re-queue) so
    we never silently re-submit something that might already have gone through. The
    daemon does this too (via _recover_orphans); the plain server must as well."""
    import logging

    from core.models import Status

    try:
        stores = make_stores(settings)
        rows = [r for r in stores.tracking.all()
                if not str(r.get("pk", "")).startswith("meta#")]
        stuck = [r for r in rows if r.get("status") == "submitting"]
        for r in stuck:
            stores.tracking.set_status(r["pk"], Status.TAILORED)
        # A killed score/tailor leaves the row in 'tailoring' — reset to found.
        mid = [r for r in rows if r.get("status") == "tailoring"]
        for r in mid:
            stores.tracking.set_status(r["pk"], Status.FOUND)
        if stuck or mid:
            logging.getLogger("server").info(
                "recovered %d 'submitting' -> tailored, %d 'tailoring' -> found",
                len(stuck), len(mid))
    except Exception:  # noqa: BLE001 - never block startup on recovery
        logging.getLogger("server").exception("orphan recovery failed")


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn

    _recover_stuck(get_settings())
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    serve()
