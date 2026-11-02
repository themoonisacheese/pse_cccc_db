"""Shared clue write service — used by both the HTTP API (human editors)
and the ingest daemon (bot). Avoids coupling the daemon to FastAPI's
request/response/auth layer."""

import logging
from datetime import date
from typing import Optional

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clue import Clue, ClueEditHistory, User

logger = logging.getLogger(__name__)

# Transaction-scoped lock for every number-space writer, including appends.
# Take it before reading a target number or MAX(number). Range shifts retain
# the unique index, so callers' earlier plain reads cannot deadlock on DDL.
_LEGACY_NUMBER_LOCK = text("SELECT pg_advisory_xact_lock(996744001)")


async def _acquire_renumber_lock(db: AsyncSession) -> None:
    """Serialise legacy-number renumbering against other renumbering ops."""
    await db.execute(_LEGACY_NUMBER_LOCK)


async def _shift_legacy_numbers(
    db: AsyncSession, *, start: int, delta: int, stop: Optional[int] = None,
) -> int:
    """Shift [start, stop) under the numbering lock, retaining the unique index.

    Temporarily unnumber only the affected rows, then restore their final
    numbers in bulk. NULL is excluded from the partial unique index; other
    transactions see the previously committed numbers until we commit.
    The caller owns the transaction and must roll it back on failure.
    """
    condition = Clue.legacy_number >= start
    if stop is not None:
        condition = condition & (Clue.legacy_number < stop)
    rows = (await db.execute(
        select(Clue.id, Clue.legacy_number).where(condition)
    )).all()
    if not rows:
        return 0
    await db.execute(
        Clue.__table__.update().where(condition).values(legacy_number=None)
    )
    await db.execute(text("""
        UPDATE clues AS c SET legacy_number = shifted.number
        FROM unnest(CAST(:ids AS integer[]), CAST(:numbers AS integer[]))
             AS shifted(id, number)
        WHERE c.id = shifted.id
    """), {
        "ids": [row.id for row in rows],
        "numbers": [row.legacy_number + delta for row in rows],
    })
    return len(rows)


async def _recompute_pills(db: AsyncSession, from_legacy: int) -> None:
    """Recount the affected tail using the complete numbered prefix.

    Two window counts replace per-clue count queries. Unnumbered clues do not
    contribute; NULL/empty solvers have no solver pill, as on ordinary ingest.
    Call only after flushing all inserts/edits/deletions into their final state.
    """
    await db.execute(text("""
        WITH counts AS (
            SELECT id,
                row_number() OVER (
                    PARTITION BY author ORDER BY legacy_number
                ) AS authored,
                CASE WHEN solver IS NOT NULL AND solver <> '' THEN
                    row_number() OVER (
                        PARTITION BY solver ORDER BY legacy_number
                    )
                END AS solved
            FROM clues WHERE legacy_number IS NOT NULL
        )
        UPDATE clues AS c
        SET clues_by_author_so_far = counts.authored,
            clues_by_solver_so_far = counts.solved
        FROM counts
        WHERE c.id = counts.id AND c.legacy_number >= :from_legacy
          AND (c.clues_by_author_so_far IS DISTINCT FROM counts.authored
               OR c.clues_by_solver_so_far IS DISTINCT FROM counts.solved)
    """), {"from_legacy": from_legacy})


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
    try:
        clue = Clue(
            clue_text=clue_text,
            author=author,
            solver=solver,
            solution=solution,
            explanation=explanation,
            transcript_link=transcript_link,
            message_id=message_id,
            source=source,
            # Ingested clues carry no date in the SE chat payload; default to
            # today (the date the clue was ingested) unless the caller supplied one.
            clue_date=extra_fields.pop("clue_date", None) or date.today(),
        )

        # Set any extra fields the caller passed (e.g. one_word_answer_length)
        for k, v in extra_fields.items():
            if hasattr(clue, k) and v is not None:
                setattr(clue, k, v)

        # Normalise solution to uppercase on ingest (crosswords convention).
        if clue.solution:
            clue.solution = clue.solution.strip().upper()

        clue.entered_by_user_id = actor.id

        # Appends must serialize too: MAX(number) read before a concurrent deletion
        # would otherwise recreate a gap even after deletion itself was atomic.
        await _acquire_renumber_lock(db)
        if legacy_number is not None:
            await _shift_legacy_numbers(db, start=legacy_number, delta=1)
            clue.legacy_number = legacy_number
        else:
            max_num = (
                await db.execute(select(func.max(Clue.legacy_number)))
            ).scalar()
            clue.legacy_number = (max_num or 0) + 1
        clue.clues_by_author_so_far = (
            await db.execute(
                select(func.count(Clue.id)).where(
                    Clue.author == clue.author,
                    Clue.legacy_number < clue.legacy_number,
                )
            )
        ).scalar() + 1

        if clue.solver:
            clue.clues_by_solver_so_far = (
                await db.execute(
                    select(func.count(Clue.id)).where(
                        Clue.solver == clue.solver,
                        Clue.legacy_number < clue.legacy_number,
                    )
                )
            ).scalar() + 1

        db.add(clue)
        if legacy_number is not None:
            await db.flush()
            await _recompute_pills(db, legacy_number)
        await db.commit()
        await db.refresh(clue)
        logger.info(
            f"ingest_clue: #{clue.legacy_number} “{clue.clue_text[:60]}…” "
            f"by {clue.author} (source={source}, msg_id={message_id})"
        )
        return clue
    except Exception:
        await db.rollback()
        raise


