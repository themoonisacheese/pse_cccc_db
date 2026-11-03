"""One-time CCCC tenth birthday reply in the normal room.

At-most-once delivery: a durable 'sending' claim precedes the chat POST. If the
process or network fails after that commit, inspect chat and the ledger rather
than automatically retrying a possibly successful send. No chat API supports
an idempotency key, so exactly-once delivery cannot be guaranteed.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import distinct, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.session import async_session
from app.models.clue import AnniversaryAnnouncement, Clue

logger = logging.getLogger(__name__)
# Verified from chat events: message 33517331 time_stamp=1479180806.
FIRST_CLUE = datetime.fromtimestamp(1479180806, timezone.utc)
DUE = FIRST_CLUE.replace(year=2026)
KEY = "cccc-10-years"
ROOM_ID = 14524
# Do not surprise the room with an ancient celebration after extended downtime.
GRACE = timedelta(minutes=10)


@dataclass(frozen=True)
class BirthdayStats:
    clues: int
    setters: int
    busiest_date: date | None
    busiest_count: int


async def load_stats(db) -> BirthdayStats:
    # Include archived clues (including undated entries) through the anniversary
    # date. Dates, not created_at, are the archive's historical date source.
    eligible = (Clue.clue_date.is_(None) | (
        (Clue.clue_date >= FIRST_CLUE.date()) & (Clue.clue_date <= DUE.date())
    ))
    # Historical identities are display names, as in existing archive stats.
    author = func.nullif(func.lower(func.trim(Clue.author)), "")
    totals = (await db.execute(select(func.count(Clue.id), func.count(distinct(author)))
                               .where(eligible))).one()
    busiest = (await db.execute(
        select(Clue.clue_date, func.count(Clue.id).label("n"))
        .where(eligible, Clue.clue_date.is_not(None))
        .group_by(Clue.clue_date)
        .order_by(func.count(Clue.id).desc(), Clue.clue_date.asc()).limit(1)
    )).first()
    return BirthdayStats(totals[0], totals[1], busiest[0] if busiest else None,
                         busiest[1] if busiest else 0)


def format_message(stats: BirthdayStats) -> str:
    days = (DUE - FIRST_CLUE).total_seconds() / 86400
    text = (
        ":33517331 🎉 Happy 10th birthday, CCCC! Ten years ago, this clue started it all. "
        f"Since then: **{stats.clues:,} clues**, averaging "
        f"**{stats.clues / days:.2f} clues/day**, from **{stats.setters:,} setters**!"
    )
    if stats.busiest_date is not None:
        text += (f" Our busiest day was **{stats.busiest_date:%d %B %Y}**, "
                 f"with **{stats.busiest_count:,} clues**.")
    # SE chat's normal single-message limit. Never silently truncate markup.
    if len(text) > 500:
        raise ValueError("Anniversary message exceeds SE chat's 500-character limit")
    return text


async def announce(room, *, now=None, sessions=async_session) -> bool:
    """Attempt one due announcement. Return True only for a confirmed send."""
    now = now or datetime.now(timezone.utc)
    if now < DUE:
        return False
    if room.roomID != ROOM_ID:
        raise ValueError("Anniversary announcement must go to The Sphinx's Lair")
    async with sessions() as db:
        # Also supports starting ingest before the web app's migration seeding.
        await db.execute(insert(AnniversaryAnnouncement).values(key=KEY)
                         .on_conflict_do_nothing(index_elements=["key"]))
        await db.commit()
        status = await db.scalar(select(AnniversaryAnnouncement.status)
                                 .where(AnniversaryAnnouncement.key == KEY))
        if status != "pending":
            logger.info("Anniversary already handled: %s", status)
            return False
        if now > DUE + GRACE:
            await db.execute(update(AnniversaryAnnouncement)
                             .where(AnniversaryAnnouncement.key == KEY,
                                    AnniversaryAnnouncement.status == "pending")
                             .values(status="missed"))
            await db.commit()
            logger.error("Anniversary missed its ten-minute delivery window")
            return False
        body = format_message(await load_stats(db))
        # Atomic compare-and-set: concurrent daemons cannot both send.
        claimed = await db.scalar(update(AnniversaryAnnouncement)
                                  .where(AnniversaryAnnouncement.key == KEY,
                                         AnniversaryAnnouncement.status == "pending")
                                  .values(status="sending", body=body, attempted_at=now)
                                  .returning(AnniversaryAnnouncement.key))
        await db.commit()
        if claimed is None:
            return False
    try:
        message_id = await asyncio.to_thread(room.send, body)
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            raise RuntimeError("Chat did not confirm an anniversary message ID")
    except Exception:
        # Keep 'sending' as an intentionally uncertain claim. Never auto-retry.
        logger.exception("Anniversary send uncertain: inspect chat and ledger, DO NOT blindly retry")
        return False
    async with sessions() as db:
        await db.execute(update(AnniversaryAnnouncement)
                         .where(AnniversaryAnnouncement.key == KEY)
                         .values(status="sent", message_id=message_id,
                                 sent_at=datetime.now(timezone.utc)))
        await db.commit()
    logger.info("Anniversary posted: message %s", message_id)
    return True


async def wait_until_due(*, clock=None, sleep=asyncio.sleep):
    """Recheck wall clock at least every minute, including after clock changes."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    while True:
        remaining = (DUE - clock()).total_seconds()
        if remaining <= 0:
            return
        await sleep(min(remaining, 60))


async def run_anniversary(room):
    logger.info("Anniversary armed for %s (process local %s, epoch %d) in room %s",
                DUE.isoformat(), DUE.astimezone().isoformat(), int(DUE.timestamp()), ROOM_ID)
    await wait_until_due()
    # Pre-send DB failures are safe to retry briefly. Once claimed, announce()
    # cannot send again, even if recording the successful response fails.
    for attempt in range(3):
        try:
            await announce(room)
            return
        except Exception:
            logger.exception("Anniversary task failed (attempt %d/3)", attempt + 1)
            if attempt < 2:
                await asyncio.sleep(5)
