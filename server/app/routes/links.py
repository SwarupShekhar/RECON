import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Email, Link, LinkClick
from ..notify import maybe_alert_click, webhook_for_email
from .pixel import (
    is_apple_mpp,
    is_email_muted,
    is_thread_muted,
    is_within_send_grace,
    recipient_domain_is_internal,
)

# Security gateways / link scanners at some recipient orgs (e.g. school
# districts) fetch EVERY tracked link in a delivered message in a tight burst
# within seconds of delivery. Each fetch looks like a genuine human click, so a
# single delivered email produces a flurry of false "click" alerts. If multiple
# distinct links of the SAME email are hit inside this short window, it is a
# scanner sweep, not a person reading and clicking, so we flag those as internal.
SCANNER_BURST_SECONDS = 10
SCANNER_BURST_THRESHOLD = 2

router = APIRouter()


async def scanner_burst_click_ids(
    db: AsyncSession,
    email_id: str,
    *,
    since: datetime,
) -> list[int]:
    result = await db.execute(
        select(LinkClick.id)
        .join(Link, LinkClick.link_id == Link.id)
        .where(Link.email_id == email_id)
        .where(LinkClick.clicked_at >= since)
    )
    return [row[0] for row in result.all()]


async def is_scanner_burst(db: AsyncSession, email_id: str, *, since: datetime) -> bool:
    # Distinct-link burst in a short window is a much stronger scanner signal
    # than total row count (which can be duplicated by retries).
    result = await db.execute(
        select(func.count(func.distinct(LinkClick.link_id)))
        .join(Link, LinkClick.link_id == Link.id)
        .where(Link.email_id == email_id)
        .where(LinkClick.clicked_at >= since)
    )
    distinct_links = result.scalar() or 0
    return distinct_links >= SCANNER_BURST_THRESHOLD


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

    # Mirror the pixel-open defenses so clicks aren't falsely attributed to the
    # recipient: self-tracking mutes (thread/email), the post-send grace window,
    # and internal recipient domains.
    internal = (
        await is_thread_muted(db, email.thread_id if email else None)
        or (email is not None and await is_email_muted(db, email.id))
        or (email is not None and is_within_send_grace(email.created_at))
        or (email is not None and recipient_domain_is_internal(email))
    )

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

    # Post-insert scanner detection catches concurrent scanner fetches that may
    # arrive nearly simultaneously. If this click is part of a burst, mark the
    # whole burst internal and suppress alerts.
    if email and not click_row.internal:
        burst_since = datetime.now(timezone.utc) - timedelta(seconds=SCANNER_BURST_SECONDS)
        if await is_scanner_burst(db, email.id, since=burst_since):
            burst_ids = await scanner_burst_click_ids(db, email.id, since=burst_since)
            # Always include this row's own id so its internal flag is persisted
            # even if the burst select races to an incomplete/empty set —
            # otherwise the DB row could stay internal=False while we suppress
            # its alert below, leaving it counted as a real click.
            ids_to_flag = set(burst_ids) | {click_row.id}
            await db.execute(
                update(LinkClick)
                .where(LinkClick.id.in_(ids_to_flag))
                .values(internal=True)
            )
            await db.commit()
            click_row.internal = True

    if email and not click_row.internal:
        webhook_url = await webhook_for_email(db, email)
        if webhook_url:
            asyncio.create_task(
                maybe_alert_click(email, link, click_row, webhook_url=webhook_url)
            )

    return RedirectResponse(url=link.original_url, status_code=302)
