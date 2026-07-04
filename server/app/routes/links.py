import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Email, Link, LinkClick
from ..notify import maybe_alert_click
from .pixel import is_apple_mpp, is_thread_muted

router = APIRouter()


@router.get("/l/{link_id}")
async def follow_link(
    link_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Link).where(Link.id == link_id))
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Link not found")

    email_result = await db.execute(select(Email).where(Email.id == link.email_id))
    email = email_result.scalar_one_or_none()

    user_agent = request.headers.get("user-agent")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else None)
    # Apple doesn't prefetch real link clicks, so this UA check is mostly a
    # safety net rather than the primary signal (unlike pixel opens).
    verified = not is_apple_mpp(user_agent)
    internal = await is_thread_muted(db, email.thread_id if email else None)

    click_row = LinkClick(
        link_id=link.id,
        user_agent=user_agent,
        ip=ip,
        verified=verified,
        internal=internal,
    )
    db.add(click_row)
    await db.commit()
    await db.refresh(click_row)

    if email and not internal:
        # Fire-and-forget so the redirect isn't delayed by the Slack call.
        asyncio.create_task(maybe_alert_click(email, link, click_row))

    return RedirectResponse(url=link.original_url, status_code=302)
