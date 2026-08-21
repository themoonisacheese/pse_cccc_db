"""Tests for the solution-ingest detection pipeline."""

import hashlib

import pytest

from app.services.ingest.window import Window, WindowMessage
from app.services.ingest.solutions import detect


def _window(clue_msg_id, clue_author_id, solver_id, messages):
    w = Window(clue_message_id=clue_msg_id, clue_author_id=clue_author_id)
    w.solver_user_id = solver_id
    for m in messages:
        w.add(m)
    w.close()
    return w


def test_enumeration_match():
    w = _window(100, 1, 42, [
        WindowMessage(message_id=101, user_id=42, user_name="solver",
                      content="Two of hearts (3, 2, 6)"),
        WindowMessage(message_id=102, user_id=99, user_name="other",
                      content="unrelated chatter"),
    ])
    cands, work = detect(w, "(3, 2, 6)")
    assert len(cands) == 1
    c = cands[0]
    assert c.solution == "Two of hearts"
    assert c.confidence == 0.6
    assert c.signals.get("solver_match")
    assert c.signals.get("enum_match")
    assert work == []


def test_hash_verification_salted():
    w = _window(200, 1, 42, [
        WindowMessage(message_id=201, user_id=42, user_name="solver",
                      content="Two of hearts"),
        WindowMessage(message_id=202, user_id=1, user_name="author",
                      content=f"md5 of the answer (prepended with CCCC): "
                              f"{hashlib.md5(b'CCCCtwoofhearts').hexdigest()}"),
    ])
    cands, _ = detect(w, "(3, 2, 6)")
    assert any(c.signals.get("hash_verified") and c.confidence == 1.0
               for c in cands)


def test_author_reply_confirmation():
    w = _window(300, 1, 42, [
        WindowMessage(message_id=301, user_id=42, user_name="solver",
                      content="Two of hearts"),
        WindowMessage(message_id=302, user_id=1, user_name="author",
                      content="yep", parent_id=301),
    ])
    cands, _ = detect(w, "(3, 2, 6)")
    assert any(c.signals.get("author_reply") and c.confidence == 0.9
               for c in cands)


def test_wordplay_routed_to_llm_work():
    w = _window(400, 1, 42, [
        WindowMessage(message_id=401, user_id=42, user_name="solver",
                      content="t woof hear t_s_"),
    ])
    cands, work = detect(w, "(3, 2, 6)")
    assert cands == []
    assert len(work) == 1
    assert work[0].message_id == 401


def test_noise_user_filtered():
    from app.services.ingest import window as window_mod
    window_mod.NOISE_USER_IDS.add(99)
    try:
        w = _window(500, 1, 42, [
            WindowMessage(message_id=501, user_id=42, user_name="solver",
                          content="Two of hearts (3, 2, 6)"),
            WindowMessage(message_id=502, user_id=99, user_name="rss-bot",
                          content="RSS feed post"),
        ])
        cands, _ = detect(w, "(3, 2, 6)")
        # Noise message must not produce a candidate.
        assert all(c.source_message_id != 502 for c in cands)
    finally:
        window_mod.NOISE_USER_IDS.discard(99)


def test_caps_concat_extraction():
    """PA'S (_S) OVER -> PASSOVER, confidence 1.0 (base 1.0 x enum-fit 1.0)."""
    w = _window(700, 1, 42, [
        WindowMessage(message_id=701, user_id=42, user_name="solver",
                      content="PA'S (_S) OVER"),
    ])
    cands, work = detect(w, "(8)")
    caps = [c for c in cands if c.signals.get("extract_caps")]
    assert len(caps) == 1
    assert caps[0].solution == "PASSOVER"
    assert caps[0].confidence == 1.0
    # Free classifier hit 1.0 -> LLM skipped.
    assert work == []


def test_caps_words_extraction():
    """Clean all-caps answer: 'I FROG' -> FROG (caps-words drops the stray 'I')."""
    w = _window(800, 1, 42, [
        WindowMessage(message_id=801, user_id=42, user_name="solver",
                      content="I FROG"),
    ])
    cands, _ = detect(w, "(4)")
    caps_words = [c for c in cands if c.signals.get("extract_caps_words")]
    assert len(caps_words) == 1
    assert caps_words[0].solution == "FROG"
    assert caps_words[0].confidence == 1.0
    # caps-concat would grab the stray 'I' -> "IFROG" (5 letters), a near-miss
    # (Δ=1) so it ranks below the clean caps-words answer.
    caps_concat = [c for c in cands if c.signals.get("extract_caps")]
    assert len(caps_concat) == 1
    assert caps_concat[0].solution == "IFROG"
    assert caps_concat[0].confidence == 0.6


