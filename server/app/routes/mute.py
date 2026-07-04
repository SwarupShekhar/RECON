from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import PixelMute
from ..schemas import MuteRequest

router = APIRouter()


@router.post("/mute")
async def mute_thread(req: MuteRequest, db: AsyncSession = Depends(get_db)):
    """Called by the extension right before it's about to render the sender's
    own Sent-view copy of a tracked thread, so the pixel/link hits that follow
    get flagged internal instead of counted as real opens/clicks. Same trust
    model as /track — unauthenticated, called proactively by the extension
    itself, not by a lead's client."""
    new_until = datetime.now(timezone.utc) + timedelta(seconds=req.seconds)

    result = await db.execute(select(PixelMute).where(PixelMute.thread_id == req.thread_id))
    mute = result.scalar_one_or_none()
    if mute:
        if new_until > mute.muted_until:
            mute.muted_until = new_until
    else:
        mute = PixelMute(thread_id=req.thread_id, muted_until=new_until)
        db.add(mute)

    await db.commit()
    return {"muted": True, "thread_id": req.thread_id, "muted_until": mute.muted_until.isoformat()}
