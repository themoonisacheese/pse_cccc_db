"""Content-negotiated .png routes for embeddable CCCC pages.

Serves a server-rendered PNG preview to image fetchers (SE chat) and
the real HTML page directly to browsers — all from URLs ending in .png.
This makes .png the canonical URL so users can copy-paste from the
browser address bar straight into SE chat.

Detection uses sec-fetch-dest (proven reliable from live SE chat logs):
  - sec-fetch-dest: image    → image fetcher → serve PNG
  - sec-fetch-dest: document  → browser      → serve HTML page
  - fallback: Accept header    → text/html = browser, image/* = fetcher

Routes:
  /clue/{clue_id}.png        — clue detail (canonical URL)
  /user/{username}.png       — user profile (canonical URL)
  /sequences/{sequence_id}.png — sequence detail (canonical URL)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload

from app.db.session import async_session
from app.models.clue import Clue
from app.models.sequence import Sequence
from app.services.preview_renderer import (
    render_clue,
    render_user_profile,
    render_sequence,
)

router = APIRouter()


def _is_fetcher(request: Request) -> bool:
    """Return True if this request should get a PNG (image fetcher, not browser)."""
    sec_fetch_dest = request.headers.get("sec-fetch-dest", "").lower()
    accept = request.headers.get("accept", "").lower()

    if sec_fetch_dest == "image":
        return True
    if sec_fetch_dest == "document":
        return False

    # Fallback: Accept header
    html_pos = accept.find("text/html")
    img_pos = accept.find("image/")
    if html_pos != -1 and (img_pos == -1 or html_pos < img_pos):
        return False
    if img_pos != -1:
        return True

    return True  # default: treat as fetcher (safe for embeds)


# ── Clue detail .png ─────────────────────────────────────────

async def _get_clue_data(clue_id: int):
    async with async_session() as db:
        result = await db.execute(select(Clue).where(Clue.id == clue_id))
        clue = result.scalar_one_or_none()
        if not clue:
            return None, 0

        solution_use_count = 0
        if clue.solution and clue.solution.strip():
            exact = clue.solution.strip().lower()
            solution_use_count = (
                await db.execute(
                    select(func.count(Clue.id)).where(func.lower(Clue.solution) == exact)
                )
            ).scalar() or 0
        return clue, solution_use_count


@router.get("/clue/{clue_id}.png")
async def clue_png(request: Request, clue_id: int):
    if not _is_fetcher(request):
        # Browser → serve the real HTML page (delegates to main.py's route)
        from app.main import _serve_clue_detail_html
        return await _serve_clue_detail_html(request, clue_id)

    clue, solution_use_count = await _get_clue_data(clue_id)
    if not clue:
        return Response(content=b"", status_code=404, media_type="image/png")

    return Response(
        content=render_clue(clue, solution_use_count),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.head("/clue/{clue_id}.png")
async def clue_png_head(request: Request, clue_id: int):
    if not _is_fetcher(request):
        return Response(content=b"", media_type="text/html",
                        headers={"Cache-Control": "no-store"})
    return Response(content=b"", media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})


# ── User profile .png ────────────────────────────────────────

async def _get_user_data(username: str):
    from app.main import _streak_from_numbers, _day_streak

    async with async_session() as db:
        authored = (await db.execute(
            select(func.count(Clue.id)).where(Clue.author == username)
        )).scalar() or 0

        solved = (await db.execute(
            select(func.count(Clue.id)).where(Clue.solver == username)
        )).scalar() or 0

        if authored == 0 and solved == 0:
            return None

        first_date = (await db.execute(
            select(func.min(Clue.clue_date)).where(
                or_(Clue.author == username, Clue.solver == username)
            )
        )).scalar()
        last_date = (await db.execute(
            select(func.max(Clue.clue_date)).where(
                or_(Clue.author == username, Clue.solver == username)
            )
        )).scalar()

        # Leaderboard rank
        all_ranks = (await db.execute(
            select(Clue.author, func.count().label("cnt"))
            .group_by(Clue.author)
            .order_by(func.count().desc())
        )).all()
        rank = None
        for i, (author, _) in enumerate(all_ranks, 1):
            if author == username:
                rank = i
                break

        # Streaks
        streak_nums = [r[0] for r in (await db.execute(
            select(Clue.legacy_number)
            .where(Clue.author == username, Clue.legacy_number.isnot(None))
            .order_by(Clue.legacy_number)
        )).all()]
        max_streak, current_streak = _streak_from_numbers(streak_nums)

        active_dates = [r[0] for r in (await db.execute(
            select(Clue.clue_date)
            .where(
                or_(Clue.author == username, Clue.solver == username),
                Clue.clue_date.isnot(None),
            )
            .order_by(Clue.clue_date)
        )).all()]
        max_day_streak, _ = _day_streak(active_dates)

    return {
        "username": username, "authored": authored, "solved": solved,
        "rank": rank, "max_streak": max_streak, "current_streak": current_streak,
        "max_day_streak": max_day_streak, "first_date": first_date,
        "last_date": last_date,
    }


@router.get("/user/{username}.png")
async def user_png(request: Request, username: str):
    if not _is_fetcher(request):
        from app.main import _serve_user_profile_html
        return await _serve_user_profile_html(request, username)

    data = await _get_user_data(username)
    if not data:
        return Response(content=b"", status_code=404, media_type="image/png")

    return Response(
        content=render_user_profile(
            data["username"], data["authored"], data["solved"], data["rank"],
            data["max_streak"], data["current_streak"], data["max_day_streak"],
            data["first_date"], data["last_date"],
        ),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.head("/user/{username}.png")
async def user_png_head(request: Request, username: str):
    if not _is_fetcher(request):
        return Response(content=b"", media_type="text/html",
                        headers={"Cache-Control": "no-store"})
    return Response(content=b"", media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})


# ── Sequence detail .png ─────────────────────────────────────

async def _get_sequence_data(sequence_id: int):
    async with async_session() as db:
        result = await db.execute(
            select(Sequence)
            .options(selectinload(Sequence.clues))
            .where(Sequence.id == sequence_id)
        )
        seq = result.scalar_one_or_none()
        if not seq:
            return None, None
        clues = sorted((seq.clues or []), key=lambda c: (c.legacy_number or 0))
        return seq, clues


@router.get("/sequences/{sequence_id}.png")
async def sequence_png(request: Request, sequence_id: int):
    if not _is_fetcher(request):
        from app.main import _serve_sequence_detail_html
        return await _serve_sequence_detail_html(request, sequence_id)

    seq, clues = await _get_sequence_data(sequence_id)
    if not seq:
        return Response(content=b"", status_code=404, media_type="image/png")

    return Response(
        content=render_sequence(seq, clues),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.head("/sequences/{sequence_id}.png")
async def sequence_png_head(request: Request, sequence_id: int):
    if not _is_fetcher(request):
        return Response(content=b"", media_type="text/html",
                        headers={"Cache-Control": "no-store"})
    return Response(content=b"", media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})
