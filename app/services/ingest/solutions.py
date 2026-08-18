"""Detection pipeline for the solution-ingest (Stage 2) feature.

Given a completed (A, B) window — the messages between a clue and the next
clue — this module produces candidate solutions for the clue, each with a
confidence score and the deterministic signal badges that produced it.

The pipeline is deliberately *extraction-first*: the LLM is only ever asked
to read a bounded window where the answer already sits (wordplay-only cases,
salt extraction), never to solve a clue cold.  Everything else is
deterministic and runs with zero LLM.

Solver-identity invariant: the author of the *next* clue is the solver of
this clue.  We search that user's messages first.  If no candidate is found
there, the clue is flagged for manual review (detection failed or something
unusual happened) rather than silently scanning the whole window.

Confidence model (deterministic signals dominate; LLM self-confidence is a
weak tiebreaker):
    hash_verified   ~ 1.0  (proof: md5(salt+answer) == hash)
    author_reply    ~ 0.9  (author replied to the solver's message)
    solver_match    ~ 0.7  (solver identity + enumeration match)
    reply_to_clue   ~ 0.6  (solver answered the clue directly)
    enum_match      ~ 0.4  (word-lengths match the clue enumeration)
    classifier      ~ 0.3  (LLM reconstruction, wordplay-only)
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

from app.services.ingest.window import Window, WindowMessage

logger = logging.getLogger(__name__)

# A 32-hex-character MD5 digest (optionally prefixed with "md5").
RE_MD5 = re.compile(r"\b(?:md5[:\s]*)?([0-9a-f]{32})\b", re.IGNORECASE)

# Common author-confirmation phrases ("yep", "correct", "you got it", ...).
RE_CONFIRM = re.compile(
    r"\b(?:yep|yes|correct|right|you\s+got\s+it|got\s+it|that'?s\s+it|"
    r"well\s+done|nice|good\s+job|hash\s+matches|mash\s+hatches)\b",
    re.IGNORECASE,
)


@dataclass
class Candidate:
    """A proposed solution for a clue, ready to be persisted as a row."""

    solution: str
    solver: str | None = None
    explanation: str | None = None
    confidence: float = 0.0
    signals: dict = field(default_factory=dict)
    source_message_id: int | None = None

    def __repr__(self):
        return (
            f"<Candidate {self.solution!r} conf={self.confidence:.2f} "
            f"signals={sorted(self.signals)}>"
        )


# ── helpers ────────────────────────────────────────────────────────────

def _word_lengths(text: str) -> list[int]:
    """Word lengths of a message, ignoring punctuation and the enumeration."""
    # Strip the trailing enumeration if present (it's not part of the answer).
    text = re.sub(r"\(\s*\d+(?:\s*,\s*\d+)*\s*\)\s*\.?\s*$", "", text.strip())
    words = re.findall(r"[A-Za-z']+", text)
    return [len(w) for w in words]


def _enumeration_lengths(enumeration: str | None) -> list[int] | None:
    """Parse "(4, 8)" -> [4, 8].  Returns None if no/invalid enumeration."""
    if not enumeration:
        return None
    m = re.search(r"\(\s*(\d+(?:\s*,\s*\d+)*)\s*\)", enumeration)
    if not m:
        return None
    return [int(x) for x in re.split(r"\s*,\s*", m.group(1))]


def _matches_enumeration(text: str, enumeration: str | None) -> bool:
    """True if the message's word lengths equal the clue's enumeration."""
    expected = _enumeration_lengths(enumeration)
    if not expected:
        return False
    actual = _word_lengths(text)
    return actual == expected


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace (for fuzzy compare)."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _verify_md5(candidate: str, digest: str, salts: list[str]) -> bool:
    """Check md5(salt+answer) or md5(answer+salt) against a digest.

    The answer is normalized (cryptic answers are usually given lowercase,
    punctuation stripped), but the *salt* case matters — the author computed
    the digest over the salt exactly as written (e.g. "CCCC" uppercase).  We
    try each salt in its original, lowercase, and uppercase forms, in both
    prepend/append orders.
    """
    digest = digest.lower()
    cand_forms = {_normalize(candidate), candidate.strip()}
    for salt in salts:
        salt_forms = {salt, salt.lower(), salt.upper()}
        for s in salt_forms:
            for c in cand_forms:
                for combined in (s + c, c + s):
                    if hashlib.md5(combined.encode()).hexdigest() == digest:
                        return True
    return False


def _extract_salts(msg: WindowMessage) -> list[str]:
    """Candidate salts from a hash message: the word before 'md5' or 'hash'.

    The salt is often given free-form in the hash message, e.g.
    "md5 of the answer (prepended with CCCC): <hash>".  We grab the token
    immediately before the digest marker as a cheap first guess; the LLM
    worker refines this when needed.
    """
    salts: list[str] = []
    text = msg.content or ""
    for m in RE_MD5.finditer(text):
        # Everything between the start of the sentence and the digest, minus
        # the digest itself, is a candidate salt source.  Keep it simple:
        # the word immediately preceding "md5"/"hash" marker.
        prefix = text[: m.start()]
        tokens = re.findall(r"[A-Za-z0-9]+", prefix)
        if tokens:
            salts.append(tokens[-1])
    return salts


# ── the pipeline ────────────────────────────────────────────────────────

def detect(window: Window, enumeration: str | None) -> tuple[list[Candidate], list[WindowMessage]]:
    """Run the detection pipeline over a completed window.

    Returns (candidates, llm_work):
      * candidates  — scored Candidate objects ready to persist (may be empty).
      * llm_work    — solver messages that need LLM reconstruction (wordplay-
                      only cases where the plain answer isn't written out).
                      These are NOT candidates yet; the LLM worker must turn
                      them into candidates.  Persisting candidates and
                      enqueueing llm_work is the caller's job.
    """
    candidates: list[Candidate] = []
    llm_work: list[WindowMessage] = []
    messages = window.excluding_noise()
    if not messages:
        return candidates, llm_work

    # Solver-identity invariant: the next clue's author is the solver.  We
    # search that user's messages first.  (The WindowManager sets
    # window.solver_user_id to the closing clue's author at close time; if
    # it's missing we fall back to the full window.)
    solver_id = window.solver_user_id
    solver_msgs = (
        window.by_author(solver_id) if solver_id is not None else []
    )
    if not solver_msgs:
        # Invariant failed (detection failure or unusual event) — fall back
        # to the full filtered window so we still surface *something* for
        # review, but mark it low-confidence.
        solver_msgs = messages

    # Tier 1: hash verification (proof).
    for msg in messages:
        digest = _find_digest(msg)
        if not digest:
            continue
        salts = _extract_salts(msg)
        # The answer is usually in a *different* message (the solver's).  Try
        # each candidate answer against the hash.
        for cand in _candidate_answers(messages):
            if _verify_md5(cand.solution, digest, salts):
                cand.signals["hash_verified"] = True
                cand.confidence = max(cand.confidence, 1.0)
                candidates.append(cand)

    # Tier 2: author-reply walk — the author replied to a message, confirming
    # it.  That message is the solution.
    author_id = window.clue_author_id
    if author_id is not None:
        for msg in _author_confirmations(messages, author_id):
            target = _message_by_id(messages, msg.parent_id)
            if target is None:
                continue
            cand = Candidate(
                solution=_extract_answer(target),
                solver=target.user_name or str(target.user_id),
                explanation=target.content,
                confidence=0.9,
                signals={"author_reply": True},
                source_message_id=target.message_id,
            )
            candidates.append(cand)

    # Tier 3: reply-to-clue — the solver answered the clue directly.
    for msg in window.replies_to(window.clue_message_id):
        cand = Candidate(
            solution=_extract_answer(msg),
            solver=msg.user_name or str(msg.user_id),
            explanation=msg.content,
            confidence=0.6,
            signals={"reply_to_clue": True},
            source_message_id=msg.message_id,
        )
        candidates.append(cand)

    # Tier 4: solver identity + enumeration match (deterministic, no LLM).
    for msg in solver_msgs:
        if _matches_enumeration(msg.content, enumeration):
            cand = Candidate(
                solution=_extract_answer(msg),
                solver=msg.user_name or str(msg.user_id),
                explanation=msg.content,
                confidence=0.7,
                signals={"solver_match": True, "enum_match": True},
                source_message_id=msg.message_id,
            )
            candidates.append(cand)

    # Tier 5: LLM wordplay residue — the plain answer isn't written out.  We
    # don't call the LLM here; we hand the solver's wordplay messages to the
    # caller as llm_work, which the LLM worker turns into candidates.
    for msg in solver_msgs:
        if _looks_like_wordplay(msg.content, enumeration):
            llm_work.append(msg)

    return _dedupe(candidates), llm_work


# ── internal helpers ───────────────────────────────────────────────────

def _find_digest(msg: WindowMessage) -> str | None:
    m = RE_MD5.search(msg.content or "")
    return m.group(1) if m else None


def _candidate_answers(messages: list[WindowMessage]) -> list[Candidate]:
    """Every plausible answer text in the window (for hash verification)."""
    out: list[Candidate] = []
    for m in messages:
        ans = _extract_answer(m)
        if ans:
            out.append(
                Candidate(
                    solution=ans,
                    solver=m.user_name or str(m.user_id),
                    explanation=m.content,
                    source_message_id=m.message_id,
                )
            )
    return out


def _author_confirmations(messages: list[WindowMessage], author_id: int) -> list[WindowMessage]:
    """Messages by the author that look like a confirmation of a reply."""
    return [
        m for m in messages
        if m.user_id == author_id and m.parent_id is not None and RE_CONFIRM.search(m.content or "")
    ]


def _message_by_id(messages: list[WindowMessage], message_id: int | None) -> WindowMessage | None:
    if message_id is None:
        return None
    for m in messages:
        if m.message_id == message_id:
            return m
    return None


def _extract_answer(msg: WindowMessage) -> str:
    """Best-effort extraction of the answer from a message.

    For a plain answer this is the message text with the enumeration and
    boilerplate stripped.  For wordplay-only messages this returns "" and the
    LLM worker is expected to reconstruct the answer.
    """
    text = (msg.content or "").strip()
    # Strip a trailing enumeration if present.
    text = re.sub(r"\(\s*\d+(?:\s*,\s*\d+)*\s*\)\s*\.?\s*$", "", text).strip()
    # If it's a confirmation ("yep"), it's not an answer.
    if RE_CONFIRM.fullmatch(text):
        return ""
    return text


def _looks_like_wordplay(text: str, enumeration: str | None) -> bool:
    """Heuristic: message is wordplay/explanation, not a plain answer.

    True when the message doesn't match the enumeration (so it's not the
    plain answer) but is short and clue-like.  The LLM reconstructs the
    answer from it.
    """
    if not text:
        return False
    if _matches_enumeration(text, enumeration):
        return False
    # Short messages that aren't confirmations and don't match the enum are
    # likely wordplay explanations.
    return len(text.split()) <= 25


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Keep the highest-confidence candidate per (solution, solver)."""
    best: dict[tuple[str, str | None], Candidate] = {}
    for c in candidates:
        key = (c.solution, c.solver)
        if key not in best or c.confidence > best[key].confidence:
            best[key] = c
    return list(best.values())
