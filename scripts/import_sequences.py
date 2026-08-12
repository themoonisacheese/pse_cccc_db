#!/usr/bin/env python3
"""
Import legacy themes & author sequences from the old spreadsheet CSV export.

The old sheet stored, per clue, two grouping hints:
  - col 4: 'theme (possibly shared among setters)'   → loose theme
  - col 5: 'sequence (by one setter)'                → setter-revealed author sequence

Both are keyed by a representative clue's legacy_number (usually the first
clue in the group). We map each distinct group key to one `Sequence` row
(type 'theme' or 'author'), linked many-to-many to the clues in that group.

Usage:
    python scripts/import_sequences.py [--csv path/to/cccc_sequences.csv]

Idempotent: re-running re-syncs membership to match the CSV rather than
duplicating (row uniqueness is on (seq_type, legacy_key)).
"""

import argparse
import asyncio
import csv
import sys
from pathlib import Path
from collections import defaultdict

# Ensure app is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session, engine, Base
from app.models.clue import Clue
from app.models.sequence import Sequence, clue_sequences

CSV_URL = (
    "https://copyparty.poggers.website/sharex/Puzzling%20Stack%20Exchange%20Chat%20Cryptic%20Clue%20Chains"
    "%20-%20themes%20%26%20sequences.csv"
)


def parse_rows(rows: list[list[str]]) -> list[dict]:
    """Parse CSV physical rows into clue dicts with theme/sequence keys."""
    def gc(r, i):
        return r[i].strip() if i < len(r) and r[i] else ""

    if not rows:
        return []
    header = rows[0]
    # Locate columns by header name (robust to column reordering).
    def colidx(*names):
        for i, h in enumerate(header):
            hh = h.strip().lower()
            if any(n in hh for n in names):
                return i
        return None

    theme_col = colidx("theme")
    seq_col = colidx("sequence")
    clue_col = colidx("clue")
    num_col = colidx("#")

    if theme_col is None or seq_col is None or num_col is None:
        raise ValueError(
            f"Could not locate required columns in header: {header!r}"
        )

    clues = []
    for r in rows[1:]:
        num = gc(r, num_col)
        if not num:
            continue
        if not num.isdigit():
            continue
        clues.append({
            "legacy_number": int(num),
            "theme_key": gc(r, theme_col) or None,
            "seq_key": gc(r, seq_col) or None,
            "author": gc(r, colidx("setter")) or None,
        })
    return clues


async def main():
    parser = argparse.ArgumentParser(description="Import legacy sequences")
    parser.add_argument("--csv", type=str, help="Path to the legacy CSV export")
    parser.add_argument("--limit", type=int, help="Only process first N clues (testing)")
    args = parser.parse_args()

    if args.csv:
        with open(args.csv, "r", encoding="utf-8") as f:
            csv_text = f.read()
    else:
        import httpx
        print(f"Downloading CSV from {CSV_URL} …")
        resp = httpx.get(CSV_URL, follow_redirects=True, timeout=120)
        resp.raise_for_status()
        csv_text = resp.text

    reader = csv.reader(csv_text.splitlines())
    rows = list(reader)
    clues = parse_rows(rows)
    if args.limit:
        clues = clues[: args.limit]
    print(f"Parsed {len(clues):,} clue records")

    # Group theme keys and sequence keys -> [clue #s]
    theme_groups: dict[str, list[int]] = defaultdict(list)
    seq_groups: dict[str, list[int]] = defaultdict(list)
    # author per clue
    num_to_author = {c["legacy_number"]: c["author"] for c in clues}

    for c in clues:
        if c["theme_key"]:
            theme_groups[c["theme_key"]].append(c["legacy_number"])
        if c["seq_key"]:
            seq_groups[c["seq_key"]].append(c["legacy_number"])

    print(f"Theme groups: {len(theme_groups):,}   Author sequences: {len(seq_groups):,}")

    # Map legacy_number -> DB clue id
    async with async_session() as session:
        # Ensure sequence tables exist (in case the app has never booted to
        # apply migration_006). create_all is idempotent.
        import app.models.sequence  # noqa: F401  (register tables)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        result = await session.execute(select(Clue.id, Clue.legacy_number))
        id_by_num = {ln: cid for cid, ln in result.all() if ln is not None}
        print(f"Found {len(id_by_num):,} clues in DB to link")

        # Resync only the legacy-imported sequences (those carrying a
        # legacy_key), leaving any user-created sequences (legacy_key IS NULL)
        # untouched so re-running the import never destroys manual work.
        await session.execute(
            sa_delete(clue_sequences).where(
                clue_sequences.c.sequence_id.in_(
                    select(Sequence.id).where(Sequence.legacy_key.isnot(None))
                )
            )
        )
        await session.execute(
            sa_delete(Sequence).where(Sequence.legacy_key.isnot(None))
        )
        await session.commit()

        # ── Import themes ──────────────────────────────────────
        for key, members in theme_groups.items():
            if not key.isdigit():
                continue
            legacy_key = int(key)
            seq = Sequence(seq_type="theme", legacy_key=legacy_key)
            session.add(seq)
            await session.flush()
            await _link(session, seq.id, members, id_by_num)

        # ── Import author sequences ────────────────────────────
        for key, members in seq_groups.items():
            if not key.isdigit():
                continue
            legacy_key = int(key)
            # derive author = common author of members
            authors = {num_to_author.get(n) for n in members}
            authors.discard(None)
            author = next(iter(authors), None) if len(authors) == 1 else None
            seq = Sequence(seq_type="author", legacy_key=legacy_key, author=author)
            session.add(seq)
            await session.flush()
            await _link(session, seq.id, members, id_by_num)

        await session.commit()

    created = len(theme_groups) + len(seq_groups)
    print(f"\nDone! Imported/generated {created:,} sequences.")
    await engine.dispose()


async def _link(session: AsyncSession, sequence_id: int, member_nums: list[int], id_by_num: dict):
    """Link the given clue numbers to a sequence, skipping unknown clue numbers."""
    for num in member_nums:
        cid = id_by_num.get(num)
        if cid is None:
            continue
        await session.execute(
            clue_sequences.insert().values(sequence_id=sequence_id, clue_id=cid)
        )


if __name__ == "__main__":
    asyncio.run(main())
