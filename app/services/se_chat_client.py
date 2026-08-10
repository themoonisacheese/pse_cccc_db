"""Authenticated Stack Exchange Chat client.

Uses the `sechat` library for authentication (email+password with built-in
cookie caching) to obtain a session that bypasses Cloudflare protection on
chat.stackexchange.com.

This provides:
  - Room owner lookup (GET /rooms/info/{roomID})
  - Message content lookup via RSS search feed (primary)
  - Message content lookup via events API (fallback)

The `sechat` library caches session cookies to disk, so repeated logins
are avoided — this prevents captcha challenges.

Since `sechat` is synchronous (uses `requests`), we wrap calls with
`asyncio.to_thread()` to integrate with our async FastAPI app.
"""

import asyncio
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, date
from typing import Optional

from bs4 import BeautifulSoup

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# ── Module-level bot instance (lazy-initialised) ────────────

_bot = None
_bot_initialised = False
_init_lock = asyncio.Lock()


def _init_bot_sync():
    """Synchronous: create a sechat.Bot, log in, and return it.

    Uses sechat's built-in cookie caching (useCookies=True) so that
    repeated logins are avoided.  If the cookies are still valid,
    no network request is made at all.
    """
    import sechat  # imported lazily so the app can start without it

    global _bot, _bot_initialised

    if _bot and _bot_initialised:
        return _bot

    settings = get_settings()

    bot = sechat.Bot(useCookies=True)
    bot.login(
        email=settings.se_bot_email,
        password=settings.se_bot_password,
        host=f"{settings.se_site}.stackexchange.com",
    )

    logger.info(f"sechat bot logged in as chat user {bot.userID}")
    _bot = bot
    _bot_initialised = True
    return bot


async def _get_bot():
    """Async: ensure the bot is initialised and return it."""
    global _bot_initialised
    if not _bot_initialised:
        async with _init_lock:
            if not _bot_initialised:
                # Run sync login in a thread to avoid blocking the event loop
                await asyncio.to_thread(_init_bot_sync)
    return _bot


async def close_session():
    """Close the chat session (call on shutdown)."""
    global _bot, _bot_initialised
    if _bot:
        # sechat registers atexit handlers for cleanup, but we can
        # leave rooms explicitly if needed.  For read-only use, the
        # session just expires naturally.
        _bot = None
        _bot_initialised = False


# ── Sync helper functions (run via asyncio.to_thread) ───────