async def update_clue(
    db: AsyncSession,
    *,
    actor: User,
    clue_id: int,
    update_data: dict,
) -> Clue:
    """Update and audit a clue, keeping numbering and affected pills consistent."""
    try:
        await _acquire_renumber_lock(db)
        result = await db.execute(
            select(Clue).where(Clue.id == clue_id)
            .execution_options(populate_existing=True)
        )
        clue = result.scalar_one_or_none()
        if clue is None:
            raise ValueError(f"Clue {clue_id} not found")

        update_data = dict(update_data)
        if update_data.get("solution"):
            update_data["solution"] = update_data["solution"].strip().upper()

        old_legacy = clue.legacy_number
        new_legacy = update_data.get("legacy_number", old_legacy)
        recompute_from = old_legacy
        if new_legacy != old_legacy:
            # Free the target's old position before restoring any shifted rows.
            await db.execute(
                Clue.__table__.update().where(Clue.id == clue_id)
                .values(legacy_number=None)
            )
            if old_legacy is None:
                await _shift_legacy_numbers(db, start=new_legacy, delta=1)
                recompute_from = new_legacy
            elif new_legacy is None:
                await _shift_legacy_numbers(db, start=old_legacy + 1, delta=-1)
            elif new_legacy < old_legacy:
                await _shift_legacy_numbers(
                    db, start=new_legacy, stop=old_legacy, delta=1,
                )
                recompute_from = new_legacy
            else:
                await _shift_legacy_numbers(
                    db, start=old_legacy + 1, stop=new_legacy + 1, delta=-1,
                )
            await db.execute(
                Clue.__table__.update().where(Clue.id == clue_id)
                .values(legacy_number=new_legacy)
            )

        for field, new_value in update_data.items():
            old_value = getattr(clue, field, None)
            if old_value != new_value:
                db.add(ClueEditHistory(
                    clue_id=clue.id,
                    edited_by_user_id=actor.id,
                    field_name=field,
                    old_value=str(old_value) if old_value is not None else None,
                    new_value=str(new_value) if new_value is not None else None,
                ))
                setattr(clue, field, new_value)

        relevant = any(f in update_data for f in ("author", "solver", "legacy_number"))
        if relevant and new_legacy is None:
            clue.clues_by_author_so_far = None
            clue.clues_by_solver_so_far = None
        await db.flush()
        if relevant and recompute_from is not None:
            await _recompute_pills(db, recompute_from)
        await db.commit()
        await db.refresh(clue)
        return clue
    except Exception:
        await db.rollback()
        raise


async def delete_clue(db: AsyncSession, *, actor: User, clue_id: int) -> None:
    """Delete a clue and close its numbered position in the same transaction.

    Stable primary keys, message IDs, and surviving relationships never change.
    Existing foreign-key cascades remove only the deleted clue's dependants.
    """
    try:
        await _acquire_renumber_lock(db)
        result = await db.execute(
            select(Clue).where(Clue.id == clue_id)
            .execution_options(populate_existing=True)
        )
        clue = result.scalar_one_or_none()
        if clue is None:
            raise ValueError(f"Clue {clue_id} not found")
        old_legacy = clue.legacy_number
        actor_id = actor.id
        await db.delete(clue)
        await db.flush()
        if old_legacy is not None:
            shifted = await _shift_legacy_numbers(
                db, start=old_legacy + 1, delta=-1,
            )
            if shifted:
                await _recompute_pills(db, old_legacy)
        await db.commit()
        logger.info("delete_clue: id=%s legacy=%s actor=%s", clue_id, old_legacy, actor_id)
    except Exception:
        await db.rollback()
        raise
