"""Tests for the ingest daemon's milestone announcements.

Two kinds of milestone are announced in the room:

  * author milestones — when a clue's `clues_by_author_so_far` lands on 1 or
    50, congratulating a single author (`_milestone_message`);
  * archive milestones — when a clue's `legacy_number` lands on 10,000,
    celebrating the archive as a whole (`_global_milestone_message`).

Both build a pure string (reply prefix + body); these tests exercise those
helpers without touching chat or the DB.
"""

from types import SimpleNamespace

from app.services.ingest.daemon import (
    _global_milestone_message,
    _milestone_message,
)


def _clue(
    n: int,
    author: str = "Alice",
    legacy_number: int = 123,
    message_id: int | None = 98765,
):
    return SimpleNamespace(
        clues_by_author_so_far=n,
        author=author,
        legacy_number=legacy_number,
        message_id=message_id,
    )


def test_first_clue_announced():
    msg = _milestone_message(_clue(1))
    assert msg is not None
    assert msg.startswith(":98765 ")
    assert "first clue" in msg
    assert "@Alice" in msg


def test_fiftieth_clue_announced():
    msg = _milestone_message(_clue(50))
    assert msg is not None
    assert "50th clue" in msg
    assert "@Alice" in msg


def test_non_milestone_silent():
    for n in (2, 3, 49, 51):
        assert _milestone_message(_clue(n)) is None


def test_no_message_id_no_reply_prefix():
    msg = _milestone_message(_clue(1, message_id=None))
    assert msg is not None
    assert not msg.startswith(":")
    assert "@Alice" in msg


def test_reply_prefix_targets_message_id_not_user_id():
    # SE chat parses a reply from a leading `:<message_id>`, not the author's
    # user id, so the prefix must carry the clue's chat message id.
    msg = _milestone_message(_clue(1, message_id=555))
    assert msg.startswith(":555 ")


def test_global_10000th_announced():
    clue = _clue(1, author="Bob", legacy_number=10000, message_id=42)
    msg = _global_milestone_message(clue)
    assert msg is not None
    assert msg.startswith(":42 ")
    assert "10,000" in msg
    assert "@Bob" in msg


def test_global_non_milestone_silent():
    for n in (9999, 10001, 5000):
        assert _global_milestone_message(_clue(1, legacy_number=n)) is None


def test_global_no_message_id_no_reply_prefix():
    msg = _global_milestone_message(
        _clue(1, legacy_number=10000, message_id=None)
    )
    assert msg is not None
    assert not msg.startswith(":")
    assert "@Alice" in msg
