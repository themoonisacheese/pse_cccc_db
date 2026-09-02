"""Accept/discard rule for the chat ingest daemon.

A message is accepted as a valid CCCC clue iff it has BOTH:
  1. a `CCCC` header, AND
  2. a trailing enumeration in parentheses, e.g. `(10)`, `(4, 8)` or
     `(2 3)` (comma- or whitespace-separated).

Logging rule (as decided):
  - Accept (both criteria)          → ingest, no log needed.
  - Near-miss (exactly one)         → log for human review.
  - Neither (no header, no enum)    → discard silently (avoids noise).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from bs4 import BeautifulSoup

# A CCCC header, allowing the common variants:
#   "CCCC", "**CCCC**", "CCCC:", "**CCCC:**", "CCCC —", "CCCC (link)", etc.
# The header must appear at the very start of the message (after optional
# leading whitespace / markdown bold).  `\*{0,2}` appears on both sides of the
# optional colon so emphasis placed around "CCCC:", as in "**CCCC:**", is
# handled (that variant puts the closing bold after the colon).
RE_HEADER = re.compile(r"^\s*\*{0,2}CCCC\*{0,2}\s*:?\s*\*{0,2}\s*", re.IGNORECASE)

# A trailing enumeration in parentheses, e.g. (10), (4, 8), (4-2), (2 3),
# (4,2-3), (3, 4, 5).  Numbers may be separated by commas or hyphens (with
# optional surrounding whitespace) or by bare whitespace.  Allows optional
# whitespace inside and around the parens, and tolerates a trailing period
# or other punctuation after the closing paren.
RE_ENUMERATION = re.compile(
    r"\(\s*\d+(?:(?:\s*[-,]\s*|\s+)\d+)*\s*\)\s*\.?\s*$"
)


class AcceptResult(Enum):
    ACCEPT = "accept"          # header AND enumeration → ingest
    NEAR_MISS = "near_miss"    # exactly one criterion → log for review
    DISCARD = "discard"        # neither → silent discard


@dataclass
class AcceptDecision:
    result: AcceptResult
    has_header: bool
    has_enumeration: bool
    # For ACCEPT: the clue text with the header stripped, enumeration kept.
    clue_text: Optional[str] = None
    # For ACCEPT: the enumeration string as it appeared, e.g. "(10)".
    enumeration: Optional[str] = None


def strip_header(text: str) -> str:
    """Remove the leading CCCC header from a message, returning the rest."""
    return RE_HEADER.sub("", text).strip()


def strip_html(text: str) -> str:
    """Strip HTML tags from a chat message, returning plain text.

    sechat delivers message content as raw HTML (e.g. ``<b>CCCC</b>: ...``),
    not plain text.  We strip tags here so the accept rule and the window
    buffer both operate on clean text.
    """
    if not text:
        return text
    return BeautifulSoup(text, "html.parser").get_text()


def extract_enumeration(text: str) -> Optional[str]:
    """Return the trailing enumeration (including parens) if present, else None."""
    m = RE_ENUMERATION.search(text)
    return m.group(0).strip() if m else None


def decide(text: str) -> AcceptDecision:
    """Classify a raw chat message against the accept rule.

    Note: the message content from the sechat callback is raw HTML (e.g.
    ``<b>CCCC</b>: ...``).  The daemon strips HTML via `strip_html` before
    calling `decide`, so we operate on plain text here.
    """
    if not text:
        return AcceptDecision(AcceptResult.DISCARD, False, False)

    has_header = bool(RE_HEADER.match(text))
    enumeration = extract_enumeration(text)
    has_enumeration = enumeration is not None

    if has_header and has_enumeration:
        return AcceptDecision(
            AcceptResult.ACCEPT,
            has_header,
            has_enumeration,
            clue_text=strip_header(text),
            enumeration=enumeration,
        )
    if has_header or has_enumeration:
        return AcceptDecision(AcceptResult.NEAR_MISS, has_header, has_enumeration)
    return AcceptDecision(AcceptResult.DISCARD, has_header, has_enumeration)
