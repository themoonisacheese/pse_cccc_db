"""DB-persisted watermark for the chat ingest daemon.

The daemon records the last-seen SE chat message ID in the single-row
`ingest_state` table.  On startup (or after a disconnect) it reads this
back and does a lightweight catch-up for any messages it may have missed
while offline.

Keeping it in the DB (rather than a local file) means it survives
container rebuilds and is consistent across the app and the daemon.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clue import IngestState

logger = logging.getLogger(__name__)

STATE_ID = 1


async def get_watermark(db: AsyncSession) -> int:
    """Return the last-seen message ID (watermark). 0 if never set."""
    result = await db.execute(
        select(IngestState.watermark).where(IngestState.id == STATE_ID)
    )
    value = result.scalar_one_or_none()
    return value if value is not None else 0


async def set_watermark(db: AsyncSession, message_id: int) -> None:
    """Advance the watermark to the given message ID."""
    await db.execute(
        IngestState.__table__.update()
        .where(IngestState.id == STATE_ID)
        .values(watermark=message_id)
    )
    await db.commit()
