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
from collections import defaultdict
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

    # message_ids already present in the DB (assigned by an earlier partial run,
    # or by the live daemon) must not be re-assigned — the partial unique index
    # forbids collisions with them too.
    async with async_session() as session:
        existing_ids = set(
            (
                await session.execute(
                    select(Clue.message_id).where(Clue.message_id.isnot(None))
                )
            ).scalars()
        )

    # Group candidates by their extractable message_id so we can detect
    # collisions up front.  The partial unique index (ux_clues_message_id)
    # forbids two clues sharing a message_id, so any duplicated id must be
    # skipped (left NULL) rather than force-assigned.
    by_id: dict[int, list[Clue]] = defaultdict(list)
    no_id = 0
    for clue in clues:
        mid = extract_message_id(clue.transcript_link)
        if mid is None:
            no_id += 1  # whole-day transcript (or malformed)
        else:
            by_id[mid].append(clue)

    duplicate_ids = {
        mid
        for mid, cs in by_id.items()
        if len(cs) > 1 or mid in existing_ids
    }
    for mid in sorted(duplicate_ids):
        clue_nums = ", ".join(f"#{c.legacy_number}" for c in by_id[mid])
        reason = "already in DB" if mid in existing_ids else "shared by multiple clues"
        print(f"  [dup] message_id={mid} {reason} ({clue_nums}) — skipped (left NULL)")

    updated = 0
    skipped = no_id + sum(len(by_id[mid]) for mid in duplicate_ids)
    for mid, cs in by_id.items():
        if mid in duplicate_ids:
            continue
        for clue in cs:
            if dry_run:
                print(f"  [dry-run] clue #{clue.legacy_number}: message_id={mid} ({clue.transcript_link})")
            else:
                async with async_session() as session:
                    await session.execute(
                        Clue.__table__.update()
                        .where(Clue.id == clue.id)
                        .values(message_id=mid, source="ingest")
                    )
                    await session.commit()
            updated += 1

    print(f"Done: {updated:,} message_ids extracted, {skipped:,} skipped (whole-day/no id or duplicate).")
    await engine.dispose()


async def main():
    parser = argparse.ArgumentParser(description="Backfill message_id from transcript links")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change, don write")
    parser.add_argument("--limit", type=int, help="Only process first N clues (for testing)")
    args = parser.parse_args()

    await backfill(args.dry_run, args.limit)


if __name__ == "__main__":
    asyncio.run(main())
