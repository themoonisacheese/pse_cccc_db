"""Tests for the chat ingest accept/discard rule."""

import pytest

from app.services.ingest.accept import (
    AcceptResult,
    decide,
    extract_enumeration,
    strip_header,
    strip_html,
)


def test_accept_plain():
    d = decide("CCCC A cryptic clue about things (10)")
    assert d.result is AcceptResult.ACCEPT
    assert d.has_header
    assert d.has_enumeration
    assert d.enumeration == "(10)"


def test_accept_multi_enumeration():
    d = decide("CCCC Something stacked (4, 8)")
    assert d.result is AcceptResult.ACCEPT
    assert d.enumeration == "(4, 8)"


def test_accept_space_separated_enumeration():
    d = decide("CCCC Something stacked (4 8)")
    assert d.result is AcceptResult.ACCEPT
    assert d.enumeration == "(4 8)"


def test_extract_enumeration_space_separated():
    assert extract_enumeration("a clue here (2 3)") == "(2 3)"


def test_accept_bold_header():
    d = decide("**CCCC**: A clue with bold header (6)")
    assert d.result is AcceptResult.ACCEPT
    assert d.clue_text == "A clue with bold header (6)"


def test_accept_html_bold_header():
    # sechat delivers content as raw HTML, e.g. <b>CCCC</b>: ...
    d = decide(strip_html("<b>CCCC</b>: About chat and the French coteries (7)"))
    assert d.result is AcceptResult.ACCEPT
    assert d.has_header
    assert d.has_enumeration
    assert d.clue_text == "About chat and the French coteries (7)"


def test_strip_html():
    assert strip_html("<b>CCCC</b>: a clue <i>here</i> (4)") == "CCCC: a clue here (4)"
    assert strip_html("plain text (4)") == "plain text (4)"
    assert strip_html("") == ""


def test_accept_bold_colon_variant():
    # closing bold AFTER the colon
    d = decide("**CCCC:** A clue (5)")
    assert d.result is AcceptResult.ACCEPT
    assert d.clue_text == "A clue (5)"


def test_accept_header_with_link():
    # 'CCCC (with a link)' style — enumeration still present
    d = decide("CCCC (https://example.com) A clue (8)")
    assert d.result is AcceptResult.ACCEPT


def test_accept_case_insensitive():
    d = decide("cccc a clue (3)")
    assert d.result is AcceptResult.ACCEPT


def test_enumeration_with_trailing_period():
    d = decide("CCCC A clue (7).")
    assert d.result is AcceptResult.ACCEPT
    assert d.enumeration == "(7)."


def test_nearmiss_header_only():
    d = decide("CCCC This has no enumeration")
    assert d.result is AcceptResult.NEAR_MISS
    assert d.has_header
    assert not d.has_enumeration


def test_nearmiss_enumeration_only():
    d = decide("some random message (10)")
    assert d.result is AcceptResult.NEAR_MISS
    assert not d.has_header
    assert d.has_enumeration


def test_discard_neither():
    d = decide("just a normal chat message")
    assert d.result is AcceptResult.DISCARD


def test_discard_empty():
    d = decide("")
    assert d.result is AcceptResult.DISCARD


def test_strip_header_bold():
    assert strip_header("**CCCC**: rest (4)") == "rest (4)"


def test_extract_enumeration():
    assert extract_enumeration("a clue here (4, 8)") == "(4, 8)"
    assert extract_enumeration("no enum here") is None


def test_accept_hyphenated_enumeration():
    """Enumerations with hyphens (multi-word answers like '4-2') must be accepted."""
    d = decide("CCCC A two-word answer (4-2)")
    assert d.result is AcceptResult.ACCEPT
    assert d.has_enumeration
    assert d.enumeration == "(4-2)"


def test_accept_hyphenated_enumeration_spaces():
    d = decide("CCCC Spaced hyphen (4 - 2)")
    assert d.result is AcceptResult.ACCEPT
    assert d.enumeration == "(4 - 2)"


def test_accept_mixed_enumeration():
    """Mixed comma/hyphen enumerations like (4,2-3) should be accepted."""
    d = decide("CCCC Mixed separators (4,2-3)")
    assert d.result is AcceptResult.ACCEPT
    assert d.enumeration == "(4,2-3)"
