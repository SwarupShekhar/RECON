import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Email, EmailMute, Open, PixelMute
from ..notify import maybe_alert_open, webhook_for_email

router = APIRouter()

PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff"
    b"\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

APPLE_MPP_UA_FRAGMENTS = ["CloudImageProxy"]

# Gmail (and other providers) prefetch/cache every image in a message within
# seconds of it being sent — before any human could possibly read it. That
# prefetch hits our pixel and looks exactly like a real open. The extension's
# post-send mute is meant to suppress it, but that relies on the extension
# being loaded and working. This server-side grace window is an independent
# backstop: any pixel fire this soon after the tracker was created can't be a
# genuine human open, so we record it but flag it internal (hidden from counts).
SEND_GRACE_SECONDS = 15


def is_apple_mpp(user_agent: str | None) -> bool:
    if not user_agent:
        return False
    return any(frag in user_agent for frag in APPLE_MPP_UA_FRAGMENTS)


def is_within_send_grace(created_at: datetime | None) -> bool:
    if not created_at:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at < timedelta(seconds=SEND_GRACE_SECONDS)


async def is_thread_muted(db: AsyncSession, thread_id: str | None) -> bool:
    """Self-tracking suppression check. A thread is muted for a short window
    right after the extension detects the sender is about to view/open their
    own Sent copy — see POST /mute. All recipients on one thread share a
    single compose body (and thus multiple embedded pixels), so muting is
    keyed by thread_id, not tracker_id."""
    if not thread_id:
        return False
    result = await db.execute(select(PixelMute).where(PixelMute.thread_id == thread_id))
    mute = result.scalar_one_or_none()
    return bool(mute and mute.muted_until > datetime.now(timezone.utc))


async def is_email_muted(db: AsyncSession, email_id: str) -> bool:
    """Same idea as is_thread_muted, but keyed by the tracker id itself. A
    brand-new compose has no thread_id yet (Gmail assigns one only after
    send), so thread-based muting can't cover it — this is set immediately
    after send instead, right when the extension knows the tracker id."""
    result = await db.execute(select(EmailMute).where(EmailMute.email_id == email_id))
    mute = result.scalar_one_or_none()
    return bool(mute and mute.muted_until > datetime.now(timezone.utc))


@router.get("/t/{tracker_id}/pixel.gif")
async def log_open(
    tracker_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Email).where(Email.id == tracker_id))
    email = result.scalar_one_or_none()

    if email:
        user_agent = request.headers.get("user-agent")
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else None)
        verified = not is_apple_mpp(user_agent)
        internal = (
            is_within_send_grace(email.created_at)
            or await is_thread_muted(db, email.thread_id)
            or await is_email_muted(db, tracker_id)
        )

        open_row = Open(
            email_id=tracker_id,
            user_agent=user_agent,
            ip=ip,
            verified=verified,
            internal=internal,
        )
        db.add(open_row)
        await db.commit()
        await db.refresh(open_row)

        if not internal:
            try:
                count_result = await db.execute(
                    select(func.count(Open.id)).where(Open.email_id == tracker_id, Open.internal == False)
                )
                total_opens = count_result.scalar() or 1
                webhook_url = await webhook_for_email(db, email)
                if webhook_url:
                    asyncio.create_task(
                        maybe_alert_open(email, open_row, total_opens, webhook_url=webhook_url)
                    )
            except Exception:
                pass

    return Response(
        content=PIXEL_GIF,
        media_type="image/gif",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )
