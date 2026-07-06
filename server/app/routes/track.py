import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Email, Link
from ..schemas import LinkOut, TrackRequest, TrackResponse

router = APIRouter()


async def _track_response_for_email(email: Email, request: Request, db: AsyncSession) -> TrackResponse:
    links_result = await db.execute(select(Link).where(Link.email_id == email.id))
    link_rows = links_result.scalars().all()
    base_url = str(request.base_url).rstrip("/")
    links_out = [
        LinkOut(link_id=link_row.id, tracked_url=f"{base_url}/l/{link_row.id}")
        for link_row in link_rows
    ]
    return TrackResponse(tracker_id=email.id, links=links_out)


async def _find_recent_duplicate(
    db: AsyncSession, req: TrackRequest, since: datetime
) -> Email | None:
    query = (
        select(Email)
        .where(
            Email.sender_email == req.sender_email,
            Email.subject == req.subject,
            Email.created_at >= since,
        )
        .order_by(Email.created_at.desc())
        .limit(1)
    )
    if req.thread_id:
        query = query.where(Email.thread_id == req.thread_id)
    else:
        query = query.where(Email.recipient_email == req.recipient_email)
    result = await db.execute(query)
    return result.scalar_one_or_none()


@router.post("/track", response_model=TrackResponse)
async def create_tracker(req: TrackRequest, request: Request, db: AsyncSession = Depends(get_db)):
    if req.id:
        existing_result = await db.execute(select(Email).where(Email.id == req.id))
        existing = existing_result.scalar_one_or_none()
        if existing:
            return await _track_response_for_email(existing, request, db)

    since = datetime.now(timezone.utc) - timedelta(seconds=120)
    duplicate = await _find_recent_duplicate(db, req, since)
    if duplicate:
        if req.all_recipients and not duplicate.all_recipients:
            duplicate.all_recipients = json.dumps([r.model_dump() for r in req.all_recipients])
            await db.commit()
            await db.refresh(duplicate)
        return await _track_response_for_email(duplicate, request, db)

    all_recipients_json = (
        json.dumps([r.model_dump() for r in req.all_recipients]) if req.all_recipients else None
    )
    email = Email(
        sender_email=req.sender_email,
        recipient_email=req.recipient_email,
        recipient_field=req.recipient_field,
        subject=req.subject,
        thread_id=req.thread_id,
        all_recipients=all_recipients_json,
        **({"id": req.id} if req.id else {}),
    )
    db.add(email)
    await db.commit()
    await db.refresh(email)

    links_out: list[LinkOut] = []
    if req.links:
        link_rows = [
            Link(
                email_id=email.id,
                original_url=item.url,
                link_type=item.type,
                **({"id": item.id} if item.id else {}),
            )
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
