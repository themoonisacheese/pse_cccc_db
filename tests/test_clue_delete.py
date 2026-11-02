"""Real PostgreSQL regressions for atomic, serialized legacy numbering.

Only CCCC_TEST_DATABASE_URL opts into database writes; see conftest.py.
The API tests mount the actual router without production startup or HTTP calls.
"""
import asyncio
from collections import Counter
from datetime import date

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select, text

from app.db.session import Base, get_db
from app.models.clue import Clue, ClueEditHistory
from app.models.sequence import Sequence, clue_sequences
from app.models.solution import ClueCandidate, PendingLlm
from app.services import clue_service as service

pytestmark = pytest.mark.asyncio


async def seed(env, count=6, *, null_tail=True):
    authors = ["Alice", "Bob", "Alice", "Bob", "Alice", "Alice"]
    solvers = ["Sam", "Sam", None, "Sue", "", "Sam"]
    rows = []
    ac, sc = Counter(), Counter()
    async with env.sessions() as db:
        for i in range(count):
            author, solver = authors[i % 6], solvers[i % 6]
            ac[author] += 1
            if solver:
                sc[solver] += 1
            clue = Clue(
                id=100 + i * 7, legacy_number=i + 1,
                clue_text=f"CCCC Stable payload {i} (5)", author=author,
                solver=solver, solution="WORDS", explanation=f"explanation {i}",
                message_id=10000 + i * 11, transcript_link=f"https://example.invalid/{i}",
                clue_date=date(2020, 1, i + 1), number_on_date=1,
                override_solver=f"override {i}", source="ingest",
                entered_by_user_id=env.actor.id,
                clues_by_author_so_far=ac[author],
                clues_by_solver_so_far=sc[solver] if solver else None,
            )
            db.add(clue)
            rows.append(clue)
        if null_tail:
            clue = Clue(id=900, legacy_number=None, clue_text="Unnumbered (5)",
                        author="Alice", solver="Sam", message_id=90000,
                        clues_by_author_so_far=77, clues_by_solver_so_far=88)
            db.add(clue)
            rows.append(clue)
        await db.commit()
    return rows


async def snapshot(env):
    async with env.sessions() as db:
        return {
            table.name: [dict(row) for row in (await db.execute(
                select(table).order_by(*table.primary_key.columns)
            )).mappings()]
            for table in Base.metadata.sorted_tables
        }


async def index_identity(env):
    async with env.sessions() as db:
        row = (await db.execute(text(
            "SELECT c.oid, i.indisvalid, i.indisunique, pg_get_expr(i.indpred, i.indrelid) "
            "FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = 'uq_clues_legacy_number'"
        ))).one()
    assert row[1] is True and row[2] is True
    assert "legacy_number IS NOT NULL" in row[3]
    return row[0]


async def assert_numbering(env, expected):
    async with env.sessions() as db:
        rows = (await db.execute(select(Clue).order_by(Clue.legacy_number.nulls_last()))).scalars().all()
        assert {row.id: row.legacy_number for row in rows} == expected
        ac, sc = Counter(), Counter()
        for row in rows:
            if row.legacy_number is None:
                continue
            ac[row.author] += 1
            if row.solver:
                sc[row.solver] += 1
            assert row.clues_by_author_so_far == ac[row.author], row.id
            assert row.clues_by_solver_so_far == (sc[row.solver] if row.solver else None), row.id


async def add_relations(env, rows):
    async with env.sessions() as db:
        sequence = Sequence(name="shared theme", legacy_key=123, seq_type="theme")
        db.add(sequence)
        await db.flush()
        for row in rows:
            db.add(ClueEditHistory(clue_id=row.id, edited_by_user_id=env.actor.id,
                                   field_name="explanation", old_value="old", new_value="new"))
            db.add(ClueCandidate(clue_id=row.id, solution="WORDS", solver="Sam",
                                 confidence=.8, signals={"preserve": row.id}))
            db.add(PendingLlm(clue_id=row.id, task_type="wordplay",
                              payload={"preserve": row.id}))
            await db.execute(clue_sequences.insert().values(clue_id=row.id, sequence_id=sequence.id))
        db.add(PendingLlm(clue_id=None, task_type="unattached", payload={"keep": True}))
        await db.commit()


async def delete(env, clue_id):
    async with env.sessions() as db:
        return await service.delete_clue(db, actor=env.actor, clue_id=clue_id)


