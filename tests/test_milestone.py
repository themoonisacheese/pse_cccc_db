"""Tests for the ingest daemon's milestone announcements.

When a clue is ingested and `clues_by_author_so_far` lands on a milestone
(1st or 50th), the bot posts a congratulatory message in the room.  These
tests exercise the pure message-building helper (`_milestone_message`).
"""

from types import SimpleNamespace

from app.services.ingest.daemon import _milestone_message


def _clue(n: int, author: str = "Alice", legacy_number: int = 123):
    return SimpleNamespace(
        clues_by_author_so_far=n,
        author=author,
        legacy_number=legacy_number,
    )


def test_first_clue_announced():
    msg = _milestone_message(_clue(1), author_id=42)
    assert msg is not None
    assert msg.startswith(":42 ")
    assert "first clue" in msg
    assert "@Alice" in msg


def test_fiftieth_clue_announced():
    msg = _milestone_message(_clue(50), author_id=42)
    assert msg is not None
    assert "50th clue" in msg
    assert "@Alice" in msg


def test_non_milestone_silent():
    for n in (2, 3, 49, 51):
        assert _milestone_message(_clue(n), author_id=42) is None


def test_no_author_id_no_reply_prefix():
    msg = _milestone_message(_clue(1), author_id=None)
    assert msg is not None
    assert not msg.startswith(":")
    assert "@Alice" in msg
