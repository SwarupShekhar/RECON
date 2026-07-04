from datetime import datetime
from html import escape

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Email, Link, LinkClick, Open

ACCENT_GREEN = "#0d9e3f"


async def build_report(db: AsyncSession, sender_email: str | None, since: datetime) -> dict:
    """Aggregate emails/opens/links/clicks since `since`, grouped by sender_email
    unless a single sender_email is given (in which case it's a single-row report)."""

    email_stmt = select(Email).where(Email.created_at >= since)
    if sender_email:
        email_stmt = email_stmt.where(Email.sender_email == sender_email)

    result = await db.execute(email_stmt)
    emails = result.scalars().all()

    by_sender: dict[str, dict] = {}

    def bucket(sender: str) -> dict:
        return by_sender.setdefault(
            sender,
            {
                "sender_email": sender,
                "emails_sent": 0,
                "opens": 0,
                "verified_opens": 0,
                "unique_recipients_opened": set(),
                "link_clicks": 0,
            },
        )

    email_ids_by_sender: dict[str, list[str]] = {}
    for e in emails:
        b = bucket(e.sender_email)
        b["emails_sent"] += 1
        email_ids_by_sender.setdefault(e.sender_email, []).append(e.id)

    email_id_to_sender = {e.id: e.sender_email for e in emails}
    email_id_to_recipient = {e.id: e.recipient_email for e in emails}
    all_email_ids = list(email_id_to_sender.keys())

    if all_email_ids:
        opens_result = await db.execute(
            select(Open.email_id, Open.verified).where(Open.email_id.in_(all_email_ids))
        )
        for email_id, verified in opens_result.all():
            sender = email_id_to_sender.get(email_id)
            if not sender:
                continue
            b = bucket(sender)
            b["opens"] += 1
            if verified:
                b["verified_opens"] += 1
                b["unique_recipients_opened"].add(email_id_to_recipient.get(email_id))

        links_result = await db.execute(
            select(Link.id, Link.email_id).where(Link.email_id.in_(all_email_ids))
        )
        link_rows = links_result.all()
        link_id_to_sender = {}
        for link_id, email_id in link_rows:
            sender = email_id_to_sender.get(email_id)
            if sender:
                link_id_to_sender[link_id] = sender

        if link_id_to_sender:
            clicks_result = await db.execute(
                select(LinkClick.link_id).where(LinkClick.link_id.in_(link_id_to_sender.keys()))
            )
            for (link_id,) in clicks_result.all():
                sender = link_id_to_sender.get(link_id)
                if sender:
                    bucket(sender)["link_clicks"] += 1

    senders_out = []
    for sender, data in by_sender.items():
        senders_out.append(
            {
                "sender_email": sender,
                "emails_sent": data["emails_sent"],
                "opens": data["opens"],
                "verified_opens": data["verified_opens"],
                "unique_recipients_opened": len(data["unique_recipients_opened"]),
                "link_clicks": data["link_clicks"],
            }
        )
    senders_out.sort(key=lambda r: r["sender_email"])

    return {
        "since": since,
        "sender_email": sender_email,
        "senders": senders_out,
    }


def render_report_html(report_data: dict, period_label: str) -> str:
    rows = report_data.get("senders", [])
    since = report_data.get("since")
    since_str = since.strftime("%Y-%m-%d %H:%M UTC") if isinstance(since, datetime) else str(since)

    row_html = "".join(
        f"<tr>"
        f"<td>{escape(r['sender_email'])}</td>"
        f"<td>{r['emails_sent']}</td>"
        f"<td>{r['opens']}</td>"
        f"<td>{r['verified_opens']}</td>"
        f"<td>{r['unique_recipients_opened']}</td>"
        f"<td>{r['link_clicks']}</td>"
        f"</tr>"
        for r in rows
    )

    if not rows:
        row_html = '<tr><td colspan="6" style="text-align:center;color:#888;">No activity in this period.</td></tr>'

    return f"""
<html>
<head>
<meta charset="utf-8">
<title>Recon {escape(period_label)} Report</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; background: #f7f7f8; color: #222; padding: 24px; }}
  h1 {{ color: {ACCENT_GREEN}; font-size: 20px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 14px; }}
  th {{ background: {ACCENT_GREEN}; color: #fff; font-weight: 600; }}
  tr:last-child td {{ border-bottom: none; }}
</style>
</head>
<body>
  <h1>Recon &mdash; {escape(period_label)} Report</h1>
  <div class="meta">Since {escape(since_str)}</div>
  <table>
    <thead>
      <tr>
        <th>Sender</th>
        <th>Emails Sent</th>
        <th>Opens</th>
        <th>Verified Opens</th>
        <th>Unique Recipients Opened</th>
        <th>Link Clicks</th>
      </tr>
    </thead>
    <tbody>
      {row_html}
    </tbody>
  </table>
</body>
</html>
"""
