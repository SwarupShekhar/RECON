import json
import os

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import User


async def alerts_enabled_for_email(db: AsyncSession, email) -> bool:
    """Per-user opt-out only. Webhook URL is always the team channel in env."""
    user = None
    if getattr(email, "user_id", None):
        result = await db.execute(select(User).where(User.id == email.user_id))
        user = result.scalar_one_or_none()
    if not user and email.sender_email:
        result = await db.execute(select(User).where(User.email == email.sender_email))
        user = result.scalar_one_or_none()
    if user:
        return bool(user.alerts_enabled)
    return True


def team_slack_webhook() -> str | None:
    return os.environ.get("SLACK_WEBHOOK_URL", "").strip() or None


async def webhook_for_email(db: AsyncSession, email) -> str | None:
    """Team Slack channel from env. Returns None if user opted out or unconfigured."""
    if not await alerts_enabled_for_email(db, email):
        return None
    return team_slack_webhook()


def _parse_recipients(email) -> list[dict]:
    raw = getattr(email, "all_recipients", None)
    if raw:
        try:
            recips = json.loads(raw)
            if isinstance(recips, list) and recips:
                return recips
        except (ValueError, TypeError):
            pass
    return [{"email": email.recipient_email, "field": "to"}]


def _recipient_summary(email) -> str:
    recipients = _parse_recipients(email)
    if len(recipients) <= 1:
        return recipients[0].get("email") or email.recipient_email or "unknown"

    by_field: dict[str, list[str]] = {}
    for r in recipients:
        addr = (r.get("email") or "").strip()
        if addr:
            by_field.setdefault(r.get("field", "to"), []).append(addr)

    labels = {"to": "To", "cc": "Cc", "bcc": "Bcc"}
    parts = [
        f"{labels.get(field, field)}: {', '.join(addrs)}"
        for field, addrs in by_field.items()
        if addrs
    ]
    return " · ".join(parts)


def _format_open_message(email, open_row, total_opens: int) -> str:
    subject = email.subject or "(no subject)"
    recipients = _parse_recipients(email)
    sender = email.sender_email or "unknown sender"

    lines = [f"📬 *{subject}* was opened"]

    if len(recipients) <= 1:
        who = recipients[0].get("email") if recipients else email.recipient_email
        lines.append(f"Recipient: _{who}_")
    else:
        lines.append(f"One of: _{_recipient_summary(email)}_")

    lines.append(f"Sent by _{sender}_")

    if total_opens and total_opens > 1:
        lines.append(f"_{total_opens} opens total on this email_")

    if not getattr(open_row, "verified", True):
        lines.append("_Unverified — may be email client prefetch_")

    return "\n".join(lines)


def _format_click_message(email, link, click_row) -> str:
    subject = email.subject or "(no subject)"
    recipients = _parse_recipients(email)
    sender = email.sender_email or "unknown sender"

    lines = [f"🔗 Link clicked in *{subject}*"]

    if len(recipients) <= 1:
        who = recipients[0].get("email") if recipients else email.recipient_email
        lines.append(f"Recipient: _{who}_")
    else:
        lines.append(f"One of: _{_recipient_summary(email)}_")

    lines.append(f"_{link.original_url}_")
    lines.append(f"Sent by _{sender}_")

    if not getattr(click_row, "verified", True):
        lines.append("_Unverified click_")

    return "\n".join(lines)


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


async def post_slack(message: str, webhook_url: str | None = None) -> None:
    """Best-effort Slack webhook post. No-ops (with a console log) if unconfigured
    or if the request fails — this must never raise into a caller's hot path."""
    if not webhook_url:
        webhook_url = team_slack_webhook()
    if not webhook_url:
        print(f"[notify] No Slack webhook configured, skipping alert: {message}")
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, json={"text": message})
            if resp.status_code >= 300:
                print(f"[notify] Slack webhook returned {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        print(f"[notify] Slack webhook post failed: {exc}")


async def maybe_alert_open(
    email, open_row, total_opens: int = 1, webhook_url: str | None = None,
) -> None:
    if not _truthy(os.environ.get("ALERT_ON_OPEN"), default=True):
        return

    message = _format_open_message(email, open_row, total_opens)
    await post_slack(message, webhook_url=webhook_url)


async def maybe_alert_click(
    email, link, click_row, webhook_url: str | None = None,
) -> None:
    if not _truthy(os.environ.get("ALERT_ON_OPEN"), default=True):
        return

    message = _format_click_message(email, link, click_row)
    await post_slack(message, webhook_url=webhook_url)
