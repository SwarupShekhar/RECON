import os

import httpx


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


async def post_slack(message: str) -> None:
    """Best-effort Slack webhook post. No-ops (with a console log) if unconfigured
    or if the request fails — this must never raise into a caller's hot path."""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print(f"[notify] SLACK_WEBHOOK_URL not set, skipping alert: {message}")
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, json={"text": message})
            if resp.status_code >= 300:
                print(f"[notify] Slack webhook returned {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        print(f"[notify] Slack webhook post failed: {exc}")


async def maybe_alert_open(email, open_row, total_opens: int = 1) -> None:
    if not _truthy(os.environ.get("ALERT_ON_OPEN"), default=True):
        return

    context_bits = []
    if total_opens and total_opens > 1:
        context_bits.append(f"(opened {total_opens} times)")
    if not getattr(open_row, "verified", True):
        context_bits.append("(unverified — Apple privacy proxy)")

    message = f"\U0001f4ec {email.recipient_email} opened '{email.subject}'"
    if context_bits:
        message += " " + " ".join(context_bits)

    await post_slack(message)


async def maybe_alert_click(email, link, click_row) -> None:
    if not _truthy(os.environ.get("ALERT_ON_OPEN"), default=True):
        return

    message = (
        f"\U0001f517 {email.recipient_email} clicked a link in '{email.subject}': "
        f"{link.original_url}"
    )
    if not getattr(click_row, "verified", True):
        message += " (unverified)"

    await post_slack(message)
