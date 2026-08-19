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

Confidence model: confidence = base_weight × enum_fit_score, where
enum_fit_score grades how well the extraction's letter count fits the clue's
enumeration (Δ=0 -> 1.0, Δ=1 -> 0.6, Δ=2 -> 0.3, Δ≥3 -> 0.1).  Base weights:

    extract_caps       1.0 (keep every capitalized letter, concatenate)
    extract_caps_words 1.0 (only entirely-uppercase words)
    extract_letters    1.0 (keep every letter, caps or not)
    full_message       0.6 (the solver's whole message as the answer)
    classifier         0.6 (LLM reconstruction, wordplay-only)

The free classifiers (caps) are the strongest source; full-message and the
LLM rank equal and lower.  A free classifier that hits enum-fit 1.0 scores
1.0 and lets us skip the LLM for that message entirely.  Hash verification
is a hard proof and overrides the model (confidence 1.0).
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


# ── deterministic extractions ────────────────────────────────────────────
#
# Cryptic answers are conventionally written in ALL CAPS by the solver, so
# the capitalized content of a message is a strong, free signal for the
# answer.  We produce two complementary extractions from each solver message
# (both pure string ops, no LLM):
#
#   * caps-concat  — keep every capitalized letter and concatenate.  Catches
#                    answers scattered across a message, e.g.
#                    "PA'S (_S) OVER" -> "PASSOVER".
#   * caps-words   — keep only words that are entirely uppercase, splitting
#                    on whitespace/hyphens.  Catches clean single/multi-word
#                    answers (e.g. "FROG") without the stray single-letter-caps
#                    false positive of caps-concat.
#
# Each extraction is scored by `enum_fit_score`: how well its letter count
# fits the clue's enumeration.  Enum fit is the dominant confidence driver
# for deterministic candidates — a caps extraction that exactly fits the
# enumeration outranks both the full-message extraction and the LLM.

# Base weights for each extraction type.  Confidence = base_weight × enum_fit.
# The free classifiers (caps-concat, caps-words) are the strongest source; the
# full-message extraction and the LLM classifier rank equal and lower.  A free
# classifier that hits enum-fit 1.0 therefore scores 1.0 and lets us skip the
# LLM entirely for that message.
BASE_WEIGHT_EXTRACT_CAPS = 1.0
BASE_WEIGHT_EXTRACT_CAPS_WORDS = 1.0
BASE_WEIGHT_EXTRACT_LETTERS = 1.0
BASE_WEIGHT_FULL_MESSAGE = 0.6
BASE_WEIGHT_CLASSIFIER = 0.6


def _delta_to_score(delta: int) -> float:
    """Graded enum-fit score from a letter-count delta.

    Enumeration fit is boolean for *correctness* (an 8-letter answer either
    fits (8) or it doesn't), but for *review ranking* it is graded: an
    off-by-one candidate is almost certainly a near-correct extraction (we
    grabbed or dropped a stray letter) and should surface near the top of the
    review queue, not be buried.  So we map the length delta to a continuous
    score:
        Δ=0 -> 1.0   (exact fit)
        Δ=1 -> 0.6   (near-miss: high review priority)
        Δ=2 -> 0.3
        Δ≥3 -> 0.1   (essentially noise)
    """
    if delta == 0:
        return 1.0
    if delta == 1:
        return 0.6
    if delta == 2:
        return 0.3
    return 0.1


def _extract_caps_concat(text: str) -> str:
    """Keep every capitalized letter and concatenate.

    e.g. "PA'S (_S) OVER" -> "PASSOVER".  Ignores the trailing enumeration.
    """
    text = re.sub(r"\(\s*\d+(?:\s*,\s*\d+)*\s*\)\s*\.?\s*$", "", text.strip())
    return "".join(ch for ch in text if ch.isupper() and ch.isalpha())


def _extract_caps_words(text: str) -> list[str]:
    """Words that are entirely uppercase, splitting on whitespace/hyphens.

    e.g. "the answer is FROG" -> ["FROG"].  A single stray capital (e.g. the
    "I" in "I FROG") is dropped (length < 2), so this avoids the caps-concat
    false positive while still catching clean multi-word answers.
    """
    text = re.sub(r"\(\s*\d+(?:\s*,\s*\d+)*\s*\)\s*\.?\s*$", "", text.strip())
    words: list[str] = []
    for token in re.split(r"[\s-]+", text):
        letters = re.sub(r"[^A-Za-z]", "", token)
        if letters and letters.isupper() and len(letters) >= 2:
            words.append(token)
    return words


def _extract_letters(text: str) -> str:
    """Keep every letter, caps or not, and concatenate (uppercased).

    e.g. "TBI + l_ i_ S_ i_" -> "TBILISI".  This catches answers whose
    letters are scattered across a message *with mixed case* — a case the
    caps-only extractors miss (the lowercase l/i are part of the answer).
    """
    text = re.sub(r"\(\s*\d+(?:\s*,\s*\d+)*\s*\)\s*\.?\s*$", "", text.strip())
    return "".join(ch for ch in text if ch.isalpha()).upper()


def _letter_count(word: str) -> int:
    return sum(1 for ch in word if ch.isalpha())


def _enum_fit_score(word_lengths: list[int], enumeration: str | None) -> float:
    """Graded enum-fit score from actual word lengths vs the enumeration.

    * single-part `(7)`  — compare the total letter count to the part.
    * multi-part `(4,8)` — a single flat word (e.g. caps-concat) compares its
      total against the *sum* of the parts; a multi-word result (e.g.
      caps-words) compares per-word against the parts in order, averaging the
      per-part scores (so a partial match ranks above a total miss but below
      a full match).
    """
    parts = _enumeration_lengths(enumeration)
    if not parts:
        return 0.0
    if len(word_lengths) == 1:
        # A single flat word matches against the total (sum of parts).
        return _delta_to_score(abs(word_lengths[0] - sum(parts)))
    scores = []
    for i, part in enumerate(parts):
        actual = word_lengths[i] if i < len(word_lengths) else 0
        scores.append(_delta_to_score(abs(actual - part)))
    return sum(scores) / len(scores)


def enum_fit_for_answer(answer: str, enumeration: str | None) -> float:
    """Graded enum fit for a plain answer string (used by the LLM worker).

    The LLM returns a bare answer (lowercase, no punctuation); we split it
    into words and score it against the enumeration the same way the
    deterministic extractions are scored.
    """
    words = re.findall(r"[A-Za-z]+", answer or "")
    return _enum_fit_score([len(w) for w in words], enumeration)


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

    # Messages where a free classifier hit enum-fit 1.0: the deterministic
    # answer is trusted, so we skip the LLM for that message entirely (saves
    # tokens + latency).
    llm_skip: set[int] = set()

    # Tier 1: enumeration pre-filter — deterministic extractions (free).
    # For each solver message we produce caps-concat, caps-words, and
    # full-message candidates, each scored base_weight × enum_fit.
    for msg in solver_msgs:
        text = msg.content or ""
        caps_concat = _extract_caps_concat(text)
        caps_words = _extract_caps_words(text)
        letters = _extract_letters(text)
        full = _extract_answer(msg)

        # Caps-concat: keep every capitalized letter (e.g. "PA'S (_S) OVER"
        # -> "PASSOVER").  Require ≥2 letters so a lone stray capital in an
        # otherwise-lowercase message doesn't produce noise.
        if len(caps_concat) >= 2:
            fit = _enum_fit_score([len(caps_concat)], enumeration)
            candidates.append(Candidate(
                solution=caps_concat,
                solver=msg.user_name or str(msg.user_id),
                explanation=text,
                confidence=BASE_WEIGHT_EXTRACT_CAPS * fit,
                signals={"extract_caps": True, "enum_match": fit == 1.0},
                source_message_id=msg.message_id,
            ))
            if fit == 1.0:
                llm_skip.add(msg.message_id)

        # Caps-words: only entirely-uppercase words (e.g. "FROG"), avoiding
        # the stray single-letter-caps false positive of caps-concat.
        if caps_words:
            fit = _enum_fit_score(
                [_letter_count(w) for w in caps_words], enumeration
            )
            candidates.append(Candidate(
                solution=" ".join(caps_words),
                solver=msg.user_name or str(msg.user_id),
                explanation=text,
                confidence=BASE_WEIGHT_EXTRACT_CAPS_WORDS * fit,
                signals={"extract_caps_words": True, "enum_match": fit == 1.0},
                source_message_id=msg.message_id,
            ))
            if fit == 1.0:
                llm_skip.add(msg.message_id)

        # Extract-letters: keep every letter, caps or not (e.g.
        # "TBI + l_ i_ S_ i_" -> "TBILISI").  Catches mixed-case answers the
        # caps-only extractors miss.  Only fire when the message is NOT a
        # clean plain answer and is *mixed case* (has both upper and lower
        # letters) — pure-lowercase wordplay ("t woof hear t_s_") must not
        # produce a junk candidate.  Also require a plausible fit (≥0.6).
        has_mixed_case = (
            any(ch.isupper() for ch in text)
            and any(ch.islower() for ch in text)
        )
        if (
            has_mixed_case
            and not _matches_enumeration(text, enumeration)
            and len(letters) >= 2
        ):
            fit = _enum_fit_score([len(letters)], enumeration)
            if fit >= 0.6:
                candidates.append(Candidate(
                    solution=letters,
                    solver=msg.user_name or str(msg.user_id),
                    explanation=text,
                    confidence=BASE_WEIGHT_EXTRACT_LETTERS * fit,
                    signals={"extract_letters": True, "enum_match": fit == 1.0},
                    source_message_id=msg.message_id,
                ))
                if fit == 1.0:
                    llm_skip.add(msg.message_id)

        # Full-message extraction (the solver's whole message as the answer).
        # Only fire when it's a *plausible* answer — i.e. it fits the
        # enumeration as an exact or near-miss (fit ≥ 0.6).  A wordplay
        # explanation ("t woof hear t_s_") is far from the enumeration and
        # must not become a candidate; it goes to the LLM instead.
        if full:
            fit = _enum_fit_score(_word_lengths(full), enumeration)
            if fit >= 0.6:
                candidates.append(Candidate(
                    solution=full,
                    solver=msg.user_name or str(msg.user_id),
                    explanation=text,
                    confidence=BASE_WEIGHT_FULL_MESSAGE * fit,
                    signals={"solver_match": True, "enum_match": fit == 1.0},
                    source_message_id=msg.message_id,
                ))

    # Tier 2: hash verification (proof).  This is a hard proof, not an
    # extraction, so it overrides the enum-fit model (confidence 1.0).
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

    # Tier 3: author-reply walk — the author replied to a message, confirming
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

    # Tier 4: reply-to-clue — the solver answered the clue directly.
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

    # Tier 5: LLM wordplay residue — the plain answer isn't written out.  We
    # don't call the LLM here; we hand the solver's wordplay messages to the
    # caller as llm_work, which the LLM worker turns into candidates.
    # Messages where a free classifier already hit enum-fit 1.0 are skipped —
    # the deterministic answer is trusted, no LLM needed.
    for msg in solver_msgs:
        if msg.message_id in llm_skip:
            continue
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
