"""Admin API: manage editors (add/remove write access)."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.clue import User, Clue
from app.schemas.clue import UserOut
from app.services import badge_check

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(request: Request):
    """Ensure the current user is an admin (diamond moderator)."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Admin privileges required. Only diamond moderators can manage editors.",
        )
    return user


@router.get("/editors", response_model=list[UserOut])
async def list_editors(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all users with editor privileges."""
    _require_admin(request)
    result = await db.execute(
        select(User).where(User.is_editor == True).order_by(User.display_name)
    )
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@router.get("/users", response_model=list[UserOut])
async def list_all_users(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all known users (for the admin to search and promote)."""
    _require_admin(request)
    result = await db.execute(
        select(User).order_by(User.display_name)
    )
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@router.post("/editors/{se_user_id}", response_model=UserOut)
async def add_editor(
    request: Request,
    se_user_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Grant editor privileges to a user by their SE user ID."""
    _require_admin(request)
    result = await db.execute(
        select(User).where(User.se_user_id == se_user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"User with SE ID {se_user_id} not found. They must log in at least once first.",
        )
    user.is_editor = True
    await db.commit()
    await db.refresh(user)
    logger.info(f"Admin {request.state.user.se_user_id} granted editor to {se_user_id}")
    return UserOut.model_validate(user)


@router.delete("/editors/{se_user_id}", response_model=UserOut)
async def remove_editor(
    request: Request,
    se_user_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Revoke editor privileges from a user."""
    _require_admin(request)
    result = await db.execute(
        select(User).where(User.se_user_id == se_user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"User with SE ID {se_user_id} not found.",
        )
    if user.is_admin:
        raise HTTPException(
            status_code=400,
            detail="Cannot revoke editor privileges from an admin (diamond moderator).",
        )
    user.is_editor = False
    await db.commit()
    await db.refresh(user)
    logger.info(f"Admin {request.state.user.se_user_id} revoked editor from {se_user_id}")
    return UserOut.model_validate(user)


# ── Badge catch-up (CCCC contributor / champion) ─────────────


@router.get("/badges/status", response_model=dict)
async def badge_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Compute CCCC badge eligibility vs. actual grant status.

    Only accessible by admins (diamond moderators). Returns the authors
    who *should* have the CCCContributor / CCCChampion badge but have
    not yet been granted it (or whose status is unknown because the
    author couldn't be resolved to an SE user, or the SE API did not
    answer for them).
    """
    _require_admin(request)

    # Tally each author's cumulative clue count (their qualifying metric).
    author_counts = (await db.execute(
        select(Clue.author, func.max(Clue.clues_by_author_so_far).label("cnt"))
        .group_by(Clue.author)
    )).all()
    authors = {a: int(c or 0) for a, c in author_counts}

    names = list(authors.keys())
    se_ids = await badge_check.resolve_se_user_ids(names, db)

    results: list[dict] = []
    for slug in ("contributor", "champion"):
        res = await badge_check.check_badge_for_authors(slug, authors, se_ids, db)
        results.append({
            "badge": res.badge_label,
            "threshold_clues": res.threshold_clues,
            "granted": [
                {"author": e.author, "clues": e.clues, "se_user_id": e.se_user_id}
                for e in res.granted
            ],
            "missed": [
                {"author": e.author, "clues": e.clues, "se_user_id": e.se_user_id}
                for e in res.missed
            ],
            "unknown": [
                {"author": e.author, "clues": e.clues, "se_user_id": e.se_user_id}
                for e in res.unknown
            ],
        })

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }
