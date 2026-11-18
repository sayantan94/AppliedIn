"""Cosign search routes; a search never authorizes a submission."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from discovery import career_ops, cosign
from discovery.index_actions import selection_router

router = APIRouter(prefix="/cosign")


class Search(BaseModel):
    term: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=40)
    city: str = Field(default="", max_length=40)
    region: Literal["", "us"] = ""
    remote: bool = False
    min_salary: int = Field(default=0, ge=0, le=2_000_000)
    cursor: str | None = Field(default=None, max_length=2000)


@router.get("")
def options():
    prefs = career_ops.search_preferences()
    return {
        "count": cosign.open_roles(),
        "roles": cosign.ROLE_TYPES,
        "cities": cosign.CITIES,
        "max_cities": cosign.MAX_CITIES,
        "defaults": cosign.defaults(prefs),
        "examples": cosign.examples(prefs),
        "excluding": prefs.get("exclude_keywords", []),
    }


@router.post("/search")
def search(body: Search):
    try:
        return cosign.search(body.model_dump(exclude={"cursor"}), body.cursor)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except cosign.CosignError as exc:
        raise HTTPException(502, str(exc)) from exc


router.include_router(selection_router(cosign.stage))
