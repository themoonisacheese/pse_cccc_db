"""Backfill missing CCCC clues from the SE chat search API.

Uses the bot's authenticated session (which bypasses Cloudflare) to search
for CCCC messages in a date range, then ingests them via the clue service.
"""
import asyncio
import re
import html
import logging
from datetime import datetime, date, timezone
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.session import async_session, engine
from app.models.clue import Clue, User
from app.services.ingest.accept import decide, AcceptResult, strip_html
from app.services.clue_service import ingest_clue

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def search_cccc_messages(bot, from_date: str, to_date: str) -> list[dict]:
    """Search the SE chat for CCCC messages in a date range.
    
    Returns a list of {message_id, content, user_name, user_id, date, link} dicts.
    """
    resp = bot.session.get(
        "https://chat.stackexchange.com/search",
        params={
            "q": "CCCC",
            "room": "14524",
            "fromDate": from_date,
            "toDate": to_date,
        },
        headers={"Referer": "https://chat.stackexchange.com/rooms/14524"},
    )
    if resp.status_code != 200:
        logger.error(f"Search returned {resp.status_code}")
        return []
    
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    
    # SE chat search results have messages in specific containers
    # Each result row typically has: message content, author, timestamp, and a link
    # Let's find all message links first
    for link in soup.find_all("a", href=re.compile(r"transcript/message/\d+")):
        href = link.get("href", "")
        m = re.search(r"transcript/message/(\d+)", href)
        if not m:
            continue
        msg_id = int(m.group(1))
        # The link text is often the message content (truncated)
        content = link.get_text(strip=True)
        results.append({
            "message_id": msg_id,
            "content": html.unescape(content),
            "link": href,
        })
    
    # If we didn't find links, try parsing the search result structure differently
    if not results:
        # SE chat search results are in <div class="search-result"> or similar
        for row in soup.select("div.search-result, .message-row, tr"):
            content_div = row.find(class_=re.compile(r"content|message|body"))
            if content_div:
                text = content_div.get_text(strip=True)
                link = row.find("a", href=re.compile(r"transcript"))
                msg_id = None
                if link:
                    m = re.search(r"message/(\d+)", link.get("href", ""))
                    if m:
                        msg_id = int(m.group(1))
                if text and "CCCC" in text:
                    results.append({
                        "message_id": msg_id,
                        "content": html.unescape(text),
                        "link": link.get("href") if link else None,
                    })
    
    return results


def search_cccc_rss(bot) -> list[dict]:
    """Fetch the RSS search feed for CCCC (30 most recent)."""
    import xml.etree.ElementTree as ET
    
    resp = bot.session.get(
        "https://chat.stackexchange.com/feeds/search/CCCC",
        params={"room": "14524"},
        headers={
            "Referer": "https://chat.stackexchange.com/rooms/14524",
            "Accept": "application/atom+xml",
        },
    )
    if resp.status_code != 200:
        logger.error(f"RSS returned {resp.status_code}")
        return []
    
    root = ET.fromstring(resp.text)
    ns = "{http://www.w3.org/2005/Atom}"
    results = []
    
    for entry in root.findall(f"{ns}entry"):
        eid_el = entry.find(f"{ns}id")
        if eid_el is None or not eid_el.text:
            continue
        m = re.search(r"message-(\d+)", eid_el.text)
        if not m:
            continue
        msg_id = int(m.group(1))
        
        title_el = entry.find(f"{ns}title")
        summary_el = entry.find(f"{ns}summary")
        pub_el = entry.find(f"{ns}published")
        author_name_el = entry.find(f"{ns}author/{ns}name")
        author_uri_el = entry.find(f"{ns}author/{ns}uri")
        link_el = entry.find(f"{ns}link")
        
        # Parse date
        msg_date = None
        if pub_el is not None and pub_el.text:
            try:
                dt = datetime.fromisoformat(pub_el.text.replace("Z", "+00:00"))
                msg_date = dt.date()
            except ValueError:
                pass
        
        # Parse chat user ID
        chat_user_id = None
        if author_uri_el is not None and author_uri_el.text:
            m = re.search(r"/users/(\d+)", author_uri_el.text)
            if m:
                chat_user_id = int(m.group(1))
        
        # Content: prefer summary (full), fall back to title (truncated)
        html_content = summary_el.text if summary_el is not None else ""
        if not html_content:
            html_content = title_el.text if title_el is not None else ""
        html_content = html.unescape(html_content)
        raw_content = BeautifulSoup(html_content, "html.parser").get_text()
        
        results.append({
            "message_id": msg_id,
            "content": raw_content,
            "user_name": author_name_el.text if author_name_el is not None else None,
            "user_id": chat_user_id,
            "date": msg_date,
            "link": link_el.get("href") if link_el is not None else None,
        })
    
    return results


