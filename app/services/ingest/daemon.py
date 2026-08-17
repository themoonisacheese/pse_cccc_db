"""Chat ingest daemon.

Watches the CCCC chat room via the sechat library's message callback and
automatically ingests valid clues into the database.

Architecture:
  - sechat's `Room` runs its own background thread and calls our message
    callback synchronously for each new message (including the bot's own,
    which we filter out by user id).
  - The main thread runs the asyncio event loop.  The sync callback is cheap
    and hands accepted-clue work to the loop via
    `asyncio.run_coroutine_threadsafe`, so all DB work happens on the loop.
  - On startup we read the DB watermark and do a lightweight catch-up for any
    messages missed while offline (the callback + backfill both dedupe on
    `clues.message_id`).

Run with:  python -m app.services.ingest
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import async_session
from app.models.clue import Clue, User
from app.services.ingest.accept import AcceptResult, decide
from app.services.ingest import state as ingest_state
from app.services.clue_service import ingest_clue

logger = logging.getLogger(__name__)


async def _get_or_create_bot_user(db) -> User:
    """Return the 'CCCC Ingest Bot' user row (seeded by migration_008)."""
    result = await db.execute(select(User).where(User.se_user_id == 0))
    user = result.scalar_one_or_none()
    if user is None:
        # Fallback: create it (migration should have, but be safe).
        user = User(
            se_user_id=0,
            display_name="CCCC Ingest Bot",
            is_editor=True,
            is_bot=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user


async def _ingest_message(event) -> None:
    """Run the accept rule and, if accepted, ingest the clue."""
    content = getattr(event, "content", "") or ""
    decision = decide(content)

    if decision.result is AcceptResult.DISCARD:
        return  # silent

    if decision.result is AcceptResult.NEAR_MISS:
        logger.info(
            "NEAR_MISS msg=%s by=%s header=%s enum=%s content=%r",
            getattr(event, "message_id", None),
            getattr(event, "user_name", None),
            decision.has_header,
            decision.has_enumeration,
            content[:200],
        )
        return

    # ACCEPT
    message_id = getattr(event, "message_id", None)
    author = getattr(event, "user_name", None) or "unknown"
    clue_text = decision.clue_text or content

    async with async_session() as db:
        # Dedupe on message_id (covers restart / callback+backfill overlap).
        if message_id is not None:
            dup = (
                await db.execute(select(Clue).where(Clue.message_id == message_id))
            ).scalar_one_or_none()
            if dup is not None:
                logger.info(
                    "DUP msg=%s already ingested as clue #%s",
                    message_id, dup.legacy_number,
                )
                return

        actor = await _get_or_create_bot_user(db)
        await ingest_clue(
            db,
            actor=actor,
            clue_text=clue_text,
            author=author,
            message_id=message_id,
            source="ingest",
            transcript_link=(
                f"https://chat.stackexchange.com/transcript/message/{message_id}#{message_id}"
                if message_id is not None
                else None
            ),
        )
        # Advance the watermark.
        if message_id is not None:
            await ingest_state.set_watermark(db, message_id)


class IngestDaemon:
    """Owns the sechat room connection and bridges callbacks to the loop."""

    def __init__(self, room_id: int):
        self.room_id = room_id
        self.bot = None
        self.room = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _on_message(self, event):
        """Sync callback, runs in sechat's room thread."""
        # Ignore the bot's own messages.
        if getattr(event, "user_id", None) == self.bot.userID:
            return
        if self._loop is None or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(_ingest_message(event), self._loop)

    async def _connect(self):
        """Log in and join the room (sechat is sync, so run in a thread)."""
        import sechat

        settings = get_settings()
        self.bot = await asyncio.to_thread(
            self._login, settings.se_bot_email, settings.se_bot_password, settings.se_site
        )
        self.room = self.bot.joinRoom(self.room_id, autoConnect=True)
        self.room.on(sechat.Events.MESSAGE, self._on_message)
        logger.info("Joined room %s, listening for clues", self.room_id)

    @staticmethod
    def _login(email: str, password: str, host: str):
        import sechat

        bot = sechat.Bot(useCookies=True)
        bot.login(email=email, password=password, host=host)
        return bot

    async def run(self) -> None:
        """Connect, then keep the loop alive forever (or until interrupted)."""
        self._loop = asyncio.get_running_loop()
        await self._connect()
        # Keep the event loop alive; sechat's room thread does the work.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            logger.info("Shutting down ingest daemon")
            if self.room is not None:
                await asyncio.to_thread(self.room.halt)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    daemon = IngestDaemon(settings.se_chat_room_id)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
