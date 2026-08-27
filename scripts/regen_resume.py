"""Re-tailor the résumé for a job whose stored PDF is gone — WITHOUT applying.

`.local/artifacts/` can be recreated while the tracking rows live on in Redis,
and then a row still names `resumes/<pk>.pdf` that no longer exists. There was
no way to rebuild one: `retry_job` re-runs the WHOLE pipeline, applier included,
so using it to recover a file would submit a real application.

This runs the tailor loop alone against the same session state the pipeline
would build, and lets `save_tailored_resume` write the .tex and .pdf back under
the row's existing keys. Nothing is submitted and no address is spent.

A row that is already `applied` is REFUSED. The application went out with a
document this cannot reproduce; writing a freshly tailored file under that row
would present a résumé the employer never received as the one that was sent.

Usage:
    .venv/bin/python scripts/regen_resume.py <pk> [<pk> ...]
    .venv/bin/python scripts/regen_resume.py --list      # what's missing, and why
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config import get_settings  # noqa: E402
from core.stores import make_stores  # noqa: E402

_APPLIED = ("applied", "applied_manual")


def _artifact_root() -> Path:
    return Path(get_settings().local_dir) / "artifacts"


def missing_rows(stores) -> list[dict]:  # noqa: ANN001
    """Rows naming a résumé artifact whose bytes are not on disk."""
    root, out = _artifact_root(), []
    for row in stores.tracking.all():
        if str(row.get("pk", "")).startswith("meta#"):
            continue
        keys = [k for k in (row.get("resume_s3_key"), row.get("resume_tex_key")) if k]
        if keys and not any((root / k).is_file() for k in keys):
            out.append(row)
    return out


def regenerate(pk: str, stores) -> dict:  # noqa: ANN001
    """Run the tailor (and only the tailor) for one job."""
    import asyncio

    from google.adk.runners import Runner
    from google.genai import types

    from agent.graph import tailor
    from agent.run import _APP, _USER, _base_latex, _github_context, _prefs_notes, _session_service

    row = stores.tracking.get(pk)
    if row is None:
        return {"pk": pk, "result": "missing_row"}
    if row.get("status") in _APPLIED:
        return {"pk": pk, "result": "refused_applied", "status": row.get("status")}

    jd_text = row.get("jd_text") or ""
    if len(jd_text) < 400 and row.get("jd_url"):
        from tools.jd import fetch_jd
        jd_text = fetch_jd(row["jd_url"]) or jd_text
    if not jd_text.strip():
        return {"pk": pk, "result": "no_jd"}

    state = {
        "pk": pk, "company": row.get("company", ""), "ats": row.get("ats", ""),
        "jd_url": row.get("jd_url", ""), "jd_text": jd_text,
        "base_latex": _base_latex(), "github_context": _github_context(),
        "prefs_notes": _prefs_notes() or "(none)",
    }

    async def _go() -> None:
        sessions = _session_service()
        sid = f"regen:{pk}"
        if await sessions.get_session(app_name=_APP, user_id=_USER, session_id=sid):
            await sessions.delete_session(app_name=_APP, user_id=_USER, session_id=sid)
        await sessions.create_session(app_name=_APP, user_id=_USER, session_id=sid, state=state)
        runner = Runner(agent=tailor, app_name=_APP, session_service=sessions)
        msg = types.Content(role="user", parts=[types.Part(
            text=f"Tailor the résumé for {row.get('title','')} at {row.get('company','')}.")])
        async for _ in runner.run_async(user_id=_USER, session_id=sid, new_message=msg):
            pass

    asyncio.run(_go())

    root = _artifact_root()
    after = stores.tracking.get(pk) or {}
    key = after.get("resume_s3_key") or ""
    ok = bool(key) and (root / key).is_file()
    return {"pk": pk, "result": "ok" if ok else "no_pdf_written", "key": key}


def main() -> int:
    stores = make_stores()
    args = sys.argv[1:]
    if not args or args[0] == "--list":
        rows = missing_rows(stores)
        eligible = [r for r in rows if r.get("status") not in _APPLIED]
        print(f"{len(rows)} row(s) name a résumé whose file is gone.")
        print(f"{len(eligible)} can be regenerated; "
              f"{len(rows) - len(eligible)} are already applied and are refused.\n")
        for r in eligible:
            print(f"  {r['pk']:<48} {r.get('status',''):<10} {r.get('title','')}")
        return 0
    for pk in args:
        print(regenerate(pk, stores))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
