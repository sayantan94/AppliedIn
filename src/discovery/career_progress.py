"""Small, read-only application receipts for the discovery board."""


def application_progress(row: dict, event: dict | None = None, *, in_flight=False) -> dict:
    event = event or {}
    status = row.get("status", "")
    agent = event.get("agent", "")
    phase, label = "queued", "Waiting to start"
    if status == "tailoring":
        phase, label = "score", "Reading posting & scoring"
        if agent in ("tailor", "critic"):
            phase, label = "tailor", "Checking résumé" if agent == "critic" else "Tailoring résumé"
    elif status == "tailored":
        phase, label = "waiting", "Waiting for application browser"
    if status == "submitting" or (in_flight and status in ("tailored", "tailoring")):
        phase, label = "apply", "Applying in browser"
    # A browser event is never proof of submission. Durable outcomes win over
    # stale telemetry and leases, including a lease still releasing after a gate.
    if status in ("applied", "applied_manual"):
        phase, label = "applied", "Applied" if status == "applied" else "Marked applied manually"
    elif status in ("needs_human", "capped", "failed", "error", "job_gone", "skipped"):
        phase = "attention"
        label = {
            "needs_human": "Needs your attention",
            "capped": "Application limit reached",
            "skipped": "Skipped",
            "job_gone": "Posting closed",
        }.get(status, "Could not finish")
        if row.get("fail_kind") == "application_limit":
            label = "Company's application limit"
    elif not status:
        phase, label = "unknown", "Status unavailable"
    reason = ""
    if phase == "attention":
        # A gate question is only the reason while the row is actually waiting on
        # it. A failed row can still carry the "Ready to apply?" prompt from the
        # preparation it passed through, and showing it read as the board asking
        # for an approval it was never going to act on.
        question = ((row.get("gate_pending") or {}).get("question")
                    if status == "needs_human" else "")
        reason = question or next(
            (
                row[k]
                for k in ("fail_reason", "jd_read_error", "skip_reason", "gate_reason")
                if row.get(k)
            ),
            "Open the pipeline for details and next steps.",
        )
    return {
        "phase": phase,
        "label": label,
        "status": status,
        "requested_at": row.get("apply_requested_at", ""),
        "applied_at": row.get("applied_at", ""),
        "match_score": row.get("match_score"),
        "updated_at": event.get("at", ""),
        "detail": str(reason)[:600],
    }


def attach_progress(rows: list[dict], stores) -> None:
    from core.apply_queue import ApplyQueue
    from core.events import recent
    from discovery.career_ops import canonical

    # A role can be submitted through the main pipeline after the board saved it.
    # Resolve its live tracking state rather than trusting the board's old "new" flag.
    records = stores.tracking.all()
    by_pk = {r["pk"]: r for r in records}
    by_url = {}
    for row in records:
        url = canonical(row["jd_url"]) if row.get("jd_url") else ""
        if url and (url not in by_url or row.get("status") in ("applied", "applied_manual")):
            by_url[url] = row
    for job in rows:
        linked = by_url.get(canonical(job["url"])) if job.get("url") else None
        linked = linked or {}
        row = by_pk.get(job.get("pk")) or linked
        if linked.get("status") in ("applied", "applied_manual"):
            row = linked
        if row:
            job.update(pk=row["pk"], pipeline_status=row.get("status", ""))
            if job.get("state") == "new":
                job["state"] = "pipeline"
    tracked = [r for r in rows if r.get("pk")]
    if not tracked:
        return
    events = {}
    for event in recent(500):
        if event.get("agent") in ("scorer", "tailor", "critic", "applier", "browser"):
            events.setdefault(event.get("pk"), event)
    flight = ApplyQueue(stores.tracking.r).in_flight() if hasattr(stores.tracking, "r") else set()
    for job in tracked:
        row = by_pk.get(job["pk"]) or {}
        if row.get("apply_requested_at"):
            job["application"] = application_progress(
                row, events.get(job["pk"]), in_flight=job["pk"] in flight
            )
