import asyncio
from datetime import datetime, timedelta, timezone
import json
import os

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Email, EmailMute, Link, LinkClick, Open, PixelMute
from ..notify import maybe_alert_open, webhook_for_email

router = APIRouter()

PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff"
    b"\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

APPLE_MPP_UA_FRAGMENTS = ["CloudImageProxy"]
SELF_ACTIVITY_WINDOW_SECONDS = int(os.environ.get("SELF_ACTIVITY_WINDOW_SECONDS", "90"))


def _load_internal_domains() -> set[str]:
    configured = (
        os.environ.get("INTERNAL_RECIPIENT_DOMAINS")
        or os.environ.get("INTERNAL_OPEN_DOMAINS")
        or "vaidikedu.com"
    )
    return {
        part.strip().lower().lstrip("@").lstrip(".")
        for part in configured.split(",")
        if part.strip()
    }


INTERNAL_OPEN_DOMAINS = _load_internal_domains()

# Gmail (and other providers) prefetch/cache every image in a message within
# seconds of it being sent — before any human could possibly read it. That
# prefetch hits our pixel and looks exactly like a real open. The extension's
# post-send mute is meant to suppress it, but that relies on the extension
# being loaded and working. This server-side grace window is an independent
# backstop: any pixel fire this soon after the tracker was created can't be a
# genuine human open, so we record it but flag it internal (hidden from counts).
SEND_GRACE_SECONDS = 15


def delayed_open_threshold_minutes() -> int:
    raw = os.environ.get("DELAYED_OPEN_THRESHOLD_MINUTES", "60")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 60


def is_apple_mpp(user_agent: str | None) -> bool:
    if not user_agent:
        return False
    return any(frag in user_agent for frag in APPLE_MPP_UA_FRAGMENTS)


def recipient_domain_is_internal(email: Email) -> bool:
    domains: set[str] = set()
    recipient = (email.recipient_email or "").strip().lower()
    if "@" in recipient:
        domains.add(recipient.rsplit("@", 1)[1])

    if email.all_recipients:
        try:
            recipients = json.loads(email.all_recipients)
        except (TypeError, ValueError):
            recipients = []
        for row in recipients:
            addr = (row.get("email") or "").strip().lower()
            if "@" in addr:
                domains.add(addr.rsplit("@", 1)[1])

    for domain in domains:
        if domain in INTERNAL_OPEN_DOMAINS:
            return True
        if any(domain.endswith(f".{internal}") for internal in INTERNAL_OPEN_DOMAINS):
            return True
    return False


def sender_self_recipient(email: Email) -> bool:
    sender = (email.sender_email or "").strip().lower()
    if not sender:
        return False
    recipient = (email.recipient_email or "").strip().lower()
    if recipient == sender:
        return True

    if email.all_recipients:
        try:
            recipients = json.loads(email.all_recipients)
        except (TypeError, ValueError):
            recipients = []
        for row in recipients:
            addr = (row.get("email") or "").strip().lower()
            if addr == sender:
                return True
    return False


async def has_recent_internal_sender_activity(
    db: AsyncSession,
    email_id: str,
    *,
    ip: str | None,
    user_agent: str | None,
) -> bool:
    # Aggressive-but-safe suppression: only reuse a prior internal decision when
    # both IP and UA match in a short window, which sharply lowers false matches.
    if not ip or not user_agent:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=SELF_ACTIVITY_WINDOW_SECONDS)
    normalized_ip = ip.strip()
    normalized_ua = user_agent.strip().lower()

    opens_result = await db.execute(
        select(Open.ip, Open.user_agent)
        .where(
            Open.email_id == email_id,
            Open.internal == True,
            Open.opened_at >= cutoff,
        )
        .order_by(Open.opened_at.desc())
        .limit(20)
    )
    for row_ip, row_ua in opens_result.all():
        if (row_ip or "").strip() == normalized_ip and (row_ua or "").strip().lower() == normalized_ua:
            return True

    clicks_result = await db.execute(
        select(LinkClick.ip, LinkClick.user_agent)
        .join(Link, LinkClick.link_id == Link.id)
        .where(
            Link.email_id == email_id,
            LinkClick.internal == True,
            LinkClick.clicked_at >= cutoff,
        )
        .order_by(LinkClick.clicked_at.desc())
        .limit(20)
    )
    for row_ip, row_ua in clicks_result.all():
        if (row_ip or "").strip() == normalized_ip and (row_ua or "").strip().lower() == normalized_ua:
            return True
    return False


def is_within_send_grace(created_at: datetime | None) -> bool:
    if not created_at:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at < timedelta(seconds=SEND_GRACE_SECONDS)


def minutes_since_sent(sent_at: datetime | None, opened_at: datetime | None) -> int | None:
    if not sent_at or not opened_at:
        return None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    delta = opened_at - sent_at
    if delta.total_seconds() < 0:
        return 0
    return int(delta.total_seconds() // 60)


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
            or recipient_domain_is_internal(email)
            or sender_self_recipient(email)
            or await has_recent_internal_sender_activity(
                db,
                tracker_id,
                ip=ip,
                user_agent=user_agent,
            )
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
                delayed_open_minutes = None
                if open_row.verified:
                    first_human_open_result = await db.execute(
                        select(func.count(Open.id)).where(
                            Open.email_id == tracker_id,
                            Open.internal == False,
                            Open.verified == True,
                        )
                    )
                    human_open_count = first_human_open_result.scalar() or 0
                    if human_open_count == 1:
                        elapsed_minutes = minutes_since_sent(email.created_at, open_row.opened_at)
                        threshold_minutes = delayed_open_threshold_minutes()
                        if elapsed_minutes is not None and elapsed_minutes >= threshold_minutes:
                            delayed_open_minutes = elapsed_minutes

                webhook_url = await webhook_for_email(db, email)
                if webhook_url:
                    asyncio.create_task(
                        maybe_alert_open(
                            email,
                            open_row,
                            total_opens,
                            webhook_url=webhook_url,
                            delayed_open_minutes=delayed_open_minutes,
                        )
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
