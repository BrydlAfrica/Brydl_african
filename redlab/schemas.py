"""Pydantic v2 schemas for the RedLab HTTP API (Phase 4+).

All request/response bodies exposed under ``/redlab/v1/`` live here so
routes stay thin — they validate inputs and serialise ORM rows through
these models rather than hand-rolling dicts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


_MAX_CONTENT = 4000
_MAX_CLIENT_TURN_ID = 64


class TurnRequest(BaseModel):
    """Annotator → LLM submission payload.

    ``client_turn_id`` is the client-generated idempotency key for this
    user turn. Retrying the same POST with the same key is guaranteed
    to return the cached user/assistant pair without re-calling the
    client model or writing extra rows.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(..., min_length=1, max_length=_MAX_CONTENT)
    client_turn_id: str = Field(
        ..., min_length=1, max_length=_MAX_CLIENT_TURN_ID,
    )


class MessageOut(BaseModel):
    """JSON shape of one Message row (user or assistant)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    content: str
    turn_index: int
    client_turn_id: Optional[str] = None
    is_unsafe_flag: bool = False
    created_at: datetime


class SessionOut(BaseModel):
    """JSON shape of a Session row returned by turn/flag endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    turn_count: int
    breach_count: int
    annotator_ref: str
    project_ref: str
    target_id: str
    conclude_note: Optional[str] = None
    created_at: datetime
    concluded_at: Optional[datetime] = None


class TurnResponse(BaseModel):
    """Response envelope for ``POST .../turn``."""

    user_message: MessageOut
    assistant_message: MessageOut
    session: SessionOut


class SessionResponse(BaseModel):
    """Response envelope for a session lookup (Phase 5+ placeholders)."""

    session: SessionOut
    messages: list[MessageOut]


class FlagRequest(BaseModel):
    """Annotator flags a single assistant message as unsafe."""

    model_config = ConfigDict(extra="forbid")

    is_unsafe: bool


class ConcludeRequest(BaseModel):
    """Annotator ends the session with an optional note."""

    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = Field(default=None, max_length=2000)


class ErrorResponse(BaseModel):
    """Machine-readable error envelope."""

    error: str
    detail: Optional[str] = None
    request_id: Optional[str] = None


__all__ = [
    "TurnRequest", "TurnResponse", "MessageOut", "SessionOut",
    "SessionResponse", "FlagRequest", "ErrorResponse",
    "_MAX_CONTENT", "_MAX_CLIENT_TURN_ID",
]
