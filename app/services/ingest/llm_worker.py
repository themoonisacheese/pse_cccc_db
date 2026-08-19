"""LLM worker for the solution-ingest pipeline.

Drains the `pending_llm` queue and calls an OpenAI-compatible chat-completions
endpoint to do the one thing the deterministic pipeline can't: reconstruct a
cryptic answer from a wordplay-only message (e.g. "t woof hear t_s_" ->
"Two of hearts"), and extract the salt from a hash message.

The LLM is an *extractor*, never a solver: it is only ever given a bounded
window message where the answer already sits, never asked to solve a clue
cold.  This is what keeps a cheap model viable.

Resilience (the plan's core requirement):
  * The worker is a background asyncio task inside the daemon process — it
    never blocks ingest (ingest is fire-and-forget into pending_llm).
  * Work is DB-backed (pending_llm), so it survives restarts; an LLM outage
    never loses work.
  * On failure the row is retried with exponential backoff and a circuit
    breaker; after max retries it is marked 'failed' and surfaced for review.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
from sqlalchemy import select, update

from app.core.config import get_settings
from app.db.session import async_session
from app.models.solution import ClueCandidate, PendingLlm
from app.services.ingest.solutions import (
    BASE_WEIGHT_CLASSIFIER,
    enum_fit_for_answer,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an assistant that extracts cryptic-clue answers from a chat "
    "room transcript. You are given a cryptic clue and the chat messages that "
    "followed it, including who replied to whom. The solver's message may "
    "state the answer plainly, or it may only explain the wordplay (in which "
    "case the answer is implied). Reconstruct the answer from the wordplay "
    "and the surrounding conversation. The answer must match the given "
    "enumeration (word lengths). Reply with ONLY the answer, in lowercase, "
    "no punctuation, no explanation."
)


class LlmWorker:
    """Background worker that drains pending_llm and calls the LLM."""

    def __init__(self, poll_interval: float = 5.0):
        self.poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._consecutive_failures = 0
        self._circuit_open = False

    # ── lifecycle ──────────────────────────────────────────────
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── main loop ─────────────────────────────────────────────
    async def _run(self) -> None:
        logger.info("LLM worker started")
        while not self._stop.is_set():
            try:
                if self._circuit_open:
                    # Circuit breaker: back off before probing again.
                    await asyncio.sleep(self.poll_interval * 5)
                    self._circuit_open = False
                    continue
                processed = await self._drain_once()
                if processed == 0:
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("LLM worker loop error")
                await asyncio.sleep(self.poll_interval)
        logger.info("LLM worker stopped")

    async def _drain_once(self) -> int:
        """Process up to a handful of pending rows; returns count processed."""
        settings = get_settings()
        if not (settings.llm_base_url and settings.llm_model):
            return 0  # LLM not configured — nothing to do

        async with async_session() as db:
            result = await db.execute(
                select(PendingLlm)
                .where(PendingLlm.status == "pending")
                .order_by(PendingLlm.id)
                .limit(5)
            )
            rows = result.scalars().all()
            if not rows:
                return 0

            processed = 0
            for row in rows:
                try:
                    answer = await self._call_llm(
                        clue_text=row.payload.get("clue_text", ""),
                        content=row.payload.get("content", ""),
                        enumeration=row.payload.get("enumeration"),
                        transcript=row.payload.get("transcript"),
                    )
                except _LlmUnavailable as exc:
                    # Provider down / rate-limited: back off, keep the row
                    # pending.  Increment attempts; trip the breaker on a run
                    # of failures.
                    row.attempts += 1
                    if row.attempts >= settings.llm_max_retries:
                        row.status = "failed"
                        logger.warning(
                            "LLM task %s gave up after %d attempts: %s",
                            row.id, row.attempts, exc,
                        )
                    else:
                        await asyncio.sleep(
                            settings.llm_backoff_base ** row.attempts
                        )
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= 3:
                        self._circuit_open = True
                    processed += 1
                    continue

                # Success — write a candidate.  Confidence follows the same
                # model as the deterministic extractions: base_weight ×
                # enum_fit.  The LLM classifier base weight is 0.6 (equal to
                # full-message), and the enum fit grades how well the
                # reconstructed answer matches the clue's enumeration.
                self._consecutive_failures = 0
                if answer:
                    fit = enum_fit_for_answer(
                        answer, row.payload.get("enumeration")
                    )
                    db.add(
                        ClueCandidate(
                            clue_id=row.clue_id,
                            solution=answer,
                            solver=row.payload.get("solver"),
                            explanation=row.payload.get("content"),
                            confidence=BASE_WEIGHT_CLASSIFIER * fit,
                            signals={"classifier": True, "enum_match": fit == 1.0},
                            source_message_id=row.payload.get("source_message_id"),
                        )
                    )
                row.status = "done"
                processed += 1

            await db.commit()
            return processed

    # ── prompt rendering ───────────────────────────────────────
    def _render_prompt(
        self,
        *,
        clue_text: str,
        content: str,
        enumeration: Optional[str],
        transcript: Optional[list[dict]] = None,
    ) -> str:
        """Build the user prompt for the LLM.

        When a full window transcript is available we render the whole
        conversation (the clue plus every message that followed, with reply
        structure) so the model can read the context around the solver's
        message — e.g. the author posting the clue, the solver replying with
        wordplay, and the author confirming "correct!".  Falls back to just
        the single solver message if no transcript was captured.
        """
        lines: list[str] = []
        lines.append(f"Cryptic clue: {clue_text}")
        lines.append(f"Enumeration: {enumeration or 'unknown'}")
        lines.append("")

        if transcript:
            lines.append("Here is the chat history for the author and the solver:")
            for entry in transcript:
                author = entry.get("author") or "?"
                reply = entry.get("reply_to")
                if reply:
                    lines.append(
                        f"[{entry.get('msg')}] {author} (replying to {reply}): "
                        f"{entry.get('content')}"
                    )
                else:
                    lines.append(
                        f"[{entry.get('msg')}] {author}: {entry.get('content')}"
                    )
            lines.append("")
            lines.append(
                "The solver's message is marked by the clue author's reply or "
                "the wordplay explanation. What is the answer?"
            )
        else:
            lines.append("Solver's chat message:")
            lines.append(content)
            lines.append("")
            lines.append("What is the answer?")

        return "\n".join(lines)

    # ── LLM call ───────────────────────────────────────────────
    async def _call_llm(
        self,
        *,
        clue_text: str,
        content: str,
        enumeration: Optional[str],
        transcript: Optional[list[dict]] = None,
    ) -> str:
        settings = get_settings()
        user_prompt = self._render_prompt(
            clue_text=clue_text,
            content=content,
            enumeration=enumeration,
            transcript=transcript,
        )
        payload = {
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 60,
            # The deployed model is a reasoning model: without this it burns
            # the whole max_tokens budget on hidden reasoning tokens and
            # returns an empty content.  Disabling thinking makes it answer
            # directly (verified against the provider: 4 tokens, finish=stop).
            "thinking": {"type": "disabled"},
        }
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                return (data["choices"][0]["message"]["content"] or "").strip()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (429, 500, 502, 503, 504):
                raise _LlmUnavailable(str(exc)) from exc
            raise
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            raise _LlmUnavailable(str(exc)) from exc


class _LlmUnavailable(Exception):
    """Raised when the LLM provider is down, rate-limited, or unreachable."""


async def drain_once() -> int:
    """Convenience: run a single drain pass (used by tests / scripts)."""
    worker = LlmWorker()
    return await worker._drain_once()
