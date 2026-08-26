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


async def _recompute_pills(db: AsyncSession, from_legacy: int) -> None:
    """Recompute the author/solver pills for every clue at or after a position.

    The pills (`clues_by_author_so_far` / `clues_by_solver_so_far`) are defined
    as "count of clues by this author/solver with legacy_number <= this clue's
    legacy_number".  When a clue is inserted mid-chain, every clue that gets
    shifted up (legacy_number >= insertion point) can have its pill change —
    e.g. if the inserted clue is by the same author, every later clue by that
    author gains one.  This recomputes them from scratch for correctness.

    Mid-chain insertions are rare (a human fixing a missed/mis-formatted clue),
    so recomputing per-clue is acceptable at ~10k rows.
    """
    result = await db.execute(
        select(Clue).where(Clue.legacy_number >= from_legacy)
    )
    shifted = result.scalars().all()
    for clue in shifted:
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
        else:
            clue.clues_by_solver_so_far = None


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

    # ── Legacy-number assignment ─────────────────────────────
    if legacy_number is not None:
        # Temporarily drop the partial unique index so the bulk shift
        # (legacy_number + 1) doesn't trip a duplicate-key violation
        # on intermediate row states.  Recreated after the shift, all
        # within the same transaction.
        await db.execute(text("DROP INDEX IF EXISTS uq_clues_legacy_number"))
        # Shift existing clues up to make room (will be cheap at ~10k rows).
        await db.execute(
            Clue.__table__.update()
            .where(Clue.legacy_number >= legacy_number)
            .values(legacy_number=Clue.legacy_number + 1)
        )
        # Recreate the partial unique index.
        await db.execute(text(
            "CREATE UNIQUE INDEX uq_clues_legacy_number "
            "ON clues (legacy_number) WHERE legacy_number IS NOT NULL"
        ))
        clue.legacy_number = legacy_number
        # The shift changed legacy_numbers, so every clue that moved needs its
        # author/solver pill recomputed (it may have gained entries).
        await _recompute_pills(db, legacy_number)
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
    Shared by the HTTP PUT path and any future reconciliation logic.

    If ``legacy_number`` is being changed to a value already taken by another
    clue, the existing clue(s) are shifted (the same drop-index / shift /
    recreate-index dance used by :func:`ingest_clue`) so the partial unique
    index doesn't trip on intermediate row states.
    """
    result = await db.execute(select(Clue).where(Clue.id == clue_id))
    clue = result.scalar_one_or_none()
    if not clue:
        raise ValueError(f"Clue {clue_id} not found")

    # Normalise solution to uppercase.
    if "solution" in update_data and update_data["solution"]:
        update_data["solution"] = update_data["solution"].strip().upper()

    # ── Legacy-number shifting ──────────────────────────────
    new_legacy = update_data.get("legacy_number")
    old_legacy = clue.legacy_number
    if new_legacy is not None and new_legacy != old_legacy:
        # NULL out this clue's legacy_number first so it doesn't collide
        # with the shifted rows or the recreated unique index.
        await db.execute(
            Clue.__table__.update()
            .where(Clue.id == clue_id)
            .values(legacy_number=None)
        )
        # Temporarily drop the partial unique index so the bulk shift
        # doesn't trip a duplicate-key violation on intermediate states.
        await db.execute(text("DROP INDEX IF EXISTS uq_clues_legacy_number"))
        if new_legacy < old_legacy:
            # Moving earlier: shift clues in [new_legacy, old_legacy) up by 1.
            await db.execute(
                Clue.__table__.update()
                .where(Clue.legacy_number >= new_legacy)
                .where(Clue.legacy_number < old_legacy)
                .values(legacy_number=Clue.legacy_number + 1)
            )
            recompute_from = new_legacy
        else:
            # Moving later: shift clues in (old_legacy, new_legacy] down by 1.
            await db.execute(
                Clue.__table__.update()
                .where(Clue.legacy_number > old_legacy)
                .where(Clue.legacy_number <= new_legacy)
                .values(legacy_number=Clue.legacy_number - 1)
            )
            recompute_from = old_legacy
        # Set the clue's new legacy_number before recreating the index.
        await db.execute(
            Clue.__table__.update()
            .where(Clue.id == clue_id)
            .values(legacy_number=new_legacy)
        )
        clue.legacy_number = new_legacy
        # Recreate the partial unique index.
        await db.execute(text(
            "CREATE UNIQUE INDEX uq_clues_legacy_number "
            "ON clues (legacy_number) WHERE legacy_number IS NOT NULL"
        ))
        # The shift changed legacy_numbers, so every clue that moved needs its
        # author/solver pill recomputed.
        await _recompute_pills(db, recompute_from)
        # Don't double-apply legacy_number in the field loop below.
        update_data.pop("legacy_number", None)

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
                    Clue.legacy_number < clue.legacy_number,
                )
            )
        ).scalar() + 1
        clue.clues_by_solver_so_far = (
            await db.execute(
                select(func.count(Clue.id)).where(
                    Clue.solver == clue.solver,
                    Clue.legacy_number < clue.legacy_number,
                )
            )
        ).scalar() + 1 if clue.solver else None

    await db.commit()
    await db.refresh(clue)
    return clue