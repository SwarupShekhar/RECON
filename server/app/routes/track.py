from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Email
from ..schemas import TrackRequest, TrackResponse

router = APIRouter()


@router.post("/track", response_model=TrackResponse)
async def create_tracker(req: TrackRequest, db: AsyncSession = Depends(get_db)):
    email = Email(
        sender_email=req.sender_email,
        recipient_email=req.recipient_email,
        recipient_field=req.recipient_field,
        subject=req.subject,
        thread_id=req.thread_id,
    )
    db.add(email)
    await db.commit()
    await db.refresh(email)
    return TrackResponse(tracker_id=email.id)
