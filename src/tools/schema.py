"""Typed structured-output schemas for the agents (no JSON scraping)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MatchScore(BaseModel):
    """The scorer's structured verdict for one job."""

    score: int = Field(ge=0, le=10)
    reasoning: str = ""


class RelevanceResult(BaseModel):
    """The stage-1 relevance agent's structured pick: the 0-based indices of the
    postings that fit the target role."""

    fit_indices: list[int] = Field(default_factory=list)
