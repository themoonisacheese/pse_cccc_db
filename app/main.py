"""Main FastAPI application factory."""

from contextlib import asynccontextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
from app.api.admin import router as admin_router
from app.api.auth import get_current_user, router as auth_router
from app.api.clues import router as clues_router
from app.api.transcript import router as transcript_router
from app.db.session import async_session, engine, Base

settings = get_settings()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _relative_date(d):
    """Format a date as relative if < 7 days, otherwise ISO date."""
    if d is None:
        return "—"
    today = date.today()
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        return str(d)
    delta = (today - d).days
    if delta == 0:
        return "Today"
    elif delta == 1:
        return "Yesterday"
    elif delta == -1:
        return "Tomorrow"
    elif 0 < delta < 7:
        return f"{delta} days ago"
    elif -7 < delta < 0:
        return f"In {-delta} days"
    return d.isoformat()


templates.env.filters["rel_date"] = _relative_date


def _split_sql(sql: str) -> list[str]:
    """Split SQL into individual statements, respecting $$ dollar-quoting."""
    statements = []
    current = []
    in_dollar = False
    i = 0
    while i < len(sql):
        if sql[i:i+2] == "$$":
            in_dollar = not in_dollar
            current.append("$$")
            i += 2
        elif sql[i] == ";" and not in_dollar:
            stmt = "".join(current).strip()
            if stmt:
                lines = [l for l in stmt.split("\n") if not l.strip().startswith("--")]
                clean = "\n".join(lines).strip()
                if clean:
                    statements.append(clean)
            current = []
            i += 1
        else:
            current.append(sql[i])
            i += 1
    remaining = "".join(current).strip()
    if remaining:
        statements.append(remaining)
    return statements


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create tables and apply migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from sqlalchemy import text
        # Run all migrations in order
        for migration_file in ["migration_001_fts.sql", "migration_002_editors.sql", "migration_003_generated_lengths.sql", "migration_004_drop_unused_columns.sql", "migration_005_solver_so_far.sql"]:
            migration_path = BASE_DIR.parent / "scripts" / migration_file
            if migration_path.exists():
                migration_sql = migration_path.read_text()
                for stmt in _split_sql(migration_sql):
                    await conn.execute(text(stmt))
    yield
    # Close the SE Chat session if it was opened
    from app.services import se_chat_client
    await se_chat_client.close_session()
    await engine.dispose()


app = FastAPI(
    title="CCCC DB",
    description="Cryptic Clue Chat Chains archive — API and web interface",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ── Middleware: attach user to request.state ────────────────


class UserMiddleware(BaseHTTPMiddleware):
    """Attach the current user (if any) to request.state.user."""

    async def dispatch(self, request: Request, call_next):
        token = request.cookies.get("cccc_session")
        if token:
            from itsdangerous import URLSafeSerializer, BadSignature
            from sqlalchemy import select
            from app.models.clue import User

            s = URLSafeSerializer(settings.secret_key, salt="session")
            try:
                data = s.loads(token, max_age=86400 * 7)
                async with async_session() as db:
                    result = await db.execute(
                        select(User).where(User.id == data["uid"])
                    )
                    request.state.user = result.scalar_one_or_none()
            except (BadSignature, Exception):
                request.state.user = None
        else:
            request.state.user = None

        response = await call_next(request)
        return response


app.add_middleware(UserMiddleware)


# ── API routers ─────────────────────────────────────────────

app.include_router(clues_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(transcript_router, prefix="/api")


# ── Web UI routes (HTMX + Jinja2) ───────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Home page: search bar + latest clues."""
    from sqlalchemy import select, func
    from app.models.clue import Clue

    user = getattr(request.state, "user", None)
    async with async_session() as db:
        # Latest 20 clues
        result = await db.execute(
            select(Clue)
            .order_by(Clue.legacy_number.desc())
            .limit(20)
        )
        latest_clues = result.scalars().all()

        total = (await db.execute(select(func.count(Clue.id)))).scalar()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "latest_clues": latest_clues,
            "total_clues": total,
        },
    )


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = "",
    author: str = "",
    solver: str = "",
    solution: str = "",
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    page: int = 1,
    page_size: int = 50,
):
    """Search page with HTMX-powered live results."""
    from sqlalchemy import select, func
    from app.models.clue import Clue

    user = getattr(request.state, "user", None)
    async with async_session() as db:
        query = select(Clue)
        count_query = select(func.count(Clue.id))

        if q:
            tsquery = func.plainto_tsquery("english", q)
            query = query.where(Clue.search_vector.op("@@")(tsquery))
            count_query = count_query.where(Clue.search_vector.op("@@")(tsquery))
        if author:
            query = query.where(Clue.author.ilike(f"%{author}%"))
            count_query = count_query.where(Clue.author.ilike(f"%{author}%"))
        if solver:
            query = query.where(Clue.solver.ilike(f"%{solver}%"))
            count_query = count_query.where(Clue.solver.ilike(f"%{solver}%"))
        if solution:
            # Quoted search → exact match (case-insensitive); otherwise ILIKE substring.
            stripped = solution.strip()
            if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
                exact = stripped[1:-1]
                query = query.where(func.lower(Clue.solution) == func.lower(exact))
                count_query = count_query.where(func.lower(Clue.solution) == func.lower(exact))
            else:
                query = query.where(Clue.solution.ilike(f"%{solution}%"))
                count_query = count_query.where(Clue.solution.ilike(f"%{solution}%"))
        if date_from:
            query = query.where(Clue.clue_date >= date_from)
            count_query = count_query.where(Clue.clue_date >= date_from)
        if date_to:
            query = query.where(Clue.clue_date <= date_to)
            count_query = count_query.where(Clue.clue_date <= date_to)

        offset = (page - 1) * page_size
        query = query.order_by(Clue.legacy_number).offset(offset).limit(page_size)

        result = await db.execute(query)
        clues = result.scalars().all()

        total = (await db.execute(count_query)).scalar()
        total_pages = (total + page_size - 1) // page_size

    template = "partials/clue_results.html" if request.headers.get("HX-Request") else "search.html"
    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "user": user,
            "clues": clues,
            "q": q,
            "author": author,
            "solver": solver,
            "solution": solution,
            "date_from": date_from.isoformat() if date_from else "",
            "date_to": date_to.isoformat() if date_to else "",
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    )


