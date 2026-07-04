from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Email, Link
from ..schemas import LinkOut, TrackRequest, TrackResponse

router = APIRouter()


@router.post("/track", response_model=TrackResponse)
async def create_tracker(req: TrackRequest, request: Request, db: AsyncSession = Depends(get_db)):
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

    links_out: list[LinkOut] = []
    if req.links:
        link_rows = [
            Link(email_id=email.id, original_url=item.url, link_type=item.type)
            for item in req.links
        ]
        db.add_all(link_rows)
        await db.commit()
        for link_row in link_rows:
            await db.refresh(link_row)

        base_url = str(request.base_url).rstrip("/")
        for link_row in link_rows:
            links_out.append(
                LinkOut(
                    link_id=link_row.id,
                    tracked_url=f"{base_url}/l/{link_row.id}",
                )
            )

    return TrackResponse(tracker_id=email.id, links=links_out)