@pytest.mark.parametrize("position", [0, 2, 5, "null"])
async def test_delete_compacts_only_later_numbers_and_preserves_payload(numbering_db, position):
    env = numbering_db
    rows = await seed(env)
    target = rows[-1] if position == "null" else rows[position]
    before = await snapshot(env)
    index_oid = await index_identity(env)
    assert await delete(env, target.id) is None
    expected = {r.id: (r.legacy_number - 1 if target.legacy_number is not None
                       and r.legacy_number is not None and r.legacy_number > target.legacy_number
                       else r.legacy_number) for r in rows if r.id != target.id}
    await assert_numbering(env, expected)
    after = await snapshot(env)
    mutable = {"legacy_number", "clues_by_author_so_far", "clues_by_solver_so_far", "updated_at"}
    original = {r["id"]: r for r in before["clues"]}
    for row in after["clues"]:
        assert {k: v for k, v in row.items() if k not in mutable} == {
            k: v for k, v in original[row["id"]].items() if k not in mutable}
        if row["legacy_number"] is None:
            assert row == original[row["id"]]  # Unnumbered pills are untouched.
    assert await index_identity(env) == index_oid  # Never DROP/recreate the live index.


async def test_delete_only_clue_and_repeat_missing_are_noops(numbering_db):
    env = numbering_db
    rows = await seed(env, 1, null_tail=False)
    await delete(env, rows[0].id)
    await assert_numbering(env, {})
    before = await snapshot(env)
    for missing in [rows[0].id, rows[0].id, 1234567]:
        with pytest.raises(ValueError, match="not found"):
            await delete(env, missing)
        assert await snapshot(env) == before


async def test_missing_in_nonempty_database_changes_nothing(numbering_db):
    env = numbering_db
    await seed(env)
    before = await snapshot(env)
    with pytest.raises(ValueError, match="not found"):
        await delete(env, 999999)
    assert await snapshot(env) == before


async def test_delete_cascades_only_target_relations(numbering_db):
    env = numbering_db
    rows = await seed(env)
    await add_relations(env, rows)
    before = await snapshot(env)
    target = rows[2]
    await delete(env, target.id)
    after = await snapshot(env)
    for name in ["clue_sequences", "clue_candidates", "clue_edit_history", "pending_llm"]:
        assert after[name] == [r for r in before[name] if r["clue_id"] != target.id]
    assert after["sequences"] == before["sequences"]
    assert after["users"] == before["users"]


