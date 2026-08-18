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
from app.services.ingest.accept import AcceptResult, decide, strip_html
from app.services.ingest import state as ingest_state
from app.services.ingest.window import from_event
from app.services.ingest.window_manager import WindowManager
from app.services.ingest.persist import process_window
from app.services.ingest.llm_worker import LlmWorker
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


# Milestones the bot announces in chat when an author reaches them.
MILESTONE_CLUE_NUMBERS = {1, 50}


def _milestone_message(clue: Clue, author_id: int | None) -> str | None:
    """Return a chat message to post for a milestone clue, else None.

    `clues_by_author_so_far` is the "Nth clue by this author" pill computed
    by `ingest_clue` — the exact same metric the admin Clue Milestones page
    uses.  When it lands on a milestone (1st or 50th), we congratulate the
    author in the room.
    """
    n = clue.clues_by_author_so_far
    if n not in MILESTONE_CLUE_NUMBERS:
        return None
    if n == 1:
        text = f"🎉 @{clue.author} just posted their very first clue! (#{clue.legacy_number})"
    else:
        text = f"🎉 @{clue.author} just posted their 50th clue! (#{clue.legacy_number})"
    # Reply to the author so they get a ping.
    if author_id is not None:
        return f":{author_id} {text}"
    return text


async def _ingest_message(
    event,
    window_manager: WindowManager | None = None,
    room=None,
) -> None:
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
    author_id = getattr(event, "user_id", None)
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

        # Window management: this genuinely-new clue closes the *previous*
        # clue's window (which now spans up to this clue) and opens a fresh
        # one for this clue.  Runs on the loop so detection stays off the
        # sync callback path.  Only fires for new clues (after dedupe).
        if window_manager is not None:
            window_manager.on_clue(message_id, author_id)

        actor = await _get_or_create_bot_user(db)
        clue = await ingest_clue(
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

    # Announce milestones in chat.  sechat's `send` is sync + network-bound,
    # so run it off the loop (fire-and-forget; never blocks ingest).
    if room is not None:
        msg = _milestone_message(clue, author_id)
        if msg is not None:
            try:
                await asyncio.to_thread(room.send, msg)
                logger.info("Announced milestone for %s (clue #%s)", author, clue.legacy_number)
            except Exception:  # noqa: BLE001 — announcement must never break ingest
                logger.exception("Failed to post milestone announcement for %s", author)


class IngestDaemon:
    """Owns the sechat room connection and bridges callbacks to the loop."""

    def __init__(self, room_id: int):
        self.room_id = room_id
        self.bot = None
        self.room = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Window accumulation for solution ingest.  Detection runs on window
        # close (when the next clue arrives) and never blocks ingest.
        self.window_manager = WindowManager(on_window_closed=self._on_window_closed)
        # In-process background LLM worker (non-blocking; drains pending_llm).
        self.llm_worker = LlmWorker()

    def _on_window_closed(self, window, next_clue_author_id):
        """Handle a completed (A, B) window.  Runs on the asyncio loop.

        Schedules the (async) detection + persistence on the loop.  This is
        fire-and-forget: detection never blocks ingest, and any LLM work is
        deferred to the pending_llm queue.
        """
        logger.info(
            "Window closed for clue msg=%s: %d messages, next-clue author=%s",
            window.clue_message_id, len(window), next_clue_author_id,
        )
        if self._loop is None or not self._loop.is_running():
            return

        async def _run():
            async with async_session() as db:
                await process_window(db, window)

        asyncio.run_coroutine_threadsafe(_run(), self._loop)

    def _on_message(self, event):
        """Sync callback, runs in sechat's room thread."""
        # Ignore the bot's own messages.
        if getattr(event, "user_id", None) == self.bot.userID:
            return
        if self._loop is None or not self._loop.is_running():
            return
        # sechat delivers content as raw HTML; strip tags once so both the
        # window buffer and the accept rule see clean plain text.
        event = event._replace(content=strip_html(getattr(event, "content", "") or ""))
        # Append to the open window (cheap, structured).
        self.window_manager.on_message(from_event(event))
        asyncio.run_coroutine_threadsafe(
            _ingest_message(event, self.window_manager, self.room), self._loop
        )

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
        # Start the in-process LLM worker (drains pending_llm in the
        # background; never blocks ingest).
        self.llm_worker.start()
        # Keep the event loop alive; sechat's room thread does the work.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            logger.info("Shutting down ingest daemon")
            await self.llm_worker.stop()
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
