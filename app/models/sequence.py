"""SQLAlchemy ORM models for sequences (themes + author sequences).

Themes and author sequences are the same underlying thing: an unordered
group of clues that "go together". The old spreadsheet tracked them in
two separate columns (loose shared themes vs. one-author revealed sequences),
but conceptually they're both just a *sequence* of related clues. We model
each as one `Sequence` row joined to clues many-to-many, so a clue can be a
member of any number of sequences, and the only meaningful distinction is
`seq_type`: whether it's a setter-revealed author sequence (shown in its own
color) or a loose theme.
"""

from datetime import datetime

from sqlalchemy import (
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    Column,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


# Association table: a clue can belong to any number of sequences, and a
# sequence can gather any number of clues (many-to-many).
clue_sequences = Table(
    "clue_sequences",
    Base.metadata,
    Column("clue_id", Integer, ForeignKey("clues.id", ondelete="CASCADE"), primary_key=True),
    Column("sequence_id", Integer, ForeignKey("sequences.id", ondelete="CASCADE"), primary_key=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)


class Sequence(Base):
    """A named or unnamed group of related clues (theme or author sequence)."""

    __tablename__ = "sequences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Name is optional. Legacy groups, and "these fit together but there's no
    # name for it" groups, simply leave it NULL.
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # "author" = a setter revealed a run of their own clues; "theme" = loose
    # playful theme. Author sequences render in a distinct color.
    seq_type: Mapped[str] = mapped_column(
        String(16), default="theme", server_default="theme"
    )
    # For author sequences, which setter the run belongs to (informational).
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Optional color override (CSS color keyword/hex). When null, the UI picks
    # a default color based on seq_type.
    color: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Optional short note describing the sequence / its theme.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Key used when importing from the legacy spreadsheet: the representative
    # clue# the old sheet used to reference the group. Kept so re-imports are
    # idempotent and legacy linkage is auditable.
    legacy_key: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    clues: Mapped[list["Clue"]] = relationship(
        secondary="clue_sequences",
        back_populates="sequences",
        lazy="selectin",
    )

    __table_args__ = (
        # One legacy key per type, so re-running the import can't duplicate.
        UniqueConstraint("seq_type", "legacy_key", name="uq_sequences_type_legacy"),
    )

    def __repr__(self):
        return f"<Sequence {self.name or '(unnamed)'} ({self.seq_type})>"
