"""Anniversary tests never log in or post to live chat."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select, text

from app.models.clue import AnniversaryAnnouncement, Clue
from app.services.ingest import anniversary as birthday


def test_verified_timestamp_and_message():
    assert birthday.FIRST_CLUE.timestamp() == 1479180806
    assert birthday.DUE == datetime(2026, 11, 15, 3, 33, 26, tzinfo=timezone.utc)
    assert (birthday.DUE - birthday.FIRST_CLUE).days == 3652
    message = birthday.format_message(birthday.BirthdayStats(10200, 170, date(2017, 1, 1), 42))
    assert message.startswith(":33517331 🎉 Happy 10th birthday, CCCC!")
    assert "**10,200 clues**" in message
    assert "**2.79 clues/day**" in message
    assert "**170 setters**" in message
    assert "**01 January 2017**" in message and "**42 clues**" in message
    assert "Thanks" not in message
    assert len(message) <= 500
    assert "busiest" not in birthday.format_message(birthday.BirthdayStats(0, 0, None, 0))


@pytest.mark.asyncio
async def test_wait_tracks_wall_clock_and_never_sends_early():
    now = birthday.DUE - timedelta(seconds=90)
    sleeps = []
    async def sleep(delay):
        nonlocal now
        assert now < birthday.DUE
        sleeps.append(delay)
        now += timedelta(seconds=delay)
        if len(sleeps) == 1:
            now -= timedelta(seconds=20)  # system clock corrected backwards
    await birthday.wait_until_due(clock=lambda: now, sleep=sleep)
    assert sleeps == [60, 50]
    assert now == birthday.DUE


@pytest.mark.asyncio
async def test_before_due_does_not_touch_db_or_chat():
    sessions = Mock(side_effect=AssertionError("must not use DB before due"))
    room = SimpleNamespace(roomID=14524, send=Mock())
    assert not await birthday.announce(room, now=birthday.DUE-timedelta(microseconds=1), sessions=sessions)
    room.send.assert_not_called()


async def seed(env):
    async with env.sessions() as db:
        for i, (author, day) in enumerate([
            ("Alice", date(2020, 1, 2)), (" alice ", date(2020, 1, 2)),
            ("Bob", date(2020, 1, 1)), ("Bob", date(2020, 1, 1)),
            ("Carol", None), ("Future", date(2026, 11, 16)),
            ("Before", date(2016, 11, 14)), ("", None),
        ]):
            db.add(Clue(clue_text=f"clue {i}", author=author, clue_date=day))
        await db.commit()


@pytest.mark.asyncio
async def test_real_aggregate_filters_ties_and_unknown_dates(numbering_db):
    await seed(numbering_db)
    async with numbering_db.sessions() as db:
        stats = await birthday.load_stats(db)
    assert stats == birthday.BirthdayStats(6, 3, date(2020, 1, 1), 2)


async def ledger(env):
    async with env.sessions() as db:
        return (await db.execute(select(AnniversaryAnnouncement))).scalar_one()


@pytest.mark.asyncio
async def test_concurrent_daemons_and_restart_only_post_once(numbering_db):
    await seed(numbering_db)
    room = SimpleNamespace(roomID=14524, send=Mock(return_value=123456789))
    results = await asyncio.gather(*[
        birthday.announce(room, now=birthday.DUE, sessions=numbering_db.sessions)
        for _ in range(3)
    ])
    assert results.count(True) == 1
    assert room.send.call_count == 1
    row = await ledger(numbering_db)
    assert row.status == "sent" and row.message_id == 123456789
    assert row.body == room.send.call_args.args[0]
    assert row.attempted_at == birthday.DUE and row.sent_at is not None
    assert not await birthday.announce(room, now=birthday.DUE, sessions=numbering_db.sessions)
    assert room.send.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, False, 0, "123", TimeoutError("possibly posted")])
async def test_uncertain_send_retains_claim_and_never_retries(numbering_db, response):
    room = SimpleNamespace(roomID=14524, send=Mock())
    if isinstance(response, Exception):
        room.send.side_effect = response
    else:
        room.send.return_value = response
    assert not await birthday.announce(room, now=birthday.DUE, sessions=numbering_db.sessions)
    row = await ledger(numbering_db)
    assert row.status == "sending" and row.message_id is None and row.body
    assert not await birthday.announce(room, now=birthday.DUE, sessions=numbering_db.sessions)
    room.send.assert_called_once()


@pytest.mark.asyncio
async def test_expired_schedule_does_not_post(numbering_db):
    room = SimpleNamespace(roomID=14524, send=Mock())
    assert not await birthday.announce(room, now=birthday.DUE+birthday.GRACE+timedelta(seconds=1),
                                       sessions=numbering_db.sessions)
    assert (await ledger(numbering_db)).status == "missed"
    room.send.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_room_is_rejected_before_db():
    with pytest.raises(ValueError, match="Sphinx"):
        await birthday.announce(SimpleNamespace(roomID=1), now=birthday.DUE)


@pytest.mark.asyncio
async def test_migration_rerun_preserves_existing_claim(numbering_db):
    # Exercise the actual migration SQL against PostgreSQL, including the web
    # runner's splitter, both fresh and after an already-attempted send.
    from app.main import _split_sql
    sql = (Path(__file__).parents[1] / "scripts/migration_013_anniversary.sql").read_text()
    async with numbering_db.sessions() as db:
        await db.execute(text("DROP TABLE anniversary_announcements"))
        for statement in _split_sql(sql):
            await db.execute(text(statement))
        row = await db.get(AnniversaryAnnouncement, birthday.KEY)
        assert row.status == "pending"
        row.status = "sending"
        row.body = "do not reset"
        await db.commit()
        for statement in _split_sql(sql):
            await db.execute(text(statement))
        await db.commit()
    row = await ledger(numbering_db)
    assert row.status == "sending" and row.body == "do not reset"


@pytest.mark.asyncio
async def test_daemon_starts_and_cancels_anniversary_task(monkeypatch):
    from app.services.ingest import daemon
    started = asyncio.Event()
    stopped = asyncio.Event()
    async def fake_anniversary(room):
        assert room.roomID == 14524
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    async def connect(self):
        self.room = SimpleNamespace(roomID=14524, halt=Mock())
    monkeypatch.setattr(daemon, "run_anniversary", fake_anniversary)
    monkeypatch.setattr(daemon.IngestDaemon, "_connect", connect)
    instance = daemon.IngestDaemon(14524)
    instance.llm_worker = SimpleNamespace(start=Mock(), stop=AsyncMock())
    task = asyncio.create_task(instance.run())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    instance.room.halt.assert_called_once()
