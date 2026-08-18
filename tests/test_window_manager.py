"""Tests for the window manager (window accumulation + close/open)."""

from app.services.ingest.window import WindowMessage
from app.services.ingest.window_manager import WindowManager


def test_accumulate_and_close_on_next_clue():
    closed = []
    mgr = WindowManager(on_window_closed=lambda w, author: closed.append((w, author)))

    # First clue opens a window.
    mgr.on_clue(100, 1)
    assert mgr.open_window is not None
    assert mgr.open_window.clue_message_id == 100

    # Messages accumulate.
    mgr.on_message(WindowMessage(message_id=101, user_id=42, content="hello"))
    mgr.on_message(WindowMessage(message_id=102, user_id=42, content="world"))
    assert len(mgr.open_window) == 2

    # Second clue closes the first window (spanning 100..102) and opens a new one.
    mgr.on_clue(200, 7)
    assert len(closed) == 1
    closed_window, next_author = closed[0]
    assert closed_window.clue_message_id == 100
    assert closed_window.solver_user_id == 7  # next-clue author = solver
    assert next_author == 7
    assert len(closed_window) == 2
    assert mgr.open_window.clue_message_id == 200


def test_closing_clue_message_is_excluded():
    """The window is the exclusive (A, B) span: the closing clue B's own
    message (appended by _on_message before on_clue runs) must be dropped."""
    closed = []
    mgr = WindowManager(on_window_closed=lambda w, author: closed.append((w, author)))

    mgr.on_clue(100, 1)
    mgr.on_message(WindowMessage(message_id=101, user_id=42, content="solve"))
    # Clue B's message gets appended to A's window by _on_message, then on_clue
    # fires and should discard it.
    mgr.on_message(WindowMessage(message_id=200, user_id=7, content="**CCCC:** next clue (4)"))
    mgr.on_clue(200, 7)

    closed_window, _ = closed[0]
    assert len(closed_window) == 1  # only msg 101, not the closing clue 200
    assert 200 not in [m.message_id for m in closed_window.messages]
    assert 101 in [m.message_id for m in closed_window.messages]


def test_no_messages_before_first_clue():
    mgr = WindowManager()
    mgr.on_message(WindowMessage(message_id=1, user_id=42, content="early"))
    assert mgr.open_window is None  # no window open yet


def test_reset_drops_open_window():
    mgr = WindowManager()
    mgr.on_clue(100, 1)
    mgr.on_message(WindowMessage(message_id=101, user_id=42, content="x"))
    mgr.reset()
    assert mgr.open_window is None