def is_clue(content: str) -> bool:
    """Check if a message is a CCCC clue (not discussion)."""
    decision = decide(content)
    return decision.result is AcceptResult.ACCEPT


async def backfill():
    """Main backfill: fetch CCCC messages from search + RSS, ingest the clues."""
    import sechat
    
    settings = get_settings()
    bot = sechat.Bot(useCookies=True)
    bot.login(email=settings.se_bot_email, password=settings.se_bot_password, host=settings.se_site)
    logger.info(f"Bot logged in as {bot.userID}")
    
    # Collect messages from both sources
    all_messages = {}
    
    # 1. RSS feed (30 most recent CCCC messages)
    rss_msgs = search_cccc_rss(bot)
    logger.info(f"RSS feed returned {len(rss_msgs)} messages")
    for msg in rss_msgs:
        all_messages[msg["message_id"]] = msg
    
    # 2. Search API (date range 8/15 to 8/25)
    # Try multiple date ranges to catch everything
    for from_d, to_d in [
        ("2026-08-15", "2026-08-20"),
        ("2026-08-20", "2026-08-25"),
        ("2026-08-15", "2026-08-25"),
    ]:
        search_msgs = search_cccc_messages(bot, from_d, to_d)
        logger.info(f"Search {from_d} to {to_d}: {len(search_msgs)} messages")
        for msg in search_msgs:
            if msg["message_id"] and msg["message_id"] not in all_messages:
                all_messages[msg["message_id"]] = msg
    
    logger.info(f"Total unique messages collected: {len(all_messages)}")
    
    # Filter to actual clues
    clues_to_ingest = []
    for msg_id, msg in sorted(all_messages.items()):
        content = msg["content"]
        if is_clue(content):
            clues_to_ingest.append(msg)
        else:
            logger.debug(f"NOT A CLUE msg={msg_id}: {content[:100]}")
    
    logger.info(f"Clues to ingest: {len(clues_to_ingest)}")
    
    # Get or create bot user
    async with async_session() as db:
        result = await db.execute(select(User).where(User.se_user_id == 0))
        actor = result.scalar_one_or_none()
        if actor is None:
            actor = User(se_user_id=0, display_name="CCCC Ingest Bot", is_editor=True, is_bot=True)
            db.add(actor)
            await db.commit()
            await db.refresh(actor)
    
    # Ingest each clue
    ingested = 0
    skipped = 0
    for msg in clues_to_ingest:
        msg_id = msg["message_id"]
        content = msg["content"]
        author = msg.get("user_name") or "unknown"
        
        # Strip the CCCC header to get the clue text
        decision = decide(content)
        clue_text = decision.clue_text or content
        
        async with async_session() as db:
            # Check for dup
            if msg_id is not None:
                dup = (
                    await db.execute(select(Clue).where(Clue.message_id == msg_id))
                ).scalar_one_or_none()
                if dup is not None:
                    logger.info(f"DUP msg={msg_id} already clue #{dup.legacy_number}")
                    skipped += 1
                    continue
            
            try:
                clue = await ingest_clue(
                    db,
                    actor=actor,
                    clue_text=clue_text,
                    author=author,
                    message_id=msg_id,
                    source="backfill",
                    transcript_link=f"https://chat.stackexchange.com/transcript/message/{msg_id}#{msg_id}" if msg_id else None,
                )
                logger.info(f"INGESTED clue #{clue.legacy_number} msg={msg_id} author={author}: {clue_text[:80]}")
                ingested += 1
            except Exception as e:
                logger.error(f"FAILED msg={msg_id}: {e}")
    
    logger.info(f"Done. Ingested: {ingested}, Skipped (dup): {skipped}")
    
    # Show final count
    async with async_session() as db:
        count = (await db.execute(select(Clue).order_by(Clue.legacy_number.desc()).limit(1))).scalar_one_or_none()
        total = (await db.execute(select(Clue.id))).all()
        logger.info(f"Total clues in DB: {len(total)}")
        if count:
            logger.info(f"Latest clue: #{count.legacy_number} by {count.author} on {count.clue_date}")


if __name__ == "__main__":
    asyncio.run(backfill())
