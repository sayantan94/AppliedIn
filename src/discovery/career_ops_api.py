"""Local job-board routes; discovery never authorizes a submission."""

import asyncio
import json

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from discovery import career_ops

router = APIRouter(prefix="/career-ops")


class BoardSelection(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=50)


class BoardSettings(BaseModel):
    companies: list[str] = Field(max_length=300)
    scheduled: bool
    auto_prepare: bool
    interests: str = Field(default="", max_length=600)
    scheduled_interests: bool = False


class ScanScope(BaseModel):
    company: str = Field(default="", max_length=200)


class InterestSearch(ScanScope):
    interests: str | None = Field(default=None, max_length=600)


@router.get("")
def board():
    return career_ops.snapshot()


@router.get("/progress")
async def progress():
    async def updates():
        previous = None
        while True:
            state = career_ops.progress_snapshot()
            payload = json.dumps(state, ensure_ascii=False)
            if payload != previous:
                yield f"data: {payload}\n\n"
                previous = payload
            else:
                yield ": keep-alive\n\n"
            if not state["running"]:
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        updates(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/settings")
def settings(body: BoardSettings):
    try:
        return career_ops.configure(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/scan")
def scan(body: ScanScope, background: BackgroundTasks):
    try:
        sources = career_ops.catalog()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    use_web = body.company and body.company not in {s["name"] for s in sources}
    if not career_ops.reserve_scan("company_web" if use_web else "feeds", body.company):
        return {"running": True, "already_running": True}
    background.add_task(
        career_ops.search_reserved if use_web else career_ops.scan_reserved, body.company
    )
    return {"running": True, "kind": "web" if use_web else "feeds"}


@router.post("/search")
def search(body: InterestSearch, background: BackgroundTasks):
    if not career_ops.reserve_scan("company_web" if body.company else "interests", body.company):
        return {"running": True, "already_running": True}
    background.add_task(career_ops.search_reserved, body.company, body.interests)
    return {"running": True, "kind": "web"}


@router.post("/prepare")
def prepare(body: BoardSelection):
    try:
        return career_ops.prepare(body.ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/dismiss")
def dismiss(body: BoardSelection):
    try:
        return career_ops.dismiss(body.ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
