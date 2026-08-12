"""Pydantic schemas for sequences (themes + author sequences)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ClueRef(BaseModel):
    """Minimal clue reference for embedding in a sequence."""

    id: int
    legacy_number: Optional[int] = None
    clue_text: Optional[str] = None
    author: Optional[str] = None
    solution: Optional[str] = None

    model_config = {"from_attributes": True}


class SequenceBase(BaseModel):
    name: Optional[str] = None
    seq_type: str = Field("theme", pattern="^(author|theme)$")
    author: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    legacy_key: Optional[int] = None


class SequenceCreate(SequenceBase):
    pass


class SequenceUpdate(BaseModel):
    name: Optional[str] = None
    seq_type: Optional[str] = Field(None, pattern="^(author|theme)$")
    author: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None


class SequenceOut(SequenceBase):
    id: int
    clue_count: int = 0
    clues: list[ClueRef] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SequenceListOut(BaseModel):
    sequences: list[SequenceOut]
    total: int


# ── Membership ──────────────────────────────────────────────


class SequenceMembershipUpdate(BaseModel):
    """Add or remove a clue from a sequence by database id."""

    clue_ids: list[int] = Field(default_factory=list)


# ── Clue-embedded sequence summary ──────────────────────────


class ClueSequenceRef(BaseModel):
    """Lightweight sequence summary embedded in a ClueOut (avoids recursion)."""

    id: int
    name: Optional[str] = None
    seq_type: str
    author: Optional[str] = None
    color: Optional[str] = None

    model_config = {"from_attributes": True}
