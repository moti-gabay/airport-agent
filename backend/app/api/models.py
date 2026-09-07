"""Request and response shapes for the HTTP layer."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None


class ToolCallOut(BaseModel):
    name: str
    input: dict
    ok: bool
    summary: str


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    trace: list[ToolCallOut]
    rounds: int
    stop_reason: str | None


class AirportOut(BaseModel):
    iata: str
    name: str | None
    municipality: str | None
    state: str | None
    composite_score: float | None
    rank: int | None
