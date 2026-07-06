from datetime import datetime
from html import escape

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Email, Link, LinkClick, Open

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
                "emails_opened": 0,
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
            select(Open.email_id, Open.verified).where(
                Open.email_id.in_(all_email_ids),
                Open.internal == False,
            )
        )
        emails_opened: dict[str, set[str]] = {}
        for email_id, verified in opens_result.all():
            sender = email_id_to_sender.get(email_id)
            if not sender:
                continue
            b = bucket(sender)
            b["opens"] += 1
            emails_opened.setdefault(sender, set()).add(email_id)
            if verified:
                b["verified_opens"] += 1
                b["unique_recipients_opened"].add(email_id_to_recipient.get(email_id))

        for sender, opened_ids in emails_opened.items():
            bucket(sender)["emails_opened"] = len(opened_ids)

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
                select(LinkClick.link_id).where(
                    LinkClick.link_id.in_(link_id_to_sender.keys()),
                    LinkClick.internal == False,
                )
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
                "emails_opened": data.get("emails_opened", 0),
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
        row_html = '<tr><td colspan="6" style="text-align:center;color:#8a8f86;">No activity in this period.</td></tr>'

    return f"""
<html>
<head>
<meta charset="utf-8">
<title>Recon {escape(period_label)} Report</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; background: #f8f8f6; color: #23261f; padding: 32px; font-size: 14px; }}
  .wrap {{ max-width: 720px; margin: 0 auto; }}
  h1 {{ font-size: 1.25rem; font-weight: 650; letter-spacing: -0.01em; margin: 0 0 4px; }}
  .meta {{ color: #6b6f66; font-size: 0.8125rem; margin-bottom: 20px; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; border: 1px solid #e2e4e0; border-radius: 8px; overflow: hidden; }}
  th, td {{ padding: 9px 14px; text-align: left; border-bottom: 1px solid #e2e4e0; font-size: 0.8125rem; }}
  th {{ color: #6b6f66; font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.02em; }}
  tr:last-child td {{ border-bottom: none; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>{escape(period_label)} Report</h1>
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
  </div>
</body>
</html>
"""
