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
    if status == "error":
        return row.get("error") or "Hit an error during the pipeline (see the logs)."
    if status == "capped":
        return "Daily application cap reached — parked for the next run."
    return None


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
        "gate_question": (row.get("gate_pending") or {}).get("question"),
        "skip_reason": row.get("skip_reason"),
        "closed_reason": _closed_reason(row),
        "resume_version": row.get("resume_version"),
        "resume_url": link("resume_s3_key"),
        "has_diff": bool(row.get("resume_tex_key")),
        "jd_url": row.get("jd_url"),
        "screenshot_url": link("screenshot_s3_key"),
        "confirmation_id": row.get("confirmation_id"),
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
        rows = stores.tracking.all()
        return {"items": [_to_ui(r, stores.artifacts) for r in rows]}

    @app.get("/stats")
    def stats():
        stores = make_stores(settings)
        counts: dict[str, int] = {}
        for r in stores.tracking.all():
            counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
        applied = counts.get("applied", 0) + counts.get("applied_manual", 0)
        return {"daily_cap": settings.daily_cap, "today_submitted": applied,
                "queue_age_seconds": None, "paused": False, "counts_by_status": counts}

    @app.post("/actions/resume/{pk}")
    def resume(pk: str, body: dict, background: BackgroundTasks):
        # Continue the gated run in the background so the click returns instantly;
        # the pipeline's next steps stream to the Logs view as they happen.
        from agent.run import resume_job
        background.add_task(resume_job, pk, body.get("answer", ""))
        return {"ok": True, "status": "resuming"}

    @app.post("/actions/skip/{pk}")
    def skip(pk: str):
        make_stores(settings).tracking.set_status(pk, Status.SKIPPED, skip_reason="user_skipped")
        return {"ok": True}

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
        """Serve a stored artifact (résumé PDF, screenshot) to the dashboard."""
        import mimetypes

        try:
            data = make_stores(settings).artifacts.get(key)
        except Exception:
            return Response(status_code=404)
        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        return Response(data, media_type=ctype)

    @app.get("/events")
    async def events():
        """Live activity stream (SSE): every agent step's input/response, gates,
        discoveries — pushed the instant they happen."""
        import redis.asyncio as aredis

        from core.events import CHANNEL, recent

        async def gen():
            for ev in reversed(recent(40)):  # recent history first
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


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    serve()