@pytest.mark.parametrize("phase", ["shift", "pills"])
async def test_failure_rolls_back_deleted_row_numbers_and_cascades(numbering_db, monkeypatch, phase):
    env = numbering_db
    rows = await seed(env)
    await add_relations(env, rows)
    before = await snapshot(env)
    index_oid = await index_identity(env)
    name = "_shift_legacy_numbers" if phase == "shift" else "_recompute_pills"
    original = getattr(service, name)

    async def fail_after_mutation(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected numbering failure")

    monkeypatch.setattr(service, name, fail_after_mutation)
    async with env.sessions() as db:
        with pytest.raises(RuntimeError, match="injected numbering failure"):
            await service.delete_clue(db, actor=env.actor, clue_id=rows[2].id)
        # The service, not session context-manager teardown, must roll back.
        assert (await db.execute(select(Clue.id).where(Clue.id == rows[2].id))).scalar_one() == rows[2].id
    assert await snapshot(env) == before
    assert await index_identity(env) == index_oid


async def test_midchain_insert_counts_inserted_row_and_keeps_index(numbering_db):
    env = numbering_db
    rows = await seed(env)
    oid = await index_identity(env)
    async with env.sessions() as db:
        clue = await service.ingest_clue(db, actor=env.actor, clue_text="Inserted (5)",
                                         author="Alice", solver="Sam", legacy_number=2)
    expected = {r.id: (r.legacy_number + 1 if r.legacy_number is not None
                        and r.legacy_number >= 2 else r.legacy_number) for r in rows}
    expected[clue.id] = 2
    await assert_numbering(env, expected)
    assert await index_identity(env) == oid


@pytest.mark.parametrize("old,new", [(5, 2), (1, 5)])
async def test_midchain_edit_reorders_and_recomputes_pills(numbering_db, old, new):
    env = numbering_db
    rows = await seed(env)
    target = rows[old - 1]
    oid = await index_identity(env)
    async with env.sessions() as db:
        await service.update_clue(db, actor=env.actor, clue_id=target.id,
                                  update_data={"legacy_number": new})
    expected = {r.id: r.legacy_number for r in rows}
    for row in rows:
        n = row.legacy_number
        if n is not None and new <= n < old:
            expected[row.id] = n + 1
        elif n is not None and old < n <= new:
            expected[row.id] = n - 1
    expected[target.id] = new
    await assert_numbering(env, expected)
    assert await index_identity(env) == oid


@pytest.mark.parametrize("permission,status", [(None, 401), ("reader", 403), ("editor", 204)])
async def test_actual_delete_route_auth_and_repeat_404(numbering_db, monkeypatch, permission, status):
    from app.api import clues as api
    from app.models.clue import User

    env = numbering_db
    rows = await seed(env)
    before = await snapshot(env)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    user = env.actor if permission == "editor" else (
        User(se_user_id=55, display_name="reader", is_editor=False, is_admin=False)
        if permission else None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request.state.user = user
        return await call_next(request)

    async def db_override():
        async with env.sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_override
    calls = []
    original = api.service_delete_clue

    async def tracked_delete(db, *, actor, clue_id):
        calls.append((actor.id, clue_id))
        return await original(db, actor=actor, clue_id=clue_id)

    monkeypatch.setattr(api, "service_delete_clue", tracked_delete)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(f"/api/clues/{rows[2].id}")
        assert response.status_code == status, response.text
        if status == 204:
            assert response.content == b""
            assert calls == [(env.actor.id, rows[2].id)]
            for missing in [rows[2].id, rows[2].id, 999999]:
                response = await client.delete(f"/api/clues/{missing}")
                assert response.status_code == 404, response.text
                assert "not found" in response.json()["detail"].lower()
            expected = {r.id: (r.legacy_number - 1 if r.legacy_number is not None
                                and r.legacy_number > 3 else r.legacy_number)
                        for r in rows if r.id != rows[2].id}
            await assert_numbering(env, expected)
        else:
            assert calls == []
            assert await snapshot(env) == before


async def wait_for_database_lock(env, pid):
    """Observe a real blocked advisory lock, never guess ordering with sleep."""
    async with asyncio.timeout(10):
        async with env.sessions() as observer:
            while True:
                waiting = (await observer.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE pid = :pid AND locktype = 'advisory' "
                    "AND objid = 996744001 AND NOT granted)"
                ), {"pid": pid})).scalar_one()
                if waiting:
                    return
                # Each SQL roundtrip yields to both writer tasks.


@pytest.mark.parametrize("first_operation,second_operation", [
    ("delete", "delete"), ("delete", "append"), ("delete", "update"),
    ("append", "append"), ("insert", "delete"),
])
async def test_concurrent_writers_read_current_numbers_after_lock(numbering_db, monkeypatch, first_operation, second_operation):
    env = numbering_db
    rows = await seed(env, null_tail=False)
    acquired = asyncio.Event()
    release = asyncio.Event()
    second_attempted = asyncio.Event()
    original = service._acquire_renumber_lock
    tasks = []
    async with env.sessions() as first_db, env.sessions() as second_db:
        # Deliberately keep an ORM identity-map entry with a soon-stale number.
        stale = await second_db.get(Clue, rows[4].id)
        assert stale.legacy_number == 5
        second_pid = (await second_db.execute(text("SELECT pg_backend_pid()"))).scalar_one()

        async def gated_lock(db):
            if db is second_db:
                second_attempted.set()
            await original(db)
            if db is first_db:
                acquired.set()
                await release.wait()

        monkeypatch.setattr(service, "_acquire_renumber_lock", gated_lock)
        try:
            if first_operation == "delete":
                first = asyncio.create_task(service.delete_clue(
                    first_db, actor=env.actor, clue_id=rows[1].id))
            else:
                first = asyncio.create_task(service.ingest_clue(
                    first_db, actor=env.actor, clue_text="First writer (5)",
                    author="Bob", solver="Sam",
                    legacy_number=2 if first_operation == "insert" else None))
            tasks.append(first)
            await asyncio.wait_for(acquired.wait(), 10)
            if second_operation == "delete":
                second = asyncio.create_task(service.delete_clue(
                    second_db, actor=env.actor, clue_id=stale.id))
            elif second_operation == "update":
                second = asyncio.create_task(service.update_clue(
                    second_db, actor=env.actor, clue_id=stale.id,
                    update_data={"legacy_number": 2}))
            else:
                second = asyncio.create_task(service.ingest_clue(
                    second_db, actor=env.actor, clue_text="Automatic append (5)",
                    author="Alice", solver="Sam", message_id=555555, source="ingest"))
            tasks.append(second)
            # If append never asks for the lock, this times out and fails.
            await asyncio.wait_for(second_attempted.wait(), 10)
            await wait_for_database_lock(env, second_pid)
            assert not first.done() and not second.done()
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 20)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    remaining = [row.id for row in rows]
    if first_operation == "delete":
        remaining.remove(rows[1].id)
    elif first_operation == "append":
        remaining.append(results[0].id)
    else:
        remaining.insert(1, results[0].id)
    if second_operation == "delete":
        remaining.remove(rows[4].id)
    elif second_operation == "update":
        remaining.remove(rows[4].id)
        remaining.insert(1, rows[4].id)
    else:
        remaining.append(results[1].id)
    expected = {clue_id: i for i, clue_id in enumerate(remaining, 1)}
    await assert_numbering(env, expected)
    await index_identity(env)