@app.get("/clue/{clue_id}", response_class=HTMLResponse)
async def clue_detail(request: Request, clue_id: int):
    """Detailed view of a single clue."""
    from sqlalchemy import select
    from app.models.clue import Clue

    user = getattr(request.state, "user", None)
    async with async_session() as db:
        result = await db.execute(select(Clue).where(Clue.id == clue_id))
        clue = result.scalar_one_or_none()

        if not clue:
            return templates.TemplateResponse(
                "error.html",
                {"request": request, "message": "Clue not found", "user": user},
                status_code=404,
            )

    return templates.TemplateResponse(
        "clue_detail.html",
        {"request": request, "clue": clue, "user": user},
    )


@app.get("/clue/legacy/{legacy_number}", response_class=HTMLResponse)
async def clue_by_legacy(request: Request, legacy_number: int):
    """Redirect to the clue detail page by legacy number."""
    from sqlalchemy import select
    from app.models.clue import Clue
    from fastapi.responses import RedirectResponse

    async with async_session() as db:
        result = await db.execute(
            select(Clue).where(Clue.legacy_number == legacy_number)
        )
        clue = result.scalar_one_or_none()

    if not clue:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "message": f"Clue #{legacy_number} not found",
                "user": getattr(request.state, "user", None),
            },
            status_code=404,
        )

    return RedirectResponse(url=f"/clue/{clue.id}", status_code=301)


@app.get("/clue/{clue_id}/edit", response_class=HTMLResponse)
async def edit_clue_form(request: Request, clue_id: int):
    """Edit form for an existing clue (editors and admins only)."""
    from sqlalchemy import select
    from app.models.clue import Clue

    user = getattr(request.state, "user", None)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(
            url=f"/api/auth/login?redirect_after=/clue/{clue_id}/edit",
            status_code=303,
        )
    if not user.is_editor and not user.is_admin:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "message": "You don't have permission to edit clues. A diamond moderator may grant you these permissions.",
                "user": user,
            },
            status_code=403,
        )

    async with async_session() as db:
        result = await db.execute(select(Clue).where(Clue.id == clue_id))
        clue = result.scalar_one_or_none()

    if not clue:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "Clue not found", "user": user},
            status_code=404,
        )

    return templates.TemplateResponse(
        "edit_clue.html",
        {"request": request, "clue": clue, "user": user},
    )


