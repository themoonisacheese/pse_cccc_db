#!/usr/bin/env python3
"""
Import the CCCC Google Sheet CSV export into the database.

Usage:
    python scripts/import_csv.py [--csv path/to/file.csv] [--limit N]

If --csv is omitted, downloads from the copyparty URL.

The CSV has 12 rows of metadata/stats at the top, then a header row
at row 12 (0-indexed), with data starting at row 13.

Column layout (from the header row):
  0:  #                          (legacy number)
  1:  Clue length
  2:  Cryptic Clue
  3:  Clues by author so far
  4:  Author
  5:  (empty — old column, ignored)
  6:  Number on date
  7:  Date                       (YYYY/MM/DD)
  8:  Link                       (transcript URL)
  9:  Answer length
  10: One-word answer length
  11: answer as entered in a grid
  12: count of the answer
  13: OverrideSolver
  14: Solver
  15: Solution
  16: Standard clue?
  17: Explanation
  18: (empty)
  19: (empty)
  20: (internal spreadsheet metric — ignored)
"""

import argparse
import asyncio
import csv
import io
import os
import sys
from datetime import date, datetime
from pathlib import Path

# Ensure app is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import async_session, engine, Base
from app.models.clue import Clue, User


def _split_sql(sql: str) -> list[str]:
    """Split SQL into individual statements, respecting $$ dollar-quoting."""
    statements = []
    current = []
    in_dollar = False
    i = 0
    while i < len(sql):
        if sql[i:i+2] == "$$":
            in_dollar = not in_dollar
            current.append("$$")
            i += 2
        elif sql[i] == ";" and not in_dollar:
            stmt = "".join(current).strip()
            if stmt:
                lines = [l for l in stmt.split("\n") if not l.strip().startswith("--")]
                clean = "\n".join(lines).strip()
                if clean:
                    statements.append(clean)
            current = []
            i += 1
        else:
            current.append(sql[i])
            i += 1
    remaining = "".join(current).strip()
    if remaining:
        statements.append(remaining)
    return statements

CSV_URL = "https://copyparty.poggers.website/sharex/Puzzling%20Stack%20Exchange%20Chat%20Cryptic%20Clue%20Chains%20-%20Sheet1.csv?k=n9t9hBBY"

# Header row is at CSV line index 12 (0-based), data starts at index 13
HEADER_ROW_INDEX = 12
DATA_START_INDEX = 13


def parse_date(val: str) -> date | None:
    """Parse YYYY/MM/DD or M/D/YYYY format dates."""
    val = val.strip()
    if not val:
        return None
    for fmt in ("%Y/%m/%d", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            continue
    return None


def parse_int(val: str) -> int | None:
    val = val.strip()
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return None


def parse_bool(val: str) -> bool | None:
    val = val.strip().lower()
    if val in ("true", "1", "yes", "x"):
        return True
    if val in ("false", "0", "no", ""):
        return False
    return None


def download_csv(url: str) -> str:
    """Download the CSV from the given URL."""
    import httpx
    print(f"Downloading CSV from {url} …")
    resp = httpx.get(url, follow_redirects=True, timeout=120)
    resp.raise_for_status()
    print(f"Downloaded {len(resp.content):,} bytes")
    return resp.text


def parse_csv_rows(csv_text: str, limit: int | None = None) -> list[dict]:
    """Parse the CSV text and return a list of clue dictionaries."""
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)

    # Find the header row — look for '#' in column 0
    header_idx = None
    for i, row in enumerate(rows):
        if len(row) > 0 and row[0].strip() == "#":
            header_idx = i
            break

    if header_idx is None:
        print("ERROR: Could not find header row (looking for '#' in column 0)")
        sys.exit(1)

    print(f"Header row found at index {header_idx}")
    headers = rows[header_idx]
    print(f"Headers: {headers[:18]}")

    clues = []
    for i, row in enumerate(rows[header_idx + 1:], start=header_idx + 1):
        # Skip empty rows
        if not any(cell.strip() for cell in row):
            continue

        # Stop if we hit a row where col 0 is not a number (e.g. footer)
        legacy_num = parse_int(row[0]) if len(row) > 0 else None
        if legacy_num is None:
            # Could be a continuation row or junk; skip
            continue

        def getcol(idx):
            return row[idx].strip() if idx < len(row) else ""

        clue_data = {
            "legacy_number": legacy_num,
            "clue_text": getcol(2),
            "clues_by_author_so_far": parse_int(getcol(3)),
            "author": getcol(4) or "Unknown",
            "number_on_date": parse_int(getcol(6)),
            "clue_date": parse_date(getcol(7)),
            "transcript_link": getcol(8),
            "one_word_answer_length": parse_int(getcol(10)),
            "override_solver": getcol(13) or None,
            "solver": getcol(14) or None,
            "solution": getcol(15).strip().upper(),
            "explanation": getcol(17) or None,
        }

        # Skip rows without a clue text or solution
        if not clue_data["clue_text"] and not clue_data["solution"]:
            continue

        clues.append(clue_data)

        if limit and len(clues) >= limit:
            break

    return clues