def _get_room_owners_sync(room_id: int) -> list[int]:
    """Fetch room owner chat user IDs from the room info page."""
    bot = _bot  # called from thread; _init_bot_sync has already run
    resp = bot.session.get(
        f"https://chat.stackexchange.com/rooms/info/{room_id}",
        headers={"Referer": f"https://chat.stackexchange.com/rooms/{room_id}"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch room info: {resp.status_code}")

    # Room owners appear as <div id="owner-user-{id}" ...>
    owner_ids = re.findall(r'owner-user-(\d+)', resp.text)
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for uid_str in owner_ids:
        uid = int(uid_str)
        if uid not in seen:
            seen.add(uid)
            unique.append(uid)
    return unique


def _get_room_owner_names_sync(room_id: int) -> dict[int, str]:
    """Fetch room owners as a {chat_user_id: display_name} dict."""
    bot = _bot
    resp = bot.session.get(
        f"https://chat.stackexchange.com/rooms/info/{room_id}",
        headers={"Referer": f"https://chat.stackexchange.com/rooms/{room_id}"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch room info: {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")
    owners = {}
    for card in soup.select("[id^=owner-user-]"):
        uid_match = re.search(r'owner-user-(\d+)', card.get("id", ""))
        if uid_match:
            uid = int(uid_match.group(1))
            header = card.find(class_="user-header")
            name = header.get("title", "") if header else ""
            if not name:
                # Fallback: try the username link text
                link = card.find("a", href=re.compile(r'/users/'))
                name = link.text.strip() if link else f"user-{uid}"
            owners[uid] = name
    return owners


def _get_message_by_rss_sync(
    message_id: int, room_id: int = 14524
) -> Optional[dict]:
    """Fetch a message via the RSS search feed for 'CCCC'.

    The feed at /feeds/search/CCCC?room={room_id} returns the 30 most
    recent messages containing "CCCC" in structured Atom XML with:
      - message ID, content (raw + HTML), full ISO timestamp,
      - author name + chat user ID, transcript link.

    This is the primary lookup method because it returns full dates and
    doesn't require joining a room.  The search returns 30 messages;
    if the target message is not among them, returns None so the caller
    can fall back to the events API.
    """
    bot = _bot
    resp = bot.session.get(
        f"https://chat.stackexchange.com/feeds/search/CCCC",
        params={"room": str(room_id)},
        headers={
            "Referer": f"https://chat.stackexchange.com/rooms/{room_id}",
            "Accept": "application/atom+xml",
        },
    )
    if resp.status_code != 200:
        logger.warning(f"RSS feed returned {resp.status_code}")
        return None

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        logger.error(f"RSS XML parse error: {e}")
        return None

    ns = "{http://www.w3.org/2005/Atom}"
    target_id = f"message-{message_id}"

    for entry in root.findall(f"{ns}entry"):
        entry_id_el = entry.find(f"{ns}id")
        if entry_id_el is None or entry_id_el.text != target_id:
            continue

        # Found the matching entry
        title_el = entry.find(f"{ns}title")
        published_el = entry.find(f"{ns}published")
        author_name_el = entry.find(f"{ns}author/{ns}name")
        author_uri_el = entry.find(f"{ns}author/{ns}uri")
        summary_el = entry.find(f"{ns}summary")
        link_el = entry.find(f"{ns}link")

        # Parse the ISO timestamp
        msg_date = None
        if published_el is not None and published_el.text:
            try:
                dt = datetime.fromisoformat(
                    published_el.text.replace("Z", "+00:00")
                )
                msg_date = dt.date()
            except ValueError:
                pass

        # Extract chat user ID from author URI
        chat_user_id = None
        if author_uri_el is not None and author_uri_el.text:
            m = re.search(r"/users/(\d+)", author_uri_el.text)
            if m:
                chat_user_id = int(m.group(1))

        # Raw content from <title> (markdown), HTML from <summary>
        raw_content = title_el.text if title_el is not None else ""
        html_content = summary_el.text if summary_el is not None else ""

        # Unescape HTML entities in the raw content
        raw_content = html.unescape(raw_content)

        return {
            "message_id": message_id,
            "user_id": chat_user_id,
            "user_name": author_name_el.text if author_name_el is not None else None,
            "content": raw_content,
            "html_content": html_content,
            "date": msg_date,
            "timestamp": None,
            "room_id": room_id,
            "transcript_link": link_el.get("href") if link_el is not None else None,
        }

    # Message not in the most recent 30 CCCC messages
    return None


def _get_message_by_id_sync(message_id: int, room_id: int) -> Optional[dict]:
    """Fetch a single chat message by ID.

    Primary: RSS search feed (/feeds/search/CCCC?room={room_id}).
    Fallback: events API (/chats/{room_id}/events) if the message is
    older than the most recent 30 CCCC messages.
    """
    # Try RSS first
    msg = _get_message_by_rss_sync(message_id, room_id)
    if msg is not None:
        return msg

    logger.info(
        f"Message {message_id} not in recent RSS feed; falling back to events API"
    )

    # Fall back to events API
    bot = _bot
    fkey = bot.fkey

    resp = bot.session.post(
        f"https://chat.stackexchange.com/chats/{room_id}/events",
        data={
            "fkey": fkey,
            "mode": "Messages",
            "msgCount": 1,
            "since": message_id - 1,
        },
        headers={
            "Referer": f"https://chat.stackexchange.com/rooms/{room_id}",
        },
    )
    if resp.status_code != 200:
        logger.warning(f"Events API returned {resp.status_code}")
        return None

    data = resp.json()
    events = data.get("events", [])

    for event in events:
        if event.get("message_id") == message_id:
            ts = event.get("time_stamp", 0)
            msg_date = None
            if ts:
                msg_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            return {
                "message_id": message_id,
                "user_id": event.get("user_id"),
                "user_name": event.get("user_name"),
                "content": event.get("content", ""),
                "html_content": event.get("content", ""),
                "timestamp": ts,
                "date": msg_date,
                "room_id": event.get("room_id", room_id),
                "parent_id": event.get("parent_id"),
                "parent_text": event.get("parent_text"),
                "parent_username": event.get("parent_username"),
            }

    logger.warning(f"Message {message_id} not found in events response")
    return None


def _get_messages_around_sync(
    message_id: int, room_id: int, count: int = 5
) -> list[dict]:
    """Fetch messages around a given message ID."""
    bot = _bot
    fkey = bot.fkey

    since = max(0, message_id - count // 2)
    resp = bot.session.post(
        f"https://chat.stackexchange.com/chats/{room_id}/events",
        data={
            "fkey": fkey,
            "mode": "Messages",
            "msgCount": count,
            "since": since,
        },
        headers={
            "Referer": f"https://chat.stackexchange.com/rooms/{room_id}",
        },
    )
    if resp.status_code != 200:
        return []

    data = resp.json()
    events = data.get("events", [])
    result = []
    for event in events:
        ts = event.get("time_stamp", 0)
        msg_date = None
        if ts:
            msg_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        result.append({
            "message_id": event.get("message_id"),
            "user_id": event.get("user_id"),
            "user_name": event.get("user_name"),
            "content": event.get("content", ""),
            "timestamp": ts,
            "date": msg_date,
            "room_id": event.get("room_id", room_id),
        })
    return result


def _get_user_info_by_chat_id_sync(chat_user_id: int) -> Optional[dict]:
    """Look up a chat user's profile to find their SE site user ID.

    Chat profiles at chat.stackexchange.com/users/{id} redirect to the
    parent site profile.
    """
    bot = _bot
    resp = bot.session.get(
        f"https://chat.stackexchange.com/users/{chat_user_id}",
        allow_redirects=False,
    )

    location = resp.headers.get("location", "")
    if not location:
        resp = bot.session.get(
            f"https://chat.stackexchange.com/users/{chat_user_id}"
        )
        location = str(resp.url)

    match = re.search(r'/users/(\d+)', location)
    if match:
        site_user_id = int(match.group(1))
        host_match = re.search(r'//([^.]+)\.stackexchange\.com', location)
        site = host_match.group(1) if host_match else None
        return {
            "chat_user_id": chat_user_id,
            "site_user_id": site_user_id,
            "site": site,
            "profile_url": location,
        }
    return None


# ── Public async API (wraps sync calls) ─────────────────────


async def get_room_owners(room_id: int = 14524) -> list[int]:
    """Fetch the list of room owner user IDs for a chat room."""
    await _get_bot()
    return await asyncio.to_thread(_get_room_owners_sync, room_id)


async def get_room_owner_names(room_id: int = 14524) -> dict[int, str]:
    """Fetch room owners as a {chat_user_id: display_name} dict."""
    await _get_bot()
    return await asyncio.to_thread(_get_room_owner_names_sync, room_id)


async def get_message_by_id(
    message_id: int, room_id: int = 14524
) -> Optional[dict]:
    """Fetch a single chat message by its ID."""
    await _get_bot()
    return await asyncio.to_thread(
        _get_message_by_id_sync, message_id, room_id
    )


async def get_messages_around(
    message_id: int, room_id: int = 14524, count: int = 5
) -> list[dict]:
    """Fetch messages around a given message ID."""
    await _get_bot()
    return await asyncio.to_thread(
        _get_messages_around_sync, message_id, room_id, count
    )


async def get_user_info_by_chat_id(chat_user_id: int) -> Optional[dict]:
    """Look up a chat user's SE site user ID from their chat user ID."""
    await _get_bot()
    return await asyncio.to_thread(
        _get_user_info_by_chat_id_sync, chat_user_id
    )
