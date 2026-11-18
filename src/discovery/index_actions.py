"""Selection and progress routes shared by the job search indexes."""

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from discovery import cosign


class Picked(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=cosign.STAGE_MAX)


class Tracked(BaseModel):
    pks: list[str] = Field(min_length=1, max_length=cosign.STAGE_MAX * 3)


def selection_router(stage) -> APIRouter:
    router = APIRouter()

    @router.post("/prepare")
    def prepare(body: Picked):
        try:
            return stage(body.ids)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/apply")
    def apply_selected(body: Picked, background: BackgroundTasks):
        from discovery.career_apply import run_selected

        try:
            result = stage(body.ids, apply_requested=True)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        background.add_task(run_selected, result["pks"])
        return {**result, "started": result["prepared"]}

    @router.post("/progress")
    def progress(body: Tracked):
        return cosign.progress(body.pks)

    return router
