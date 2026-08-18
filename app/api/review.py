"""Review API for the solution-ingest (Stage 2) feature.

Endpoints for editors to review candidate solutions for a clue:
  * GET  /review/clues/{clue_id}/candidates — list candidates for a clue.
  * POST /review/candidates/{candidate_id}/approve — accept a candidate as
    the clue's solution (writes solution/solver/explanation to the clue,
    updates the solver pill, prunes the clue's candidates to the top 2).
  * POST /review/candidates/{candidate_id}/reject — dismiss a candidate.
  * POST /review/clues/{clue_id}/manual-solve — write a solution directly to
    an unsolved clue (for the "all candidates rejected, editor has a
    different answer" case), without going through the candidate flow.

All require editor/admin privileges.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.clue import Clue
from app.models.solution import ClueCandidate
from app.services.clue_service import update_clue as service_update_clue

router = APIRouter(prefix="/review", tags=["review"])


def _check_write_perm(request: Request):
    """Check that the authenticated user has write permissions."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not user.is_editor and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to review clues. A diamond moderator may grant you these permissions.",
        )
    return user


@router.get("/clues/{clue_id}/candidates")
async def list_candidates(
    clue_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List candidate solutions for a clue, sorted by confidence (desc).

    Only pending candidates are shown (approved/rejected are pruned/kept for
    calibration but not surfaced in the review queue).
    """
    _check_write_perm(request)
    result = await db.execute(
        select(ClueCandidate)
        .where(
            ClueCandidate.clue_id == clue_id,
            ClueCandidate.status == "pending",
        )
        .order_by(ClueCandidate.confidence.desc())
    )
    candidates = result.scalars().all()
    return [
        {
            "id": c.id,
            "solution": c.solution,
            "solver": c.solver,
            "explanation": c.explanation,
            "confidence": c.confidence,
            "signals": c.signals,
            "source_message_id": c.source_message_id,
        }
        for c in candidates
    ]


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate(
    candidate_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Approve a candidate as the clue's solution.

    Writes the candidate's solution/solver/explanation onto the clue (via
    update_clue so the solver pill recomputes), marks the candidate approved,
    and prunes the clue's remaining candidates to the top 2 by confidence
    (per the plan: keep calibration signal, drop stale noise).
    """
    user = _check_write_perm(request)
    result = await db.execute(
        select(ClueCandidate).where(ClueCandidate.id == candidate_id)
    )
    cand = result.scalar_one_or_none()
    if not cand:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if cand.status != "pending":
        raise HTTPException(status_code=409, detail="Candidate already resolved")

    # Write onto the clue (recomputes solver pill + records edit history).
    await service_update_clue(
        db,
        actor=user,
        clue_id=cand.clue_id,
        update_data={
            "solution": cand.solution,
            "solver": cand.solver,
            "explanation": cand.explanation,
        },
    )

    cand.status = "approved"
    await db.flush()

    # Prune the clue's other candidates to the top 2 by confidence.
    others = (
        await db.execute(
            select(ClueCandidate)
            .where(
                ClueCandidate.clue_id == cand.clue_id,
                ClueCandidate.id != cand.id,
                ClueCandidate.status == "pending",
            )
            .order_by(ClueCandidate.confidence.desc())
        )
    ).scalars().all()
    for stale in others[2:]:
        stale.status = "rejected"

    await db.commit()
    return {"status": "approved", "candidate_id": cand.id}


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(
    candidate_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Dismiss a candidate (marks it rejected; kept for calibration)."""
    _check_write_perm(request)
    result = await db.execute(
        select(ClueCandidate).where(ClueCandidate.id == candidate_id)
    )
    cand = result.scalar_one_or_none()
    if not cand:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if cand.status != "pending":
        raise HTTPException(status_code=409, detail="Candidate already resolved")
    cand.status = "rejected"
    await db.commit()
    return {"status": "rejected", "candidate_id": cand.id}


@router.post("/clues/{clue_id}/manual-solve")
async def manual_solve(
    clue_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    solution: str = Form(""),
    solver: str = Form(""),
    explanation: str = Form(""),
):
    """Write a solution directly to an unsolved clue.

    For the case where all candidates were rejected and the editor has a
    different answer.  Reads solution/solver/explanation from the form body.
    """
    user = _check_write_perm(request)
    solution = solution.strip()
    if not solution:
        raise HTTPException(status_code=400, detail="solution is required")

    result = await db.execute(select(Clue).where(Clue.id == clue_id))
    clue = result.scalar_one_or_none()
    if not clue:
        raise HTTPException(status_code=404, detail="Clue not found")

    await service_update_clue(
        db,
        actor=user,
        clue_id=clue_id,
        update_data={
            "solution": solution,
            "solver": solver.strip() or None,
            "explanation": explanation.strip() or None,
        },
    )
    return {"status": "solved", "clue_id": clue_id}
