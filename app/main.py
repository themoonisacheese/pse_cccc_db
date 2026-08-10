"""Main FastAPI application factory."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
from app.api.auth import get_current_user, router as auth_router
from app.api.clues import router as clues_router
from app.api.transcript import router as transcript_router
from app.db.session import async_session, engine, Base

settings = get_settings()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create tables and apply FTS migration."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Apply FTS trigger migration
        migration_path = BASE_DIR.parent / "scripts" / "migration_001_fts.sql"
        if migration_path.exists():
            from sqlalchemy import text
            migration_sql = migration_path.read_text()
            await conn.execute(text(migration_sql))
    yield
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
            query = query.where(Clue.solution.ilike(f"%{solution}%"))
            count_query = count_query.where(Clue.solution.ilike(f"%{solution}%"))

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


@app.get("/add", response_class=HTMLResponse)
async def add_clue_form(request: Request):
    """Form to add a new clue (room owners only)."""
    from app.api.clues import _check_write_perm

    user = getattr(request.state, "user", None)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/api/auth/login?redirect_after=/add", status_code=303)
    if not user.is_room_owner and not user.is_admin:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "Room owner privileges required", "user": user},
            status_code=403,
        )

    return templates.TemplateResponse(
        "add_clue.html",
        {"request": request, "user": user},
    )


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    """Statistics page."""
    from sqlalchemy import select, func
    from app.models.clue import Clue

    user = getattr(request.state, "user", None)
    async with async_session() as db:
        total = (await db.execute(select(func.count(Clue.id)))).scalar()
        total_authors = (
            await db.execute(select(func.count(func.distinct(Clue.author))))
        ).scalar()

        # Top 20 authors
        top_authors = (
            await db.execute(
                select(Clue.author, func.count().label("cnt"))
                .group_by(Clue.author)
                .order_by(func.count().desc())
                .limit(20)
            )
        ).all()

        # Top 20 solvers
        top_solvers = (
            await db.execute(
                select(Clue.solver, func.count().label("cnt"))
                .where(Clue.solver.isnot(None))
                .group_by(Clue.solver)
                .order_by(func.count().desc())
                .limit(20)
            )
        ).all()

        first_date = (await db.execute(select(func.min(Clue.clue_date)))).scalar()
        last_date = (await db.execute(select(func.max(Clue.clue_date)))).scalar()

    return templates.TemplateResponse(
        "stats.html",
        {
            "request": request,
            "user": user,
            "total_clues": total,
            "total_authors": total_authors,
            "top_authors": top_authors,
            "top_solvers": top_solvers,
            "first_date": first_date,
            "last_date": last_date,
        },
    )