async def import_clues(clue_dicts: list[dict]):
    """Upsert clues into the database (idempotent — safe to run multiple times)."""
    settings = get_settings()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Apply FTS migration (split by $$ dollar-quoting for asyncpg)
        migration_path = Path(__file__).parent / "migration_001_fts.sql"
        if migration_path.exists():
            migration_sql = migration_path.read_text()
            for stmt in _split_sql(migration_sql):
                await conn.execute(text(stmt))
            print("Applied FTS migration")

        # Add unique constraint on legacy_number if it doesn't exist
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_clues_legacy_number "
            "ON clues (legacy_number) WHERE legacy_number IS NOT NULL"
        ))

    # Upsert clues in batches using ON CONFLICT
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    batch_size = 500
    total = len(clue_dicts)
    upserted = 0

    async with async_session() as session:
        for i in range(0, total, batch_size):
            batch = clue_dicts[i:i + batch_size]

            # Build a bulk upsert statement
            stmt = pg_insert(Clue).values(batch)
            # On conflict, update all fields except legacy_number
            update_cols = {
                k: getattr(stmt.excluded, k)
                for k in batch[0].keys()
                if k != "legacy_number"
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["legacy_number"],
                set_=update_cols,
            )
            await session.execute(stmt)
            await session.commit()
            upserted += len(batch)
            print(f"  Upserted {upserted:,}/{total:,} clues…")

    print(f"\nDone! Upserted {upserted:,} clues.")
    await engine.dispose()


async def main():
    parser = argparse.ArgumentParser(description="Import CCCC CSV into database")
    parser.add_argument("--csv", type=str, help="Path to local CSV file")
    parser.add_argument("--limit", type=int, help="Only import first N clues (for testing)")
    parser.add_argument(
        "--if-empty", action="store_true",
        help="Skip import if the clues table already has rows",
    )
    args = parser.parse_args()

    if args.if_empty:
        # Check if clues table already has data
        from sqlalchemy import select, func as sa_func
        async with async_session() as session:
            count = await session.scalar(select(sa_func.count(Clue.id)))
        if count and count > 0:
            print(f"Clues table already has {count:,} rows — skipping import.")
            await engine.dispose()
            return

    if args.csv:
        with open(args.csv, "r", encoding="utf-8") as f:
            csv_text = f.read()
    else:
        csv_text = download_csv(CSV_URL)

    clues = parse_csv_rows(csv_text, limit=args.limit)
    print(f"\nParsed {len(clues):,} clue records from CSV")
    if clues:
        print(f"  First: #{clues[0]['legacy_number']} — {clues[0]['clue_text'][:60]}")
        print(f"  Last:  #{clues[-1]['legacy_number']} — {clues[-1]['clue_text'][:60]}")

    await import_clues(clues)


if __name__ == "__main__":
    asyncio.run(main())