@pytest.mark.parametrize("to_numbered", [False, True])
async def test_edit_numbered_null_transitions(numbering_db, to_numbered):
    env = numbering_db
    rows = await seed(env)
    target = rows[-1] if to_numbered else rows[2]
    old = target.legacy_number
    new = 2 if to_numbered else None
    oid = await index_identity(env)
    async with env.sessions() as db:
        await service.update_clue(db, actor=env.actor, clue_id=target.id,
                                  update_data={"legacy_number": new})
    expected = {r.id: r.legacy_number for r in rows}
    for row in rows:
        n = row.legacy_number
        if n is not None and to_numbered and n >= new:
            expected[row.id] = n + 1
        elif n is not None and not to_numbered and n > old:
            expected[row.id] = n - 1
    expected[target.id] = new
    await assert_numbering(env, expected)
    if not to_numbered:
        async with env.sessions() as db:
            row = await db.get(Clue, target.id)
            assert row.clues_by_author_so_far is None
            assert row.clues_by_solver_so_far is None
    assert await index_identity(env) == oid


async def test_edit_author_and_solver_recomputes_both_old_and_new_groups(numbering_db):
    env = numbering_db
    rows = await seed(env)
    oid = await index_identity(env)
    async with env.sessions() as db:
        await service.update_clue(db, actor=env.actor, clue_id=rows[1].id,
                                  update_data={"author": "Alice", "solver": "Sue"})
    await assert_numbering(env, {r.id: r.legacy_number for r in rows})
    async with env.sessions() as db:
        row = await db.get(Clue, rows[1].id)
        assert (row.author, row.solver) == ("Alice", "Sue")
    assert await index_identity(env) == oid


@pytest.mark.parametrize("operation", ["delete", "insert"])
async def test_restore_failure_rolls_back_staged_nulls_before_same_session_commit(numbering_db, monkeypatch, operation):
    env = numbering_db
    rows = await seed(env)
    await add_relations(env, rows)
    before = await snapshot(env)
    oid = await index_identity(env)
    observed_restore = False
    async with env.sessions() as db:
        original_execute = db.execute

        async def fail_before_restore(statement, *args, **kwargs):
            nonlocal observed_restore
            if "FROM unnest" in str(statement):
                observed_restore = True
                # Verify this is precisely the transient NULL state, not an
                # exception before the shift or after successful restoration.
                staged = set((await original_execute(select(Clue.id).where(
                    Clue.legacy_number.is_(None)))).scalars())
                shifted = rows[3:6] if operation == "delete" else rows[1:6]
                assert staged == {900, *(row.id for row in shifted)}
                if operation == "delete":
                    assert (await original_execute(select(Clue.id).where(
                        Clue.id == rows[2].id))).scalar_one_or_none() is None
                raise RuntimeError("injected pre-restore failure")
            return await original_execute(statement, *args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(db, "execute", fail_before_restore)
            with pytest.raises(RuntimeError, match="injected pre-restore failure"):
                if operation == "delete":
                    await service.delete_clue(db, actor=env.actor, clue_id=rows[2].id)
                else:
                    await service.ingest_clue(db, actor=env.actor, clue_text="Must not persist (5)",
                                             author="Alice", solver="Sam", legacy_number=2)
            assert observed_restore
            # A caller committing after catching the error must not persist a
            # half-shifted archive (nor deletion cascades).
            await db.commit()
        assert await snapshot(env) == before
        assert await index_identity(env) == oid
        # Prove the same session and original index are reusable after rollback.
        added = await service.ingest_clue(db, actor=env.actor, clue_text="Recovery append (5)",
                                          author="Alice", solver="Sam")
        expected = {row.id: row.legacy_number for row in rows}
        expected[added.id] = 7
        await assert_numbering(env, expected)
        assert await index_identity(env) == oid
