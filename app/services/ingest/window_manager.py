"""Window manager for the solution-ingest pipeline.

Owns the open window(s) inside the ingest daemon.  When a clue is accepted
by the daemon, the manager:

  * closes the window belonging to the *previous* clue (that window is now
    complete: it spans from the previous clue up to this one), hands it to
    the detection pipeline, and
  * opens a fresh window for the newly-accepted clue.

Every non-clue message the daemon sees is appended to the current open
window.  The manager is deliberately in-memory only — windows are lost on a
daemon restart, which is accepted (see the plan).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.services.ingest.window import Window, WindowMessage

logger = logging.getLogger(__name__)


class WindowManager:
    """Tracks the current open window and closes/processes it on the next clue."""

    def __init__(
        self,
        on_window_closed: Optional[Callable[[Window, Optional[int]], None]] = None,
    ):
        # Callback signature: on_window_closed(window, next_clue_author_id).
        # next_clue_author_id is the chat user_id of the clue that *closed* the
        # window — per the solver-identity invariant, that user is the solver
        # of the window's clue.
        self._on_window_closed = on_window_closed
        self._open: Optional[Window] = None

    @property
    def open_window(self) -> Optional[Window]:
        return self._open

    def on_message(self, msg: WindowMessage) -> None:
        """Append a message to the current open window, if any."""
        if self._open is not None and not self._open.closed:
            self._open.add(msg)

    def on_clue(self, clue_message_id: int, clue_author_id: int | None = None) -> None:
        """Handle a newly-accepted clue.

        Closes the previous clue's window (if any) and opens a fresh one for
        this clue.  The closed window is passed to the detection callback
        along with the *closing* clue's author id (the solver, per the
        solver-identity invariant).
        """
        # Close the previous window and process it.
        if self._open is not None and not self._open.closed:
            # The solver of this window's clue is the author of the clue that
            # is closing it (the *next* clue) — solver-identity invariant.
            self._open.solver_user_id = clue_author_id
            # The closing clue's own message was appended to the window by
            # `_on_message` before this ran; drop it so the window is the
            # exclusive (A, B) span, not inclusive of B.
            self._open.discard(clue_message_id)
            self._open.close()
            if self._on_window_closed is not None:
                try:
                    self._on_window_closed(self._open, clue_author_id)
                except Exception:  # noqa: BLE001 — detection must never break ingest
                    logger.exception("Error processing closed window")

        # Open a fresh window for this clue.
        self._open = Window(
            clue_message_id=clue_message_id,
            clue_author_id=clue_author_id,
        )

    def reset(self) -> None:
        """Drop any open window (used on daemon restart)."""
        self._open = None
