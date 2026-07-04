import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Email, Open, PixelMute
from ..notify import maybe_alert_open

router = APIRouter()

PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff"
    b"\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

APPLE_MPP_UA_FRAGMENTS = ["CloudImageProxy"]


def is_apple_mpp(user_agent: str | None) -> bool:
    if not user_agent:
        return False
    return any(frag in user_agent for frag in APPLE_MPP_UA_FRAGMENTS)


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
        internal = await is_thread_muted(db, email.thread_id)

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
                # Fire-and-forget: never let a Slack hiccup slow down or fail the pixel response.
                asyncio.create_task(maybe_alert_open(email, open_row, total_opens))
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
