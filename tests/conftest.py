"""Explicit, fail-closed PostgreSQL opt-in for integration tests.

Set CCCC_TEST_DATABASE_URL to a disposable database named cccc_test_*.
Never infer permission to write from the application's DATABASE_URL.
"""
import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


def _test_url():
    raw = os.environ.get("CCCC_TEST_DATABASE_URL")
    if raw is None:
        return None
    url = make_url(raw)  # Invalid explicit configuration must fail, not skip.
    if url.get_backend_name() != "postgresql" or not (url.database or "").startswith("cccc_test_"):
        raise ValueError("CCCC_TEST_DATABASE_URL must name a PostgreSQL cccc_test_* database")
    return url.set(drivername="postgresql+asyncpg")


TEST_DATABASE_URL = _test_url()
if TEST_DATABASE_URL is not None:
    # This must happen before test modules import app.db.session/settings.
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL.render_as_string(hide_password=False)
    os.environ["DATABASE_URL_SYNC"] = TEST_DATABASE_URL.set(
        drivername="postgresql+psycopg2"
    ).render_as_string(hide_password=False)


@pytest_asyncio.fixture(autouse=True)
async def explicit_database_health():
    """Fail unreachable explicit DBs, and release pooled connections on their loop.

    The older ingest-date tests import the global sessionmaker. Disposing on
    each test's loop keeps its connections from leaking into the next loop.
    """
    if TEST_DATABASE_URL is None:
        yield
        return
    from app.db.session import engine
    await engine.dispose()
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    try:
        yield
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def numbering_db():
    if TEST_DATABASE_URL is None:
        pytest.skip("set CCCC_TEST_DATABASE_URL to opt in to PostgreSQL integration tests")
    from app.db.session import Base
    from app.models.clue import User
    # Register all real FK tables, not just the tables a particular test uses.
    from app.models import sequence, solution  # noqa: F401

    schema = "numbering_" + uuid.uuid4().hex
    admin = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    engine = None
    created = False
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        engine = create_async_engine(
            TEST_DATABASE_URL,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text(
                "CREATE UNIQUE INDEX uq_clues_legacy_number "
                "ON clues (legacy_number) WHERE legacy_number IS NOT NULL"
            ))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            actor = User(se_user_id=987654, display_name="numbering-editor", is_editor=True)
            db.add(actor)
            await db.commit()
        yield SimpleNamespace(engine=engine, sessions=sessions, actor=actor, schema=schema)
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            # Only the UUID schema created above is ours to destroy.
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
