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
    assert c.confidence == 0.7
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


def test_solver_identity_invariant_fallback():
    # No solver messages -> falls back to full window, low confidence.
    w = _window(600, 1, 42, [
        WindowMessage(message_id=601, user_id=7, user_name="other",
                      content="Two of hearts (3, 2, 6)"),
    ])
    cands, _ = detect(w, "(3, 2, 6)")
    assert len(cands) == 1
    assert cands[0].solver == "other"
