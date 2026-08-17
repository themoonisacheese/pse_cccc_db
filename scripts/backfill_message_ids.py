#!/usr/bin/env python3
"""
Backfill message_id for existing clues from their transcript_link.

The ingest daemon dedupes on `clues.message_id` (partial unique index).  Clues
imported from the spreadsheet carry a transcript_link but historically no
message_id, so a bot re-ingest could duplicate them.  This script walks every
clue that has a transcript_link but no message_id, extracts the SE chat message
ID from the link, and stores it.

Transcript URL formats handled:
  - https://chat.stackexchange.com/transcript/message/{id}#{id}   → extract {id}
  - https://chat.stackexchange.com/transcript/{room}?m={id}#{id}   → extract {id}
  - https://chat.stackexchange.com/transcript/{room}/{yyyy}/{mm}/{dd}  → whole-day
    transcript, no single message ID → SKIPPED (message_id stays NULL)

Usage:
    python scripts/backfill_message_ids.py [--dry-run] [--limit N]

Exit codes: 0 = ok, 1 = error.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure app is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db.session import async_session, engine
from app.models.clue import Clue
from app.services.transcript_parser import extract_message_id


async def backfill(dry_run: bool, limit: int | None) -> None:
    """Extract and store message_id for clues that have a link but no id."""
    async with async_session() as session:
        query = select(Clue).where(
            Clue.transcript_link.isnot(None),
            Clue.message_id.is_(None),
        )
        if limit:
            query = query.limit(limit)
        result = await session.execute(query)
        clues = result.scalars().all()

    print(f"Found {len(clues):,} clues with a transcript link but no message_id.")

    updated = 0
    skipped = 0
    for clue in clues:
        link = clue.transcript_link
        mid = extract_message_id(link)
        if mid is None:
            # Whole-day transcript (or malformed) — nothing to extract.
            skipped += 1
            continue

        if dry_run:
            print(f"  [dry-run] clue #{clue.legacy_number}: message_id={mid} ({link})")
        else:
            async with async_session() as session:
                await session.execute(
                    Clue.__table__.update()
                    .where(Clue.id == clue.id)
                    .values(message_id=mid, source="ingest")
                )
                await session.commit()
        updated += 1

    print(f"Done: {updated:,} message_ids extracted, {skipped:,} skipped (whole-day/no id).")
    await engine.dispose()


async def main():
    parser = argparse.ArgumentParser(description="Backfill message_id from transcript links")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change, don write")
    parser.add_argument("--limit", type=int, help="Only process first N clues (for testing)")
    args = parser.parse_args()

    await backfill(args.dry_run, args.limit)


if __name__ == "__main__":
    asyncio.run(main())