def test_extract_letters_mixed_case():
    """Mixed-case scattered answer: 'TBI + l_ i_ S_ i_' -> TBILISI (7)."""
    w = _window(810, 1, 42, [
        WindowMessage(message_id=811, user_id=42, user_name="solver",
                      content="TBI + l_ i_ S_ i_"),
    ])
    cands, work = detect(w, "(7)")
    letters = [c for c in cands if c.signals.get("extract_letters")]
    assert len(letters) == 1
    assert letters[0].solution == "TBILISI"
    assert letters[0].confidence == 1.0
    # Free classifier hit 1.0 -> LLM skipped.
    assert work == []


def test_caps_concat_near_miss_ranked():
    """Off-by-one caps extraction ranks high but below 1.0 (Δ=1 -> 0.6)."""
    w = _window(900, 1, 42, [
        WindowMessage(message_id=901, user_id=42, user_name="solver",
                      content="PASSOVE"),  # 7 letters, enum is 8
    ])
    cands, _ = detect(w, "(8)")
    caps = [c for c in cands if c.signals.get("extract_caps")]
    assert len(caps) == 1
    assert caps[0].solution == "PASSOVE"
    assert caps[0].confidence == 0.6  # base 1.0 x enum-fit 0.6 (Δ=1)


def test_multi_part_enum_caps_concat():
    """Multi-part enumeration: caps-concat flat count vs sum of parts."""
    w = _window(1000, 1, 42, [
        WindowMessage(message_id=1001, user_id=42, user_name="solver",
                      content="PA'S (_S) OVER"),
    ])
    cands, _ = detect(w, "(3, 5)")
    caps = [c for c in cands if c.signals.get("extract_caps")]
    assert len(caps) == 1
    # PASSOVER is 8 letters; sum of (3,5) is 8 -> exact fit, 1.0.
    assert caps[0].solution == "PASSOVER"
    assert caps[0].confidence == 1.0


def test_llm_skip_when_free_classifier_hits():
    """A message with a perfect caps extraction is not routed to the LLM."""
    w = _window(1100, 1, 42, [
        WindowMessage(message_id=1101, user_id=42, user_name="solver",
                      content="PA'S (_S) OVER"),
    ])
    cands, work = detect(w, "(8)")
    assert any(c.signals.get("extract_caps") and c.confidence == 1.0
               for c in cands)
    assert work == []


def test_wordplay_still_routed_to_llm_when_no_caps_fit():
    """Wordplay with no caps extraction that fits is still LLM work."""
    w = _window(1200, 1, 42, [
        WindowMessage(message_id=1201, user_id=42, user_name="solver",
                      content="t woof hear t_s_"),
    ])
    cands, work = detect(w, "(3, 2, 6)")
    # No caps-concat (no uppercase letters), no caps-words, full-message far
    # from enum -> no deterministic candidate, routed to LLM.
    assert not any(c.signals.get("extract_caps") for c in cands)
    assert not any(c.signals.get("extract_caps_words") for c in cands)
    assert len(work) == 1


def test_solver_identity_invariant_fallback():
    # No solver messages -> falls back to full window, low confidence.
    w = _window(600, 1, 42, [
        WindowMessage(message_id=601, user_id=7, user_name="other",
                      content="Two of hearts (3, 2, 6)"),
    ])
    cands, _ = detect(w, "(3, 2, 6)")
    assert len(cands) == 1
    assert cands[0].solver == "other"


def test_single_part_enum_multiword_not_false_match():
    """A multi-word message must NOT get enum-match just because its first
    word matches the single-part enumeration.

    Regression: "Pseudocode = sue (legally bash) ..." (enum (10)) used to
    score 1.0 because the first word "Pseudocode" is 10 letters.  The total
    letter count is far from 10, so it must not be a match.
    """
    from app.services.ingest.solutions import _enum_fit_score
    # 10 + 3 + 5 + 4 + 4 + 4 + 4 = 34 letters total, enum (10).
    assert _enum_fit_score([10, 3, 5, 4, 4, 4, 4], "(10)") < 1.0
    # A genuine single-word 10-letter answer still matches.
    assert _enum_fit_score([10], "(10)") == 1.0
    # Multi-word that totals exactly the part also matches (e.g. caps-words
    # "TWO OF HEARTS" -> [3,2,6], enum (11) -> 3+2+6=11).
    assert _enum_fit_score([3, 2, 6], "(11)") == 1.0


def test_single_part_enum_does_not_fire_on_prose():
    """The full-message extraction must not fire on prose that merely starts
    with a word matching the enumeration."""
    w = _window(1300, 1, 42, [
        WindowMessage(message_id=1301, user_id=42, user_name="solver",
                      content="Pseudocode = sue (legally bash) John Doe code"),
    ])
    cands, _ = detect(w, "(10)")
    # No full-message / solver_match candidate: the whole message is not a
    # 10-letter answer.
    assert not any(c.signals.get("solver_match") for c in cands)

