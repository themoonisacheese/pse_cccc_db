"""Pydantic schemas for API input/output validation."""

from __future__ import annotations

from datetime import date as date_type, datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Clue schemas ─────────────────────────────────────────────


class ClueBase(BaseModel):
    clue_text: str
    clue_length: Optional[int] = None
    author: str
    solver: Optional[str] = None
    override_solver: Optional[str] = None
    solution: str
    answer_length: Optional[int] = None
    one_word_answer_length: Optional[int] = None
    answer_in_grid: Optional[str] = None
    answer_count: Optional[int] = None
    explanation: Optional[str] = None
    standard_clue: Optional[bool] = None
    clue_date: Optional[date_type] = None
    number_on_date: Optional[int] = None
    clues_by_author_so_far: Optional[int] = None
    transcript_link: Optional[str] = None
    legacy_number: Optional[int] = None


class ClueCreate(ClueBase):
    pass


class ClueUpdate(BaseModel):
    clue_text: Optional[str] = None
    clue_length: Optional[int] = None
    author: Optional[str] = None
    solver: Optional[str] = None
    override_solver: Optional[str] = None
    solution: Optional[str] = None
    answer_length: Optional[int] = None
    one_word_answer_length: Optional[int] = None
    answer_in_grid: Optional[str] = None
    answer_count: Optional[int] = None
    explanation: Optional[str] = None
    standard_clue: Optional[bool] = None
    clue_date: Optional[date_type] = None
    number_on_date: Optional[int] = None
    clues_by_author_so_far: Optional[int] = None
    transcript_link: Optional[str] = None
    legacy_number: Optional[int] = None


class ClueOut(ClueBase):
    id: int
    entered_by_user_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ClueListOut(BaseModel):
    """Paginated clue list."""

    clues: list[ClueOut]
    total: int
    page: int
    page_size: int
    total_pages: int


# ── Search schemas ───────────────────────────────────────────


class ClueSearch(BaseModel):
    q: Optional[str] = None  # full-text query
    author: Optional[str] = None
    solver: Optional[str] = None
    solution: Optional[str] = None  # exact/ILIKE solution search
    date_from: Optional[date_type] = None
    date_to: Optional[date_type] = None
    legacy_number: Optional[int] = None
    transcript_link: Optional[str] = None
    order_by: str = "legacy_number"
    order_dir: str = "asc"
    page: int = 1
    page_size: int = 50


# ── User schemas ─────────────────────────────────────────────


class UserOut(BaseModel):
    id: int
    se_user_id: int
    display_name: str
    profile_link: Optional[str] = None
    is_room_owner: bool
    is_editor: bool
    is_admin: bool

    model_config = {"from_attributes": True}


# ── Stats schemas ────────────────────────────────────────────


class AuthorStat(BaseModel):
    name: str
    count: int


class DateStat(BaseModel):
    date: date_type
    count: int


class StatsOut(BaseModel):
    total_clues: int
    total_authors: int
    total_solvers: int
    most_prolific_author: Optional[AuthorStat] = None
    longest_solution: Optional[str] = None
    longest_clue: Optional[str] = None
    most_repeated_solution: Optional[str] = None
    date_with_most_clues: Optional[DateStat] = None
    first_clue_date: Optional[date_type] = None
    last_clue_date: Optional[date_type] = None


# ── Transcript parse result ─────────────────────────────────


class TranscriptParseResult(BaseModel):
    """Result of parsing a chat transcript link."""

    url: str
    message_id: Optional[int] = None
    author: Optional[str] = None
    date: Optional[date_type] = None
    content: Optional[str] = None
    success: bool = False
    error: Optional[str] = None
