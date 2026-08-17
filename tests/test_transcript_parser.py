"""Tests for extracting message_id from transcript links."""

from app.services.transcript_parser import extract_message_id


def test_single_message_url():
    assert (
        extract_message_id(
            "https://chat.stackexchange.com/transcript/message/69152453#69152453"
        )
        == 69152453
    )


def test_room_with_m_param():
    assert (
        extract_message_id(
            "https://chat.stackexchange.com/transcript/14524?m=69152453#69152453"
        )
        == 69152453
    )


def test_whole_day_url_no_hash():
    # Whole-day transcript → no single message ID → None
    assert extract_message_id("https://chat.stackexchange.com/transcript/14524/2026/08/18") is None


def test_whole_day_url_with_hash():
    # Whole-day transcript even with a hash → no message ID → None
    assert (
        extract_message_id(
            "https://chat.stackexchange.com/transcript/14524/2026/08/18#69152453"
        )
        is None
    )


def test_empty_string():
    assert extract_message_id("") is None
