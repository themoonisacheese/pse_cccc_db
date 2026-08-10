"""API endpoints for transcript parsing."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.clue import TranscriptParseResult
from app.services.transcript_parser import parse_transcript_link

router = APIRouter(prefix="/transcript", tags=["transcript"])


@router.get("/parse", response_model=TranscriptParseResult)
async def parse_transcript(
    url: str,
    db: AsyncSession = Depends(get_db),
):
    """Parse a chat transcript link and return extracted metadata.

    This endpoint fetches the transcript page and extracts:
    - message ID
    - author
    - date
    - message content

    It can be used to pre-fill a clue entry form from a transcript link.
    """
    if "chat.stackexchange.com/transcript" not in url:
        raise HTTPException(
            status_code=400,
            detail="URL must be a chat.stackexchange.com transcript link",
        )
    result = await parse_transcript_link(url)
    return result
