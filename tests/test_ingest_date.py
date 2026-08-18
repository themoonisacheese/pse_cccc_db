"""Regression tests for the ingest date-defaulting behaviour.

Ingested clues carry no date in the SE chat payload, so `ingest_clue` must
default `clue_date` to today.  These tests exercise the real ORM + Postgres
path and are skipped when the database is unreachable (e.g. running the pure
unit suite locally without a DB).
"""

from datetime import date

import pytest
from sqlalchemy import select

from app.db.session import async_session
from app.models.clue import Clue, User
from app.services.clue_service import ingest_clue


pytestmark = pytest.mark.asyncio


async def _bot_user(db) -> User:
    result = await db.execute(select(User).where(User.is_bot.is_(True)).limit(1))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(se_user_id=999999, display_name="test-bot", is_bot=True)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user


async def _cleanup(db, clue_id: int) -> None:
    await db.execute(Clue.__table__.delete().where(Clue.id == clue_id))
    await db.commit()


async def test_ingested_clue_defaults_date_to_today():
    """A clue ingested without a clue_date gets today's date."""
    try:
        async with async_session() as db:
            actor = await _bot_user(db)
            clue = await ingest_clue(
                db,
                actor=actor,
                clue_text="CCCC A clue with no date (5)",
                author="test-author",
                message_id=900001,
                source="ingest",
            )
            try:
                assert clue.clue_date is not None, "clue_date should default to today"
                assert clue.clue_date == date.today(), (
                    f"expected today ({date.today()}), got {clue.clue_date}"
                )
            finally:
                await _cleanup(db, clue.id)
    except Exception as exc:
        if _is_unreachable(exc):
            pytest.skip(f"database unreachable: {exc}")
        raise


async def test_ingested_clue_honours_explicit_date():
    """A caller-supplied clue_date is respected (not clobbered by today)."""
    explicit = date(2020, 5, 17)
    try:
        async with async_session() as db:
            actor = await _bot_user(db)
            clue = await ingest_clue(
                db,
                actor=actor,
                clue_text="CCCC A clue with an explicit date (5)",
                author="test-author",
                message_id=900002,
                source="ingest",
                clue_date=explicit,
            )
            try:
                assert clue.clue_date == explicit, (
                    f"expected {explicit}, got {clue.clue_date}"
                )
            finally:
                await _cleanup(db, clue.id)
    except Exception as exc:
        if _is_unreachable(exc):
            pytest.skip(f"database unreachable: {exc}")
        raise


def _is_unreachable(exc: Exception) -> bool:
    """Heuristic: connection/name-resolution errors mean the DB isn't here."""
    import sqlalchemy.exc as sa_exc

    if isinstance(exc, sa_exc.OperationalError):
        return True
    if isinstance(exc, sa_exc.InterfaceError):
        return True
    if isinstance(exc, ConnectionError):
        return True
    # asyncpg/psycopg connection errors often surface as OSError/TimeoutError.
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    return False
