"""CRUD API for sequences (themes + author sequences).

Sequences are many-to-many groups of clues. A clue can belong to any number
of sequences. `seq_type` distinguishes author sequences (setter-revealed runs,
rendered in their own color) from loose themes.

Editing requires editor (room-owner) privileges; reads are open to all.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, func, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.clue import Clue
from app.models.sequence import Sequence
from app.schemas.sequence import (
    SequenceCreate,
    SequenceUpdate,
    SequenceOut,
    SequenceListOut,
    SequenceMembershipUpdate,
)

router = APIRouter(prefix="/sequences", tags=["sequences"])


def _check_write_perm(request: Request):
    """Check that the authenticated user is an editor or admin."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not user.is_editor and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to edit sequences. A diamond moderator may grant you these permissions.",
        )
    return user


def _serialize(seq: Sequence) -> dict:
    """Build a SequenceOut-compatible dict, computing clue_count + clue refs."""
    clues = sorted(seq.clues or [], key=lambda c: (c.legacy_number is None, c.legacy_number or 0))
    return {
        "id": seq.id,
        "name": seq.name,
        "seq_type": seq.seq_type,
        "author": seq.author,
        "color": seq.color,
        "description": seq.description,
        "legacy_key": seq.legacy_key,
        "created_at": seq.created_at,
        "updated_at": seq.updated_at,
        "clue_count": len(clues),
        "clues": [
            {
                "id": c.id,
                "legacy_number": c.legacy_number,
                "clue_text": c.clue_text,
                "author": c.author,
                "solution": c.solution,
            }
            for c in clues
        ],
    }


@router.get("", response_model=SequenceListOut)
async def list_sequences(
    request: Request,
    seq_type: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
):
    """List sequences. Optionally filter by type ('author'|'theme') or text."""
    query = select(Sequence)
    if seq_type in ("author", "theme", "tag"):
        query = query.where(Sequence.seq_type == seq_type)
    if q:
        query = query.where(Sequence.name.ilike(f"%{q}%"))
    query = query.order_by(Sequence.id).limit(min(limit, 1000))

    result = await db.execute(query)
    seqs = result.scalars().unique().all()

    # Total matching the same (type[, name]) filter, ignoring the page limit.
    count_query = select(func.count(Sequence.id))
    if seq_type in ("author", "theme", "tag"):
        count_query = count_query.where(Sequence.seq_type == seq_type)
    if q:
        count_query = count_query.where(Sequence.name.ilike(f"%{q}%"))
    total = (await db.execute(count_query)).scalar()

    return SequenceListOut(
        sequences=[_serialize(s) for s in seqs],
        total=total,
    )


@router.get("/{sequence_id}", response_model=SequenceOut)
async def get_sequence(
    sequence_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Sequence).where(Sequence.id == sequence_id))
    seq = result.scalar_one_or_none()
    if not seq:
        raise HTTPException(status_code=404, detail="Sequence not found")
    return _serialize(seq)


@router.post("", response_model=SequenceOut, status_code=201)
async def create_sequence(
    request: Request,
    seq_in: SequenceCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new sequence. Requires editor privileges."""
    _check_write_perm(request)
    seq = Sequence(**seq_in.model_dump(exclude_unset=True))
    if not seq.seq_type:
        seq.seq_type = "theme"
    db.add(seq)
    await db.commit()
    await db.refresh(seq)
    return _serialize(seq)


@router.put("/{sequence_id}", response_model=SequenceOut)
async def update_sequence(
    request: Request,
    sequence_id: int,
    seq_in: SequenceUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a sequence's name/type/author/color/description."""
    _check_write_perm(request)
    result = await db.execute(select(Sequence).where(Sequence.id == sequence_id))
    seq = result.scalar_one_or_none()
    if not seq:
        raise HTTPException(status_code=404, detail="Sequence not found")

    data = seq_in.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(seq, field, value)
    await db.commit()
    await db.refresh(seq)
    return _serialize(seq)


@router.delete("/{sequence_id}", status_code=204)
async def delete_sequence(
    request: Request,
    sequence_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a sequence (membership links are cascaded). Requires admin."""
    user = _check_write_perm(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required to delete")
    result = await db.execute(select(Sequence).where(Sequence.id == sequence_id))
    seq = result.scalar_one_or_none()
    if not seq:
        raise HTTPException(status_code=404, detail="Sequence not found")
    await db.delete(seq)
    await db.commit()


# ── Membership ──────────────────────────────────────────────


@router.put("/{sequence_id}/clues", response_model=SequenceOut)
async def set_sequence_clues(
    request: Request,
    sequence_id: int,
    body: SequenceMembershipUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Replace the full clue membership of a sequence (idempotent)."""
    _check_write_perm(request)
    result = await db.execute(select(Sequence).where(Sequence.id == sequence_id))
    seq = result.scalar_one_or_none()
    if not seq:
        raise HTTPException(status_code=404, detail="Sequence not found")

    clue_ids = list(dict.fromkeys(body.clue_ids))  # dedupe, preserve order
    valid_ids: set[int] = set()
    if clue_ids:
        found = (await db.execute(select(Clue.id).where(Clue.id.in_(clue_ids)))).scalars().all()
        valid_ids = set(found)

    # Clear current membership, then insert the valid clue ids.
    from app.models.sequence import clue_sequences

    await db.execute(
        sa_delete(clue_sequences).where(clue_sequences.c.sequence_id == sequence_id)
    )
    for cid in clue_ids:
        if cid in valid_ids:
            await db.execute(clue_sequences.insert().values(sequence_id=sequence_id, clue_id=cid))
    await db.commit()
    db.expire(seq)  # discard cached relationship state so _serialize reloads membership
    return _serialize(seq)


@router.post("/{sequence_id}/clues/{clue_id}", response_model=SequenceOut)
async def add_clue_to_sequence(
    request: Request,
    sequence_id: int,
    clue_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Add a clue to a sequence. Requires editor privileges."""
    _check_write_perm(request)
    result = await db.execute(select(Sequence).where(Sequence.id == sequence_id))
    seq = result.scalar_one_or_none()
    if not seq:
        raise HTTPException(status_code=404, detail="Sequence not found")
    clue_result = await db.execute(select(Clue).where(Clue.id == clue_id))
    clue = clue_result.scalar_one_or_none()
    if not clue:
        raise HTTPException(status_code=404, detail="Clue not found")
    if clue not in seq.clues:
        seq.clues.append(clue)
        await db.commit()
    await db.refresh(seq)
    return _serialize(seq)


@router.delete("/{sequence_id}/clues/{clue_id}", response_model=SequenceOut)
async def remove_clue_from_sequence(
    request: Request,
    sequence_id: int,
    clue_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Remove a clue from a sequence. Requires editor privileges."""
    _check_write_perm(request)
    result = await db.execute(select(Sequence).where(Sequence.id == sequence_id))
    seq = result.scalar_one_or_none()
    if not seq:
        raise HTTPException(status_code=404, detail="Sequence not found")
    clue = next((c for c in seq.clues if c.id == clue_id), None)
    if clue:
        seq.clues.remove(clue)
        await db.commit()
    return _serialize(seq)
