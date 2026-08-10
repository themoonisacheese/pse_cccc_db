"""SQLAlchemy ORM models for clues and users."""

from datetime import datetime, date
from sqlalchemy import (
    Integer,
    String,
    Text,
    Date,
    DateTime,
    Boolean,
    ForeignKey,
    Index,
    text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import TSVECTOR

from app.db.session import Base


class User(Base):
    """An authenticated Stack Exchange user."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    se_user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    profile_link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_room_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    is_editor: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    clues_authored: Mapped[list["Clue"]] = relationship(
        back_populates="author_user", foreign_keys="Clue.entered_by_user_id"
    )

    def __repr__(self):
        return f"<User {self.display_name} ({self.se_user_id})>"


class Clue(Base):
    """A single cryptic clue in the CCCC archive."""

    __tablename__ = "clues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The original sequential number from the spreadsheet (1–10000…)
    legacy_number: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)

    clue_text: Mapped[str] = mapped_column(Text)
    # clue_length is a generated column: LENGTH(clue_text) — not settable
    clue_length: Mapped[int | None] = mapped_column(
        Integer, nullable=True, server_default=text("LENGTH(clue_text)")
    )

    author: Mapped[str] = mapped_column(String(255), index=True)
    solver: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    override_solver: Mapped[str | None] = mapped_column(String(255), nullable=True)

    solution: Mapped[str | None] = mapped_column(Text, index=True, nullable=True)
    # answer_length is a generated column: letter count of solution (ignoring spaces/hyphens)
    answer_length: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        server_default=text("LENGTH(REPLACE(REPLACE(solution, ' ', ''), '-', ''))")
    )
    one_word_answer_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    clue_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    number_on_date: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clues_by_author_so_far: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clues_by_solver_so_far: Mapped[int | None] = mapped_column(Integer, nullable=True)

    transcript_link: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)

    # Track who entered / last edited the clue
    entered_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    author_user: Mapped[User | None] = relationship(
        back_populates="clues_authored", foreign_keys=[entered_by_user_id]
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Full-text search vector — kept in sync via trigger (see SQL migration)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, nullable=True, deferred=True
    )

    __table_args__ = (
        Index("ix_clues_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_clues_author_date", "author", "clue_date"),
        Index("ix_clues_solver_date", "solver", "clue_date"),
    )

    def __repr__(self):
        return f"<Clue #{self.id}: {self.clue_text[:60]}…>"


class ClueEditHistory(Base):
    """Audit log of edits to a clue."""

    __tablename__ = "clue_edit_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clue_id: Mapped[int] = mapped_column(Integer, ForeignKey("clues.id", ondelete="CASCADE"), index=True)
    edited_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    field_name: Mapped[str] = mapped_column(String(100))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
