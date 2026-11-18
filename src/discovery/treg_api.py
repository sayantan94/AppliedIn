"""On-demand search for the Treg window. Opening the window only reads local settings."""

from fastapi import APIRouter, HTTPException

from discovery import career_ops, cosign, treg
from discovery.cosign_api import Search
from discovery.index_actions import selection_router

router = APIRouter(prefix="/treg")


@router.get("")
def options():
    prefs = career_ops.search_preferences()
    defaults = cosign.defaults(prefs)
    defaults["preferred"] = defaults["preferred"][:1]
    return {
        "configured": bool(treg.team_key()),
        "setup": treg.SETUP,
        "page_size": treg.PAGE,
        "roles": {},
        "cities": cosign.CITIES,
        "max_cities": 1,
        "defaults": defaults,
        "examples": cosign.examples(prefs),
        "excluding": prefs.get("exclude_keywords", []),
    }


@router.post("/search")
def search(body: Search):
    if body.role:
        raise HTTPException(400, "Use the title field for Treg searches.")
    try:
        return treg.search(body.model_dump(exclude={"cursor"}), body.cursor)
    except ValueError as exc:
        raise HTTPException(400, "Invalid city or page.") from exc
    except treg.TregError as exc:
        raise HTTPException(503, str(exc)) from exc


router.include_router(selection_router(treg.stage))
