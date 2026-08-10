"""Transcript link parser — extracts clue info from a chat.stackexchange.com URL.

This module is intentionally independent of the main CRUD app, so that
it can be used both by the web UI and by a future bot.

Transcript URL formats:
  1. http://chat.stackexchange.com/transcript/message/{message_id}#{message_id}
  2. http://chat.stackexchange.com/transcript/{room_id}?m={message_id}#{message_id}

The parser fetches the transcript page and extracts:
  - message ID
  - author display name
  - date
  - message content
"""

import re
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup

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
    if "m" in parse_qs(parsed.query):
        return int(parse_qs(parsed.query)["m"][0])
    return None


def extract_room_id(url: str) -> Optional[int]:
    """Extract the room ID from a transcript URL."""
    m = RE_TRANSCRIPT_ROOM.search(url)
    if m:
        return int(m.group(1))
    return None


async def parse_transcript_link(url: str, timeout: int = 30) -> TranscriptParseResult:
    """Fetch and parse a chat transcript page.

    Returns a TranscriptParseResult with the message content, author, and date.
    """
    message_id = extract_message_id(url)
    if not message_id:
        return TranscriptParseResult(
            url=url,
            success=False,
            error="Could not extract message ID from URL",
        )

    room_id = extract_room_id(url) or 14524  # default to CCCC room

    # Build the message permalink URL
    message_url = f"https://chat.stackexchange.com/transcript/message/{message_id}#{message_id}"

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "CCCC-DB/1.0 (https://github.com/themoonisacheese/pse_cccc_db)"}
        ) as client:
            resp = await client.get(message_url, follow_redirects=True, timeout=timeout)
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPStatusError as e:
        # SE Chat is behind Cloudflare and may block non-browser requests.
        # In production, use the SE API with an access token instead.
        if e.response.status_code == 403:
            return TranscriptParseResult(
                url=url,
                message_id=message_id,
                success=False,
                error=(
                    "Chat Stack Exchange returned 403 (Cloudflare protection). "
                    "Transcript parsing requires browser-like access or the SE API. "
                    "The URL and message ID were extracted successfully."
                ),
            )
        return TranscriptParseResult(
            url=url,
            message_id=message_id,
            success=False,
            error=f"HTTP error: {e}",
        )
    except httpx.HTTPError as e:
        return TranscriptParseResult(
            url=url,
            message_id=message_id,
            success=False,
            error=f"HTTP error: {e}",
        )

    soup = BeautifulSoup(html, "html.parser")

    # The transcript page shows the message and surrounding context.
    # The specific message is marked with id="message-{message_id}" or
    # the fragment anchor.
    message_div = soup.find(id=f"message-{message_id}")
    if not message_div:
        # Try the "content" div within
        message_div = soup.find("div", attrs={"id": str(message_id)})

    if not message_div:
        return TranscriptParseResult(
            url=url,
            message_id=message_id,
            success=False,
            error="Message not found on transcript page",
        )

    # Extract the message content
    content = ""
    content_div = message_div.find("div", class_="content") or message_div
    if content_div:
        # Get text, clean up whitespace
        content = content_div.get_text(separator=" ", strip=True)

    # Extract the author (username)
    author = None
    # Look for the username link near the message
    user_link = message_div.find("a", class_="username")
    if not user_link:
        # Try nearby: the message is usually in a .monologue block with .signature
        monologue = message_div.find_parent(class_="monologue")
        if monologue:
            user_link = monologue.find("a", class_="username")
            if not user_link:
                user_link = monologue.find("div", class_="signature").find("a")
    if user_link:
        author = user_link.get_text(strip=True)

    # Extract the date/time
    msg_date = None
    # Look for the timestamp in the message's timestamp div
    timestamp = message_div.find("span", class_="timestamp")
    if not timestamp:
        monologue = message_div.find_parent(class_="monologue")
        if monologue:
            timestamp = monologue.find("span", class_="timestamp")
    if timestamp:
        date_text = timestamp.get_text(strip=True)
        # SE chat dates are like "Aug 10 '16" or "3:09 PM"
        for fmt in ("%b %d '%y", "%b %d '%Y", "%Y-%m-%d"):
            try:
                msg_date = datetime.strptime(date_text, fmt).date()
                break
            except ValueError:
                continue
        # If the date didn't parse (might be a time-only), try the
        # transcript's overall date from the page
        if not msg_date:
            date_header = soup.find("div", class_="date-popup")
            if date_header:
                for fmt in ("%b %d '%y", "%b %d '%Y", "%Y-%m-%d"):
                    try:
                        msg_date = datetime.strptime(
                            date_header.get_text(strip=True), fmt
                        ).date()
                        break
                    except ValueError:
                        continue

    return TranscriptParseResult(
        url=url,
        message_id=message_id,
        author=author,
        date=msg_date,
        content=content,
        success=bool(content or author),
    )
