"""In-memory window buffer for the solution-ingest pipeline.

A *window* is the set of chat messages between one clue and the next — the
span in which the solution to the first clue must appear.  Because the SE
chat APIs cannot page back into arbitrary history (the transcript HTML pages
are Cloudflare-blocked and the events API is newest-only), windows are NOT
fetched: they are accumulated live by the daemon as messages stream in via
the sechat callback.

This module provides the structured, in-memory representation of a window.
The sechat callback delivers per-message fields (message_id, user_id,
user_name, content, parent_id, parent_text, time_stamp) — NOT a blob of text
— so filtering by author and walking the reply tree are plain field/dict
operations, with no regex over message bodies.

The window is deliberately kept in memory only.  It is lost on a daemon
crash/restart, which is accepted (see the plan): after a restart we simply
start collecting again from the first new clue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class WindowMessage:
    """A single chat message captured in a window.

    Fields mirror what the sechat callback event provides.  `parent_id` is
    the SE chat reply target (None when the message is not a reply).
    """

    message_id: int
    user_id: int | None = None
    user_name: str | None = None
    content: str = ""
    parent_id: int | None = None
    parent_text: str | None = None
    parent_username: str | None = None
    timestamp: int | None = None  # unix seconds, when available

    def __repr__(self):
        return (
            f"<WindowMessage #{self.message_id} "
            f"by={self.user_name or self.user_id} "
            f"reply_to={self.parent_id}>"
        )


# Chat user IDs of known feed/bot posters whose messages are pure noise in a
# window (RSS feed posts, room bots).  Messages from these users are dropped
# before detection runs.  Populated from config where possible.
NOISE_USER_IDS: set[int] = set()


def from_event(event) -> WindowMessage:
    """Build a WindowMessage from a sechat callback event (namedtuple)."""
    return WindowMessage(
        message_id=getattr(event, "message_id", None) or 0,
        user_id=getattr(event, "user_id", None),
        user_name=getattr(event, "user_name", None),
        content=getattr(event, "content", "") or "",
        parent_id=getattr(event, "parent_id", None),
        parent_text=getattr(event, "parent_text", None),
        parent_username=getattr(event, "parent_username", None),
        timestamp=getattr(event, "time_stamp", None),
    )


class Window:
    """Accumulates the messages between two consecutive clues.

    The daemon owns one open Window per clue.  When the next clue arrives it
    calls `close()` to freeze the window, then runs the detection pipeline on
    it.  All messages are held in memory.
    """

    def __init__(self, clue_message_id: int, clue_author_id: int | None = None):
        self.clue_message_id = clue_message_id
        self.clue_author_id = clue_author_id
        # The solver of this clue = the author of the *next* clue (solver-
        # identity invariant).  Set at close time by the WindowManager, which
        # knows the closing clue's author.
        self.solver_user_id: int | None = None
        self._messages: dict[int, WindowMessage] = {}
        self._closed = False

    def add(self, msg: WindowMessage) -> None:
        """Append a message to the window (idempotent by message_id)."""
        if self._closed:
            return
        self._messages[msg.message_id] = msg

    def discard(self, message_id: int) -> None:
        """Remove a message from the window (used to drop the closing clue)."""
        self._messages.pop(message_id, None)

    def close(self) -> None:
        """Freeze the window so no further messages are accepted."""
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def messages(self) -> list[WindowMessage]:
        """All messages, ordered by message_id."""
        return [self._messages[k] for k in sorted(self._messages)]

    def by_author(self, user_id: int | None) -> list[WindowMessage]:
        """Messages posted by a given chat user ID."""
        if user_id is None:
            return []
        return [m for m in self.messages if m.user_id == user_id]

    def by_author_name(self, name: str) -> list[WindowMessage]:
        """Messages posted by a given chat display name (case-insensitive)."""
        if not name:
            return []
        return [m for m in self.messages if (m.user_name or "").lower() == name.lower()]

    def excluding_noise(self) -> list[WindowMessage]:
        """Messages with known feed/bot posters removed."""
        return [m for m in self.messages if m.user_id not in NOISE_USER_IDS]

    def replies_to(self, message_id: int) -> list[WindowMessage]:
        """Messages that are replies to a given message ID."""
        return [m for m in self.messages if m.parent_id == message_id]

    def reply_tree(self) -> dict[int, list[WindowMessage]]:
        """Build a parent -> children index over the window's messages.

        Lets the pipeline walk the reply graph: given a message, find who
        replied to it (and so on).  Only links within the window are included.
        """
        children: dict[int, list[WindowMessage]] = {}
        for m in self.messages:
            if m.parent_id is not None:
                children.setdefault(m.parent_id, []).append(m)
        return children

    def __len__(self) -> int:
        return len(self._messages)
