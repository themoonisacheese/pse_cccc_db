"""Transcript link parser — extracts clue info from a chat.stackexchange.com URL.

This module is intentionally independent of the main CRUD app, so that
it can be used both by the web UI and by a future bot.

Transcript URL formats:
  1. http://chat.stackexchange.com/transcript/message/{message_id}#{message_id}
  2. http://chat.stackexchange.com/transcript/{room_id}?m={message_id}#{message_id}

The parser uses the authenticated SE Chat client (via the bot account)
to fetch message content via the events API, which returns structured
JSON instead of requiring HTML scraping of Cloudflare-protected pages.
"""

import html
import re
from datetime import date
from typing import Optional
from urllib.parse import urlparse, parse_qs

from app.schemas.clue import TranscriptParseResult


# Regex patterns for transcript URLs
RE_MESSAGE_URL = re.compile(
    r"https?://chat\.stackexchange\.com/transcript/message/(\d+)"
)
RE_TRANSCRIPT_ROOM = re.compile(
    r"https?://chat\.stackexchange\.com/transcript/(\d+)"
)


def extract_message_id(url: str) -> Optional[int]:
    """Extract the message ID from a transcript URL."""
    m = RE_MESSAGE_URL.search(url)
    if m:
        return int(m.group(1))
    # Try the /transcript/{room}?m={id} format
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "m" in qs:
        try:
            return int(qs["m"][0])
        except ValueError:
            pass
    return None


def extract_room_id(url: str) -> Optional[int]:
    """Extract the room ID from a transcript URL."""
    m = RE_TRANSCRIPT_ROOM.search(url)
    if m:
        return int(m.group(1))
    return None


async def parse_transcript_link(url: str) -> TranscriptParseResult:
    """Fetch and parse a chat transcript link.

    Uses the RSS search feed (/feeds/search/CCCC?room=14524) as the
    primary method — it returns the 30 most recent CCCC messages in
    structured Atom XML with full ISO timestamps, author names, chat
    user IDs, and message content.

    Falls back to the events API if the message is older than the
    most recent 30 CCCC messages.

    If the chat API is unavailable (bot credentials not configured),
    still returns the message_id and url for form pre-fill.
    """
    message_id = extract_message_id(url)
    if not message_id:
        return TranscriptParseResult(
            url=url,
            success=False,
            error="Could not extract message ID from URL",
        )

    room_id = extract_room_id(url) or 14524  # default to CCCC room

    # Try the chat API (RSS + events fallback)
    try:
        from app.services import se_chat_client
        from app.core.config import get_settings

        settings = get_settings()

        if not settings.se_bot_email:
            return TranscriptParseResult(
                url=url,
                message_id=message_id,
                success=False,
                error=(
                    "Bot credentials not configured (SE_BOT_EMAIL). "
                    "Message ID extracted successfully."
                ),
            )

        msg = await se_chat_client.get_message_by_id(message_id, room_id)
        if msg:
            content = html.unescape(msg.get("content", ""))

            # Strip the CCCC header from the content
            # Common formats: "**CCCC**: ...", "**CCCC:** ...", "CCCC: ..."
            content = re.sub(
                r'^\*{0,2}CCCC\*{0,2}\s*:?\s*', '', content
            ).strip()

            return TranscriptParseResult(
                url=url,
                message_id=message_id,
                author=msg.get("user_name"),
                date=msg.get("date"),
                content=content,
                success=True,
            )
        else:
            return TranscriptParseResult(
                url=url,
                message_id=message_id,
                success=False,
                error=(
                    "Message not found. It may be too old or may not exist. "
                    "The RSS feed only returns the 30 most recent CCCC messages; "
                    "the events API fallback also did not find it."
                ),
            )

    except Exception as e:
        return TranscriptParseResult(
            url=url,
            message_id=message_id,
            success=False,
            error=f"Chat API error: {e}",
        )
