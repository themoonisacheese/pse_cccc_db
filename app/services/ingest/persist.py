"""Persistence for the solution-ingest pipeline.

Bridges the in-memory detection pipeline (`solutions.detect`) to the database:

  * looks up the Clue row by its SE chat message_id (the window's clue),
  * runs detection over the closed window,
  * persists scored ClueCandidate rows (the editor review queue), and
  * enqueues PendingLlm rows for solver messages that need LLM
    reconstruction (wordplay-only cases).

This runs on the asyncio loop inside the ingest daemon when a window closes.
It is deliberately non-blocking w.r.t. ingest: detection is fast and
deterministic, and any LLM work is deferred to the separate worker via the
pending_llm queue.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.solution import ClueCandidate, PendingLlm
from app.models.clue import Clue
from app.services.ingest.window import Window
from app.services.ingest.accept import extract_enumeration
from app.services.ingest.solutions import detect

logger = logging.getLogger(__name__)


async def process_window(
    db: AsyncSession,
    window: Window,
    *,
    enumeration: Optional[str] = None,
) -> None:
    """Run detection on a closed window and persist the results.

    Looks up the clue by the window's clue_message_id, runs the detection
    pipeline, writes any candidates to clue_candidates, and enqueues any
    LLM-required work to pending_llm.  Safe to call multiple times (dedupes
    on source_message_id + solution).

    The enumeration is taken from the clue's own stored text (which retains
    the trailing enumeration) unless explicitly overridden.
    """
    if not window.closed:
        logger.warning("process_window called on an open window; ignoring")
        return

    clue = await _clue_by_message_id(db, window.clue_message_id)
    if clue is None:
        logger.warning(
            "process_window: no clue found for message_id=%s; skipping",
            window.clue_message_id,
        )
        return

    if enumeration is None:
        enumeration = extract_enumeration(clue.clue_text or "")

    candidates, llm_work = detect(window, enumeration)

    # Persist candidates (skip ones we already have).
    existing = await _existing_sources(db, clue.id)
    for cand in candidates:
        if cand.source_message_id in existing:
            continue
        db.add(
            ClueCandidate(
                clue_id=clue.id,
                solution=cand.solution,
                solver=cand.solver,
                explanation=cand.explanation,
                confidence=cand.confidence,
                signals=cand.signals,
                source_message_id=cand.source_message_id,
            )
        )

    # Enqueue LLM work for wordplay-only messages (dedupe by source_message_id).
    # The payload carries the *full window transcript* (the clue plus every
    # message that followed it, with reply structure) so the LLM can read the
    # whole conversation, not just the single solver message.  The model has
    # a large context window, so we can afford to hand it the full span.
    transcript = _build_transcript(window, clue)
    for msg in llm_work:
        if msg.message_id in existing:
            continue
        db.add(
            PendingLlm(
                clue_id=clue.id,
                task_type="wordplay_extract",
                payload={
                    "source_message_id": msg.message_id,
                    "content": msg.content,
                    "solver": msg.user_name or msg.user_id,
                    "enumeration": enumeration,
                    "clue_text": clue.clue_text,
                    "clue_author": clue.author,
                    "transcript": transcript,
                },
            )
        )

    await db.commit()
    if candidates or llm_work:
        logger.info(
            "process_window: clue=%s -> %d candidate(s), %d llm task(s)",
            clue.id,
            len(candidates),
            len(llm_work),
        )


def _build_transcript(window: Window, clue: Clue) -> list[dict]:
    """Serialize a closed window into a structured chat transcript.

    Produces an ordered list of message records for the LLM prompt, each
    with the message number, author, reply target, and content.  The clue
    itself is included as the first entry so the model sees the full
    conversation (author posts clue -> solver replies -> author confirms),
    not just the single solver message.
    """
    transcript: list[dict] = []
    # The clue message itself (the author's post that started the window).
    transcript.append(
        {
            "msg": "#clue",
            "author": clue.author or "author",
            "reply_to": None,
            "content": clue.clue_text or "",
        }
    )
    # The window's messages, ordered by message_id.  The clue message_id is
    # excluded (it's represented above); the closing clue's own message was
    # already discarded by the WindowManager.
    for m in window.messages:
        if m.message_id == window.clue_message_id:
            continue
        transcript.append(
            {
                "msg": f"#{m.message_id}",
                "author": m.user_name or (str(m.user_id) if m.user_id else "?"),
                "reply_to": f"#{m.parent_id}" if m.parent_id else None,
                "content": m.content or "",
            }
        )
    return transcript


async def _clue_by_message_id(
    db: AsyncSession, message_id: int
) -> Optional[Clue]:
    result = await db.execute(
        select(Clue).where(Clue.message_id == message_id)
    )
    return result.scalar_one_or_none()


async def _existing_sources(db: AsyncSession, clue_id: int) -> set[int]:
    """Message IDs already represented as candidates or llm tasks for a clue."""
    result = await db.execute(
        select(ClueCandidate.source_message_id).where(
            ClueCandidate.clue_id == clue_id,
            ClueCandidate.source_message_id.is_not(None),
        )
    )
    cand_ids = {r for (r,) in result.all()}
    result = await db.execute(
        select(PendingLlm.payload["source_message_id"]).where(
            PendingLlm.clue_id == clue_id,
            PendingLlm.status == "pending",
        )
    )
    for (r,) in result.all():
        if isinstance(r, int):
            cand_ids.add(r)
    return cand_ids
