from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

load_dotenv()

from .auth import require_api_key
from .database import Base, engine, get_db, async_session
from .models import Email, Link, LinkClick, Open
from .notify import post_slack
from .reports import build_report
from .routes import links, mute, pixel, reports, status, track

scheduler = AsyncIOScheduler()


async def _send_periodic_report(period_label: str, days: int) -> None:
    import os

    if not os.environ.get("SLACK_WEBHOOK_URL", "").strip():
        print(f"[scheduler] SLACK_WEBHOOK_URL not set, skipping {period_label} report")
        return

    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with async_session() as db:
        senders_result = await db.execute(select(Email.sender_email).distinct())
        senders = [row[0] for row in senders_result.all()]

        for sender_email in senders:
            report_data = await build_report(db, sender_email, since)
            rows = report_data.get("senders", [])
            if not rows:
                continue
            r = rows[0]
            message = (
                f"\U0001f4ca {period_label} report for {sender_email}: "
                f"{r['emails_sent']} sent, {r['opens']} opens "
                f"({r['verified_opens']} verified), "
                f"{r['unique_recipients_opened']} unique recipients opened, "
                f"{r['link_clicks']} link clicks"
            )
            await post_slack(message)


async def _weekly_report_job() -> None:
    await _send_periodic_report("Weekly", 7)


async def _monthly_report_job() -> None:
    await _send_periodic_report("Monthly", 30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            await conn.execute(text("ALTER TABLE emails ADD COLUMN IF NOT EXISTS recipient_field VARCHAR(10) DEFAULT 'to'"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE opens ADD COLUMN IF NOT EXISTS internal BOOLEAN DEFAULT FALSE"))
            await conn.execute(text("ALTER TABLE link_clicks ADD COLUMN IF NOT EXISTS internal BOOLEAN DEFAULT FALSE"))
        except Exception:
            pass

    scheduler.add_job(_weekly_report_job, CronTrigger(day_of_week="mon", hour=9, minute=0))
    scheduler.add_job(_monthly_report_job, CronTrigger(day="1", hour=9, minute=0))
    scheduler.start()

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Recon",
    description="Email intelligence API",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(track.router)
app.include_router(pixel.router)
app.include_router(status.router)
app.include_router(links.router)
app.include_router(reports.router)
app.include_router(mute.router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug/emails", dependencies=[Depends(require_api_key)])
async def debug_emails(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Email).order_by(Email.created_at.desc()).limit(20)
    )
    emails = result.scalars().all()

    output = []
    for e in emails:
        opens_result = await db.execute(
            select(func.count(Open.id)).where(Open.email_id == e.id, Open.internal == False)
        )
        total_opens = opens_result.scalar() or 0

        verified_result = await db.execute(
            select(func.count(Open.id)).where(
                Open.email_id == e.id, Open.verified == True, Open.internal == False
            )
        )
        verified_opens = verified_result.scalar() or 0

        last_result = await db.execute(
            select(Open.opened_at)
            .where(Open.email_id == e.id, Open.internal == False)
            .order_by(Open.opened_at.desc())
            .limit(1)
        )
        last_opened = last_result.scalar()

        output.append({
            "id": e.id,
            "sender": e.sender_email,
            "recipient": e.recipient_email,
            "recipient_field": getattr(e, "recipient_field", "to"),
            "subject": e.subject,
            "thread_id": e.thread_id,
            "created_at": str(e.created_at),
            "total_opens": total_opens,
            "verified_opens": verified_opens,
            "last_opened_at": str(last_opened) if last_opened else None,
        })

    return output


@app.get("/status/sent", dependencies=[Depends(require_api_key)])
async def get_sent_status(
    sender_email: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(
            Email,
            func.count(Open.id).filter(Open.internal == False).label("total_opens"),
            func.count(Open.id).filter(Open.verified == True, Open.internal == False).label("verified_opens"),
        )
        .outerjoin(Open, Email.id == Open.email_id)
        .where(Email.sender_email == sender_email)
        .group_by(Email.id)
        .order_by(Email.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.all()

    output = []
    for email, total_opens, verified_opens in rows:
        last_result = await db.execute(
            select(Open.opened_at)
            .where(Open.email_id == email.id, Open.internal == False)
            .order_by(Open.opened_at.desc())
            .limit(1)
        )
        last_opened = last_result.scalar()

        links_result = await db.execute(
            select(Link).where(Link.email_id == email.id).order_by(Link.created_at)
        )
        link_rows = links_result.scalars().all()

        links_out = []
        for link in link_rows:
            clicks_count_result = await db.execute(
                select(func.count(LinkClick.id)).where(
                    LinkClick.link_id == link.id, LinkClick.internal == False
                )
            )
            clicks = clicks_count_result.scalar() or 0

            last_click_result = await db.execute(
                select(func.max(LinkClick.clicked_at)).where(
                    LinkClick.link_id == link.id, LinkClick.internal == False
                )
            )
            last_clicked_at = last_click_result.scalar()

            links_out.append({
                "link_id": link.id,
                "url": link.original_url,
                "type": link.link_type,
                "clicks": clicks,
                "last_clicked_at": str(last_clicked_at) if last_clicked_at else None,
            })

        output.append({
            "id": email.id,
            "recipient": email.recipient_email,
            "recipient_field": getattr(email, "recipient_field", "to"),
            "subject": email.subject,
            "thread_id": email.thread_id,
            "created_at": str(email.created_at),
            "total_opens": total_opens,
            "verified_opens": verified_opens,
            "last_opened_at": str(last_opened) if last_opened else None,
            "links": links_out,
        })

    return output
