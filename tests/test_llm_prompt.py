"""Tests for the LLM prompt rendering and window-transcript serialization."""

from app.services.ingest.llm_worker import LlmWorker
from app.services.ingest.persist import _build_transcript
from app.services.ingest.window import Window, WindowMessage


class _FakeClue:
    author = "author"
    clue_text = "Two of hearts (3, 2, 6)"


def test_render_prompt_with_transcript():
    w = LlmWorker()
    prompt = w._render_prompt(
        clue_text="Two of hearts (3, 2, 6)",
        content="t woof hear t_s_",
        enumeration="(3, 2, 6)",
        transcript=[
            {"msg": "#clue", "author": "author", "reply_to": None,
             "content": "Two of hearts (3, 2, 6)"},
            {"msg": "#401", "author": "solver", "reply_to": "#clue",
             "content": "t woof hear t_s_"},
            {"msg": "#402", "author": "author", "reply_to": "#401",
             "content": "correct!"},
        ],
    )
    assert "Here is the chat history for the author and the solver:" in prompt
    assert "[#clue] author: Two of hearts (3, 2, 6)" in prompt
    assert "[#401] solver (replying to #clue): t woof hear t_s_" in prompt
    assert "[#402] author (replying to #401): correct!" in prompt


def test_render_prompt_fallback_without_transcript():
    w = LlmWorker()
    prompt = w._render_prompt(
        clue_text="X", content="solver msg", enumeration="(1)", transcript=None
    )
    assert "Solver's chat message:" in prompt
    assert "solver msg" in prompt
    assert "Here is the chat history" not in prompt


def test_build_transcript_includes_clue_and_replies():
    win = Window(clue_message_id=400, clue_author_id=1)
    win.solver_user_id = 42
    win.add(WindowMessage(message_id=401, user_id=42, user_name="solver",
                         content="t woof hear t_s_", parent_id=400))
    win.add(WindowMessage(message_id=402, user_id=1, user_name="author",
                         content="correct!", parent_id=401))
    win.close()

    t = _build_transcript(win, _FakeClue())
    assert t[0] == {"msg": "#clue", "author": "author", "reply_to": None,
                     "content": "Two of hearts (3, 2, 6)"}
    # Two window messages, ordered.
    assert len(t) == 3
    assert t[1]["msg"] == "#401"
    assert t[1]["reply_to"] == "#400"
    assert t[2]["msg"] == "#402"
    assert t[2]["reply_to"] == "#401"
