from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Email, Open
from ..schemas import OpenRecord, StatusResponse, ThreadStatus

router = APIRouter()


@router.get("/status", response_model=StatusResponse)
async def get_status(
    thread_ids: list[str] = Query(..., alias="thread_ids"),
    sender_email: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Email).where(Email.thread_id.in_(thread_ids))
    if sender_email:
        stmt = stmt.where(Email.sender_email == sender_email)

    result = await db.execute(stmt)
    emails = result.scalars().all()

    threads = []
    for email in emails:
        opens_result = await db.execute(
            select(Open).where(Open.email_id == email.id).order_by(Open.opened_at)
        )
        opens = opens_result.scalars().all()

        verified_count = sum(1 for o in opens if o.verified)

        threads.append(
            ThreadStatus(
                thread_id=email.thread_id or "",
                email_id=email.id,
                recipient_email=email.recipient_email,
                recipient_field=getattr(email, "recipient_field", "to"),
                subject=email.subject,
                total_opens=len(opens),
                verified_opens=verified_count,
                last_opened_at=opens[-1].opened_at if opens else None,
                opens=[OpenRecord(opened_at=o.opened_at, verified=o.verified) for o in opens],
            )
        )

    return StatusResponse(threads=threads)
