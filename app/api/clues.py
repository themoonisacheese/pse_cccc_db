"""CRUD + search API for clues."""

import csv
import io
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, or_, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.models.clue import Clue, ClueEditHistory
from app.schemas.clue import (
    ClueCreate,
    ClueUpdate,
    ClueOut,
    ClueListOut,
    StatsOut,
    AuthorStat,
    DateStat,
)

router = APIRouter(prefix="/clues", tags=["clues"])
settings = get_settings()

# Valid sort columns
SORT_COLUMNS = {
    "legacy_number": Clue.legacy_number,
    "clue_length": Clue.clue_length,
    "author": Clue.author,
    "solver": Clue.solver,
    "clue_date": Clue.clue_date,
    "answer_length": Clue.answer_length,
    "id": Clue.id,
}


def _check_write_perm(request: Request):
    """Check that the authenticated user has write permissions."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not user.is_editor and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to edit clues. A diamond moderator may grant you these permissions.",
        )
    return user


@router.get("", response_model=ClueListOut)
async def list_clues(
    request: Request,
    q: Optional[str] = Query(None, description="Full-text search query"),
    author: Optional[str] = Query(None, description="Filter by author (ILIKE)"),
    solver: Optional[str] = Query(None, description="Filter by solver (ILIKE)"),
    solution: Optional[str] = Query(None, description="Filter by solution (ILIKE)"),
    date_from: Optional[date] = Query(None, description="Clues on or after this date"),
    date_to: Optional[date] = Query(None, description="Clues on or before this date"),
    legacy_number: Optional[int] = Query(None, description="Exact legacy number"),
    transcript_link: Optional[str] = Query(None, description="Exact transcript link"),
    order_by: str = Query("legacy_number", description="Sort column"),
    order_dir: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Search and list clues with pagination. Open to all."""

    # Build query
    query = select(Clue)
    count_query = select(func.count(Clue.id))

    # Full-text search
    if q:
        tsquery = func.plainto_tsquery("english", q)
        query = query.where(Clue.search_vector.op("@@")(tsquery))
        count_query = count_query.where(Clue.search_vector.op("@@")(tsquery))

    # ILIKE filters
    if author:
        query = query.where(Clue.author.ilike(f"%{author}%"))
        count_query = count_query.where(Clue.author.ilike(f"%{author}%"))

    if solver:
        query = query.where(Clue.solver.ilike(f"%{solver}%"))
        count_query = count_query.where(Clue.solver.ilike(f"%{solver}%"))

    if solution:
        query = query.where(Clue.solution.ilike(f"%{solution}%"))
        count_query = count_query.where(Clue.solution.ilike(f"%{solution}%"))

    # Date range
    if date_from:
        query = query.where(Clue.clue_date >= date_from)
        count_query = count_query.where(Clue.clue_date >= date_from)
    if date_to:
        query = query.where(Clue.clue_date <= date_to)
        count_query = count_query.where(Clue.clue_date <= date_to)

    # Exact matches
    if legacy_number:
        query = query.where(Clue.legacy_number == legacy_number)
        count_query = count_query.where(Clue.legacy_number == legacy_number)
    if transcript_link:
        query = query.where(Clue.transcript_link == transcript_link)
        count_query = count_query.where(Clue.transcript_link == transcript_link)

    # Ordering
    sort_col = SORT_COLUMNS.get(order_by, Clue.legacy_number)
    if order_dir == "desc":
        sort_col = sort_col.desc()
    query = query.order_by(sort_col)

    # Pagination
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    # Execute
    result = await db.execute(query)
    clues = result.scalars().all()

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    total_pages = (total + page_size - 1) // page_size

    return ClueListOut(
        clues=[ClueOut.model_validate(c) for c in clues],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/{clue_id}", response_model=ClueOut)
async def get_clue(clue_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single clue by its database ID."""
    result = await db.execute(select(Clue).where(Clue.id == clue_id))
    clue = result.scalar_one_or_none()
    if not clue:
        raise HTTPException(status_code=404, detail="Clue not found")
    return ClueOut.model_validate(clue)


@router.post("", response_model=ClueOut, status_code=201)
async def create_clue(
    request: Request,
    clue_in: ClueCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new clue. Requires room-owner privileges."""
    user = _check_write_perm(request)
    clue = Clue(**clue_in.model_dump())
    clue.entered_by_user_id = user.id
    db.add(clue)
    await db.commit()
    await db.refresh(clue)
    return ClueOut.model_validate(clue)


@router.put("/{clue_id}", response_model=ClueOut)
async def update_clue(
    request: Request,
    clue_id: int,
    clue_in: ClueUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update an existing clue. Requires room-owner privileges."""
    user = _check_write_perm(request)
    result = await db.execute(select(Clue).where(Clue.id == clue_id))
    clue = result.scalar_one_or_none()
    if not clue:
        raise HTTPException(status_code=404, detail="Clue not found")

    update_data = clue_in.model_dump(exclude_unset=True)
    for field, new_value in update_data.items():
        old_value = getattr(clue, field, None)
        if old_value != new_value:
            # Record edit history
            history = ClueEditHistory(
                clue_id=clue.id,
                edited_by_user_id=user.id,
                field_name=field,
                old_value=str(old_value) if old_value is not None else None,
                new_value=str(new_value) if new_value is not None else None,
            )
            db.add(history)
            setattr(clue, field, new_value)

    await db.commit()
    await db.refresh(clue)
    return ClueOut.model_validate(clue)


@router.delete("/{clue_id}", status_code=204)
async def delete_clue(
    request: Request,
    clue_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a clue. Requires admin privileges."""
    user = _check_write_perm(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required to delete")
    result = await db.execute(select(Clue).where(Clue.id == clue_id))
    clue = result.scalar_one_or_none()
    if not clue:
        raise HTTPException(status_code=404, detail="Clue not found")
    await db.delete(clue)
    await db.commit()


# ── Stats ─────────────────────────────────────────────────


@router.get("/stats/overview", response_model=StatsOut)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Aggregate statistics about the clue archive."""
    # Total clues
    total = (await db.execute(select(func.count(Clue.id)))).scalar()

    # Total distinct authors / solvers
    total_authors = (
        await db.execute(select(func.count(func.distinct(Clue.author))))
    ).scalar()
    total_solvers = (
        await db.execute(
            select(func.count(func.distinct(Clue.solver))).where(Clue.solver.isnot(None))
        )
    ).scalar()

    # Most prolific author
    prolific = (
        await db.execute(
            select(Clue.author, func.count().label("cnt"))
            .group_by(Clue.author)
            .order_by(func.count().desc())
            .limit(1)
        )
    ).first()

    # Longest solution
    longest_sol = (
        await db.execute(
            select(Clue.solution)
            .order_by(func.length(Clue.solution).desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # Longest clue text
    longest_clue = (
        await db.execute(
            select(Clue.clue_text)
            .order_by(func.length(Clue.clue_text).desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # Most repeated solution
    most_repeated = (
        await db.execute(
            select(Clue.solution, func.count().label("cnt"))
            .group_by(Clue.solution)
            .order_by(func.count().desc())
            .limit(1)
        )
    ).first()

    # Date with most clues
    busiest_date = (
        await db.execute(
            select(Clue.clue_date, func.count().label("cnt"))
            .where(Clue.clue_date.isnot(None))
            .group_by(Clue.clue_date)
            .order_by(func.count().desc())
            .limit(1)
        )
    ).first()

    # First and last clue dates
    first_date = (
        await db.execute(select(func.min(Clue.clue_date)))
    ).scalar()
    last_date = (
        await db.execute(select(func.max(Clue.clue_date)))
    ).scalar()

    return StatsOut(
        total_clues=total,
        total_authors=total_authors,
        total_solvers=total_solvers,
        most_prolific_author=(
            AuthorStat(name=prolific[0], count=prolific[1]) if prolific else None
        ),
        longest_solution=longest_sol,
        longest_clue=longest_clue,
        most_repeated_solution=most_repeated[0] if most_repeated else None,
        date_with_most_clues=(
            DateStat(date=busiest_date[0], count=busiest_date[1])
            if busiest_date
            else None
        ),
        first_clue_date=first_date,
        last_clue_date=last_date,
    )


@router.get("/stats/authors")
async def get_author_stats(db: AsyncSession = Depends(get_db)):
    """Author leaderboard — returns all authors with their clue counts."""
    result = await db.execute(
        select(Clue.author, func.count().label("clue_count"))
        .group_by(Clue.author)
        .order_by(func.count().desc())
    )
    return [{"author": row[0], "count": row[1]} for row in result]


@router.get("/stats/solvers")
async def get_solver_stats(db: AsyncSession = Depends(get_db)):
    """Solver leaderboard — returns all solvers with their solve counts."""
    result = await db.execute(
        select(Clue.solver, func.count().label("solve_count"))
        .where(Clue.solver.isnot(None))
        .group_by(Clue.solver)
        .order_by(func.count().desc())
    )
    return [{"solver": row[0], "count": row[1]} for row in result]


@router.get("/export.csv")
async def export_csv(db: AsyncSession = Depends(get_db)):
    """Export all clues as CSV. Content is CC-BY-SA (Puzzling SE contributors)."""
    result = await db.execute(
        select(Clue).order_by(Clue.legacy_number)
    )
    clues = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "legacy_number", "clue_text", "clue_length", "author",
        "solver", "override_solver", "solution", "answer_length",
        "one_word_answer_length", "answer_count", "explanation",
        "clue_date", "number_on_date", "clues_by_author_so_far",
        "transcript_link",
    ])
    for c in clues:
        writer.writerow([
            c.legacy_number, c.clue_text, c.clue_length, c.author,
            c.solver or "", c.override_solver or "", c.solution, c.answer_length,
            c.one_word_answer_length or "", c.answer_count or "",
            c.explanation or "", c.clue_date or "", c.number_on_date or "",
            c.clues_by_author_so_far or "", c.transcript_link or "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cccc_archive.csv"},
    )
