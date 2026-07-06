from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import EmailMute, PixelMute
from ..schemas import MuteRequest

router = APIRouter()


@router.post("/mute")
async def mute_thread(req: MuteRequest, db: AsyncSession = Depends(get_db)):
    """Called by the extension right before it's about to render the sender's
    own Sent-view copy of a tracked email, so the pixel/link hits that follow
    get flagged internal instead of counted as real opens/clicks. Same trust
    model as /track — unauthenticated, called proactively by the extension
    itself, not by a lead's client.

    Accepts thread_id (existing threads — a Sent-list row click) and/or
    email_ids (fired right after send, before Gmail has assigned a thread_id
    for a brand-new compose)."""
    new_until = datetime.now(timezone.utc) + timedelta(seconds=req.seconds)

    if req.thread_id:
        result = await db.execute(select(PixelMute).where(PixelMute.thread_id == req.thread_id))
        mute = result.scalar_one_or_none()
        if mute:
            if new_until > mute.muted_until:
                mute.muted_until = new_until
        else:
            db.add(PixelMute(thread_id=req.thread_id, muted_until=new_until))

    for email_id in req.email_ids:
        result = await db.execute(select(EmailMute).where(EmailMute.email_id == email_id))
        mute = result.scalar_one_or_none()
        if mute:
            if new_until > mute.muted_until:
                mute.muted_until = new_until
        else:
            db.add(EmailMute(email_id=email_id, muted_until=new_until))

    await db.commit()
    return {"muted": True, "thread_id": req.thread_id, "email_ids": req.email_ids, "muted_until": new_until.isoformat()}