@app.get("/add", response_class=HTMLResponse)
async def add_clue_form(request: Request):
    """Form to add a new clue (editors and admins only)."""
    user = getattr(request.state, "user", None)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/api/auth/login?redirect_after=/add", status_code=303)
    if not user.is_editor and not user.is_admin:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "message": "You don't have permission to edit clues. A diamond moderator may grant you these permissions.",
                "user": user,
            },
            status_code=403,
        )

    return templates.TemplateResponse(
        "add_clue.html",
        {"request": request, "user": user},
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin page to manage editors (admins only)."""
    user = getattr(request.state, "user", None)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/api/auth/login?redirect_after=/admin", status_code=303)
    if not user.is_admin:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "message": "Admin privileges required. Only diamond moderators can access this page.",
                "user": user,
            },
            status_code=403,
        )
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "user": user},
    )


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, period: str = "all"):
    """Statistics page."""
    from sqlalchemy import select, func
    from sqlalchemy.orm import aliased
    from app.models.clue import Clue
    from datetime import date, timedelta

    user = getattr(request.state, "user", None)

    today = date.today()
    if period == "year":
        date_from = date(today.year, 1, 1)
    elif period == "month":
        date_from = date(today.year, today.month, 1)
    elif period == "30d":
        date_from = today - timedelta(days=30)
    else:
        date_from = None

    async with async_session() as db:
        # ── Overview stats (always all-time) ──
        total = (await db.execute(select(func.count(Clue.id)))).scalar()
        distinct_people = (
            await db.execute(
                select(func.count(func.distinct(Clue.author)))
            )
        ).scalar()

        first_date = (await db.execute(select(func.min(Clue.clue_date)))).scalar()
        last_date = (await db.execute(select(func.max(Clue.clue_date)))).scalar()

        # ── Leaderboard: authored count only (filtered by period) ──
        # In the CCCC chain you must solve a clue to author the next one,
        # so authored ≈ solved for everyone — one column is enough.
        lb_q = (
            select(Clue.author.label("person"), func.count(Clue.id).label("cnt"))
            .group_by(Clue.author)
            .order_by(func.count(Clue.id).desc())
            .limit(25)
        )
        if date_from is not None:
            lb_q = lb_q.where(Clue.clue_date >= date_from)
        leaderboard = (await db.execute(lb_q)).all()

        # ── Records & curiosities ──
        longest_clue = (
            await db.execute(
                select(
                    Clue.id,
                    Clue.clue_text,
                    func.length(Clue.clue_text).label("len"),
                )
                .order_by(func.length(Clue.clue_text).desc())
                .limit(1)
            )
        ).first()

        shortest_clue = (
            await db.execute(
                select(
                    Clue.id,
                    Clue.clue_text,
                    func.length(Clue.clue_text).label("len"),
                )
                .where(func.length(Clue.clue_text) > 0)
                .order_by(func.length(Clue.clue_text).asc())
                .limit(1)
            )
        ).first()

        longest_sol = (
            await db.execute(
                select(
                    Clue.id,
                    Clue.solution,
                    func.length(Clue.solution).label("len"),
                )
                .where(Clue.solution.isnot(None))
                .order_by(func.length(Clue.solution).desc())
                .limit(1)
            )
        ).first()

        # Shortest non-empty solution (empty string is the theoretical minimum
        # and always wins, so exclude it; show it specially instead).
        shortest_sol = (
            await db.execute(
                select(
                    Clue.id,
                    Clue.solution,
                    func.length(Clue.solution).label("len"),
                )
                .where(Clue.solution.isnot(None))
                .where(func.length(Clue.solution) > 0)
                .order_by(func.length(Clue.solution).asc())
                .limit(1)
            )
        ).first()

        # Count of empty-string solutions for the special-case display
        empty_sol_count = (
            await db.execute(
                select(func.count(Clue.id)).where(
                    Clue.solution.isnot(None),
                    func.length(Clue.solution) == 0,
                )
            )
        ).scalar()

        # Most repeated solutions (top 5)
        most_repeated = (
            await db.execute(
                select(Clue.solution, func.count().label("cnt"))
                .where(Clue.solution.isnot(None))
                .group_by(Clue.solution)
                .order_by(func.count().desc())
                .limit(5)
            )
        ).all()

        # Oldest unsolved clue
        oldest_unsolved = (
            await db.execute(
                select(
                    Clue.id,
                    Clue.legacy_number,
                    Clue.clue_text,
                    Clue.author,
                    Clue.clue_date,
                )
                .where(Clue.solution.is_(None))
                .order_by(Clue.clue_date.asc())
                .limit(1)
            )
        ).first()

        # Longest time a clue went unsolved (max delta to next clue in chain)
        # Self-join on legacy_number: the "next" clue is the one that was posted
        # when someone solved the current one.  delta = next.clue_date - cur.clue_date
        NextClue = aliased(Clue)
        longest_unsolved = (
            await db.execute(
                select(
                    Clue.id,
                    Clue.legacy_number,
                    Clue.clue_text,
                    Clue.author,
                    Clue.clue_date,
                    NextClue.clue_date.label("next_date"),
                    (NextClue.clue_date - Clue.clue_date).label("delta"),
                )
                .join(
                    NextClue,
                    NextClue.legacy_number == Clue.legacy_number + 1,
                )
                .where(Clue.clue_date.isnot(None))
                .where(NextClue.clue_date.isnot(None))
                .order_by((NextClue.clue_date - Clue.clue_date).desc())
                .limit(1)
            )
        ).first()

        # Busiest days (top 5)
        busiest_days = (
            await db.execute(
                select(Clue.clue_date, func.count().label("cnt"))
                .where(Clue.clue_date.isnot(None))
                .group_by(Clue.clue_date)
                .order_by(func.count().desc())
                .limit(5)
            )
        ).all()

        # Nemeses (top 10 author→solver pairs)
        nemeses = (
            await db.execute(
                select(Clue.author, Clue.solver, func.count().label("cnt"))
                .where(Clue.solver.isnot(None))
                .group_by(Clue.author, Clue.solver)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()

        # Clues per month (for chart)
        month_expr = func.to_char(Clue.clue_date, "YYYY-MM")
        per_month = (
            await db.execute(
                select(month_expr.label("month"), func.count().label("cnt"))
                .where(Clue.clue_date.isnot(None))
                .group_by(month_expr)
                .order_by(month_expr)
            )
        ).all()
        max_monthly = max((r[1] for r in per_month), default=1)

    return templates.TemplateResponse(
        "stats.html",
        {
            "request": request,
            "user": user,
            "total_clues": total,
            "total_authors": distinct_people,
            "leaderboard": leaderboard,
            "first_date": first_date,
            "last_date": last_date,
            "period": period,
            "longest_clue": longest_clue,
            "shortest_clue": shortest_clue,
            "longest_solution": longest_sol,
            "shortest_solution": shortest_sol,
            "empty_sol_count": empty_sol_count,
            "most_repeated": most_repeated,
            "oldest_unsolved": oldest_unsolved,
            "longest_unsolved": longest_unsolved,
            "busiest_days": busiest_days,
            "nemeses": nemeses,
            "per_month": per_month,
            "max_monthly": max_monthly,
        },
    )


# ── Helper: compute max & current streak from sorted legacy numbers ──

def _streak_from_numbers(numbers: list[int]) -> tuple[int, int]:
    """Given legacy numbers (sorted ascending), compute (max_streak, current_streak).

    In the CCCC chain you can't author/solve two consecutive clues
    (someone else must post between yours), so a streak = same person
    appearing at positions n, n+2, n+4, …  Gap of exactly 2 continues
    the streak; any other gap breaks it.
    """
    if not numbers:
        return 0, 0
    numbers = sorted(set(numbers))
    max_streak = 1
    current = 1
    for i in range(1, len(numbers)):
        if numbers[i] - numbers[i - 1] == 2:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 1
    # current streak = streak ending at the user's most recent entry
    current = 1
    for i in range(len(numbers) - 1, 0, -1):
        if numbers[i] - numbers[i - 1] == 2:
            current += 1
        else:
            break
    return max_streak, current


# ── Helper: consecutive-day streak from sorted dates ──

def _day_streak(dates: list) -> tuple[int, int]:
    """Given a list of date objects (sorted), return (max_consecutive_days, current_streak)."""
    from datetime import date as date_cls, timedelta
    if not dates:
        return 0, 0
    unique = sorted(set(dates))
    max_streak = 1
    current = 1
    for i in range(1, len(unique)):
        if (unique[i] - unique[i - 1]) == timedelta(days=1):
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 1
    # current streak: count backward from today
    today = date_cls.today()
    current = 0
    for i in range(len(unique) - 1, -1, -1):
        if (today - unique[i]).days == current:
            current += 1
        elif (today - unique[i]).days > current:
            break
    return max_streak, current


@app.get("/user/{username}", response_class=HTMLResponse)
async def user_profile(request: Request, username: str):
    """User profile page: per-person stats, nemeses, streaks, history."""
    from sqlalchemy import select, func, or_
    from sqlalchemy.orm import aliased
    from app.models.clue import Clue, ClueEditHistory, User

    user = getattr(request.state, "user", None)
    NextClue = aliased(Clue)

    async with async_session() as db:
        # ── Basic counts ──
        authored = (await db.execute(
            select(func.count(Clue.id)).where(Clue.author == username)
        )).scalar() or 0

        solved = (await db.execute(
            select(func.count(Clue.id)).where(Clue.solver == username)
        )).scalar() or 0

        if authored == 0 and solved == 0:
            return templates.TemplateResponse(
                "error.html",
                {"request": request, "message": f"No clues found for user '{username}'.", "user": user},
                status_code=404,
            )

        # ── First / last activity ──
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

        # ── Leaderboard rank (by authored count) ──
        all_ranks = (await db.execute(
            select(Clue.author, func.count().label("cnt"))
            .group_by(Clue.author)
            .order_by(func.count().desc())
        )).all()
        rank = None
        total_authors = len(all_ranks)
        for i, (author, _) in enumerate(all_ranks, 1):
            if author == username:
                rank = i
                break

        # ── Edit count (if we can match a User record) ──
        edit_count = 0
        user_record = (await db.execute(
            select(User).where(User.display_name == username)
        )).first()
        if user_record:
            edit_count = (await db.execute(
                select(func.count(ClueEditHistory.id)).where(
                    ClueEditHistory.edited_by_user_id == user_record[0].id
                )
            )).scalar() or 0

        # ── Bidirectional nemeses ──
        # People this user solves the most
        solved_most = (await db.execute(
            select(Clue.author, func.count().label("cnt"))
            .where(Clue.solver == username)
            .group_by(Clue.author)
            .order_by(func.count().desc())
            .limit(5)
        )).all()

        # People who solve this user the most
        solves_you = (await db.execute(
            select(Clue.solver, func.count().label("cnt"))
            .where(Clue.author == username, Clue.solver.isnot(None))
            .group_by(Clue.solver)
            .order_by(func.count().desc())
            .limit(5)
        )).all()

        # ── Author streak (gap of 2 in legacy numbers) ──
        author_nums = [r[0] for r in (await db.execute(
            select(Clue.legacy_number)
            .where(Clue.author == username, Clue.legacy_number.isnot(None))
            .order_by(Clue.legacy_number)
        )).all()]
        author_streak, author_current = _streak_from_numbers(author_nums)

        # ── Solver streak ──
        solver_nums = [r[0] for r in (await db.execute(
            select(Clue.legacy_number)
            .where(Clue.solver == username, Clue.legacy_number.isnot(None))
            .order_by(Clue.legacy_number)
        )).all()]
        solver_streak, solver_current = _streak_from_numbers(solver_nums)

        # ── Consecutive active days ──
        active_dates = [r[0] for r in (await db.execute(
            select(Clue.clue_date)
            .where(
                or_(Clue.author == username, Clue.solver == username),
                Clue.clue_date.isnot(None),
            )
            .order_by(Clue.clue_date)
        )).all()]
        max_day_streak, current_day_streak = _day_streak(active_dates)

        # ── Longest unsolved authored clue (earliest authored, still unsolved) ──
        longest_unsolved = (await db.execute(
            select(
                Clue.id,
                Clue.legacy_number,
                Clue.clue_text,
                Clue.clue_date,
            )
            .where(Clue.author == username, Clue.solver.is_(None))
            .order_by(Clue.clue_date.asc())
            .limit(1)
        )).first()

        # ── Solution length stats (authored clues) ──
        solution_length_stats = (await db.execute(
            select(
                func.round(func.avg(Clue.answer_length), 1).label("avg"),
                func.min(Clue.answer_length).label("min"),
                func.max(Clue.answer_length).label("max"),
            ).where(Clue.author == username, Clue.answer_length.isnot(None))
        )).first()

        # ── Recent activity (last 15 clues authored or solved) ──
        recent = (await db.execute(
            select(Clue)
            .where(or_(Clue.author == username, Clue.solver == username))
            .order_by(Clue.legacy_number.desc())
            .limit(15)
        )).scalars().all()

    return templates.TemplateResponse(
        "user_profile.html",
        {
            "request": request,
            "user": user,
            "profile_name": username,
            "authored": authored,
            "solved": solved,
            "first_date": first_date,
            "last_date": last_date,
            "rank": rank,
            "total_authors": total_authors,
            "edit_count": edit_count,
            "solved_most": solved_most,
            "solves_you": solves_you,
            "author_streak": author_streak,
            "author_current": author_current,
            "solver_streak": solver_streak,
            "solver_current": solver_current,
            "max_day_streak": max_day_streak,
            "current_day_streak": current_day_streak,
            "longest_unsolved": longest_unsolved,
            "solution_length_stats": solution_length_stats,
            "recent": recent,
        },
    )


@app.get("/me", response_class=HTMLResponse)
async def my_profile(request: Request):
    """Redirect to the logged-in user's profile."""
    from fastapi.responses import RedirectResponse
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/api/auth/login?redirect_after=/me", status_code=303)
    return RedirectResponse(url=f"/user/{user.display_name}", status_code=301)
