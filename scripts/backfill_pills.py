#!/usr/bin/env python3
"""Recompute author/solver pills for every clue in the database.

The pill is defined as "count of clues by this author/solver with
legacy_number <= this clue's legacy_number".  A previous off-by-one bug
computed it before the new row was committed, leaving every pill 1 short
(first-time authors got 0 instead of 1).  This recomputes all of them
from scratch.

Usage:
    python scripts/backfill_pills.py
"""
import asyncio

from sqlalchemy import select, func

from app.db.session import async_session
from app.models.clue import Clue


async def main() -> None:
    async with async_session() as db:
        result = await db.execute(select(Clue).order_by(Clue.legacy_number))
        clues = result.scalars().all()
        print(f"Recomputing pills for {len(clues)} clues...")
        for clue in clues:
            clue.clues_by_author_so_far = (
                await db.execute(
                    select(func.count(Clue.id)).where(
                        Clue.author == clue.author,
                        Clue.legacy_number <= clue.legacy_number,
                    )
                )
            ).scalar()
            if clue.solver:
                clue.clues_by_solver_so_far = (
                    await db.execute(
                        select(func.count(Clue.id)).where(
                            Clue.solver == clue.solver,
                            Clue.legacy_number <= clue.legacy_number,
                        )
                    )
                ).scalar()
            else:
                clue.clues_by_solver_so_far = None
        await db.commit()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
