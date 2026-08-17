"""Shared clue write service — used by both the HTTP API (human editors)
and the ingest daemon (bot). Avoids coupling the daemon to FastAPI's
request/response/auth layer."""

import logging
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clue import Clue, ClueEditHistory, User

logger = logging.getLogger(__name__)


async def ingest_clue(
    db: AsyncSession,
    *,
    actor: User,
    clue_text: str,
    author: str,
    solver: Optional[str] = None,
    solution: Optional[str] = None,
    explanation: Optional[str] = None,
    transcript_link: Optional[str] = None,
    message_id: Optional[int] = None,
    source: str = "manual",
    legacy_number: Optional[int] = None,
    **extra_fields,
) -> Clue:
    """Create a new clue record with all the normalisation and numbering
    that the human API path does.  Shared by:
      - POST /api/clues (human editor, via _check_write_perm)
      - The ingest daemon (bot, via the bot's User row)

    Returns the newly created Clue ORM object (already committed and
    refreshed).
    """
    clue = Clue(
        clue_text=clue_text,
        author=author,
        solver=solver,
        solution=solution,
        explanation=explanation,
        transcript_link=transcript_link,
        message_id=message_id,
        source=source,
    )

    # Set any extra fields the caller passed (e.g. one_word_answer_length, clue_date)
    for k, v in extra_fields.items():
        if hasattr(clue, k) and v is not None:
            setattr(clue, k, v)

    # Normalise solution to uppercase on ingest (crosswords convention).
    if clue.solution:
        clue.solution = clue.solution.strip().upper()

    clue.entered_by_user_id = actor.id

    # ── Legacy-number assignment ─────────────────────────────
    if legacy_number is not None:
        # Shift existing clues up to make room (will be cheap at ~10k rows).
        await db.execute(
            Clue.__table__.update()
            .where(Clue.legacy_number >= legacy_number)
            .values(legacy_number=Clue.legacy_number + 1)
        )
        clue.legacy_number = legacy_number
    else:
        max_num = (
            await db.execute(select(func.max(Clue.legacy_number)))
        ).scalar()
        clue.legacy_number = (max_num or 0) + 1

    # ── Author / solver pills ────────────────────────────────
    clue.clues_by_author_so_far = (
        await db.execute(
            select(func.count(Clue.id)).where(
                Clue.author == clue.author,
                Clue.legacy_number <= clue.legacy_number,
            )
        )
    ).scalar()

    if clue.solver:
        clue.clues_by_solver_so_far = (
            await db.execute(
                select(func.count(Clue.id)).where(
                    Clue.solver == clue.solver,
                    Clue.legacy_number <= clue.legacy_number,
                )
            )
        ).scalar()

    db.add(clue)
    await db.commit()
    await db.refresh(clue)
    logger.info(
        f"ingest_clue: #{clue.legacy_number} “{clue.clue_text[:60]}…” "
        f"by {clue.author} (source={source}, msg_id={message_id})"
    )
    return clue


async def update_clue(
    db: AsyncSession,
    *,
    actor: User,
    clue_id: int,
    update_data: dict,
) -> Clue:
    """Update an existing clue, recording edit history.
    Shared by the HTTP PUT path and any future reconciliation logic."""
    result = await db.execute(select(Clue).where(Clue.id == clue_id))
    clue = result.scalar_one_or_none()
    if not clue:
        raise ValueError(f"Clue {clue_id} not found")

    # Normalise solution to uppercase.
    if "solution" in update_data and update_data["solution"]:
        update_data["solution"] = update_data["solution"].strip().upper()

    for field, new_value in update_data.items():
        old_value = getattr(clue, field, None)
        if old_value != new_value:
            history = ClueEditHistory(
                clue_id=clue.id,
                edited_by_user_id=actor.id,
                field_name=field,
                old_value=str(old_value) if old_value is not None else None,
                new_value=str(new_value) if new_value is not None else None,
            )
            db.add(history)
            setattr(clue, field, new_value)

    # Recompute author/solver pills if relevant fields changed.
    if any(f in update_data for f in ("author", "solver", "legacy_number")):
        clue.clues_by_author_so_far = (
            await db.execute(
                select(func.count(Clue.id)).where(
                    Clue.author == clue.author,
                    Clue.legacy_number <= clue.legacy_number,
                )
            )
        ).scalar()
        clue.clues_by_solver_so_far = (
            await db.execute(
                select(func.count(Clue.id)).where(
                    Clue.solver == clue.solver,
                    Clue.legacy_number <= clue.legacy_number,
                )
            )
        ).scalar() if clue.solver else None

    await db.commit()
    await db.refresh(clue)
    return clue