"""a16z portfolio search routes; a search never authorizes a submission."""

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from discovery import a16z, career_ops, cosign

router = APIRouter(prefix="/a16z")


class Search(BaseModel):
    term: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=60)
    city: str = Field(default="", max_length=80)
    region: Literal["", "us"] = ""
    remote: bool = False
    min_salary: int = Field(default=0, ge=0, le=2_000_000)
    cursor: str | None = Field(default=None, max_length=12)


class Picked(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=a16z.STAGE_MAX)


@router.get("")
def options():
    prefs = career_ops.search_preferences()
    return {
        "count": a16z.open_roles(),
        "roles": a16z.ROLE_TYPES,
        "cities": a16z.CITIES,
        "max_cities": a16z.MAX_CITIES,
        "defaults": a16z.defaults(prefs),
        "examples": cosign.examples(prefs),
        "excluding": prefs.get("exclude_keywords", []),
    }


@router.post("/search")
def search(body: Search):
    try:
        return a16z.search(body.model_dump(exclude={"cursor"}), body.cursor)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except a16z.A16zError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/prepare")
def prepare(body: Picked):
    try:
        return a16z.stage(body.ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/apply")
def apply_selected(body: Picked, background: BackgroundTasks):
    from discovery.career_apply import run_selected

    try:
        result = a16z.stage(body.ids, apply_requested=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    background.add_task(run_selected, result["pks"])
    return {**result, "started": result["prepared"]}


class Tracked(BaseModel):
    pks: list[str] = Field(min_length=1, max_length=a16z.STAGE_MAX * 3)


@router.post("/progress")
def progress(body: Tracked):
    return cosign.progress(body.pks)
