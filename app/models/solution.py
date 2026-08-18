"""SQLAlchemy ORM models for the solution-ingest (Stage 2) feature.

Two tables back the feature (see migration_009_solutions.sql):

  * ClueCandidate — one row per proposed solution for a clue.  Doubles as the
    editor review queue AND the calibration source: approved / highest-
    unapproved confidence scores are read straight off these rows at runtime.
  * PendingLlm — a DB-backed retry queue for LLM-required work (salt
    extraction, wordplay-only extraction).  Survives restarts so an LLM
    outage never loses work; the worker drains it with backoff.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Integer,
    String,
    Text,
    Float,
    DateTime,
    ForeignKey,
    Index,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ClueCandidate(Base):
    """A proposed solution for a clue, awaiting editor review."""

    __tablename__ = "clue_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clue_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clues.id", ondelete="CASCADE")
    )
    solution: Mapped[str] = mapped_column(Text)
    solver: Mapped[str | None] = mapped_column(String(255), nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    signals: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 'pending' | 'approved' | 'rejected'
    status: Mapped[str] = mapped_column(String(16), default="pending")
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_clue_candidates_clue_id", "clue_id"),
        Index("ix_clue_candidates_status", "status"),
    )

    def __repr__(self):
        return f"<ClueCandidate #{self.id} clue={self.clue_id} conf={self.confidence:.2f} {self.status}>"


class PendingLlm(Base):
    """A unit of LLM-required work waiting to be processed by the worker."""

    __tablename__ = "pending_llm"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clue_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("clues.id", ondelete="CASCADE"), nullable=True
    )
    task_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # 'pending' | 'done' | 'failed'
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_pending_llm_status", "status"),
    )

    def __repr__(self):
        return f"<PendingLlm #{self.id} {self.task_type} {self.status}>"
