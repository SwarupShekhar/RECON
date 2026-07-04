from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Base, engine, get_db
from .models import Email, Open
from .routes import pixel, status, track


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            await conn.execute(text("ALTER TABLE emails ADD COLUMN IF NOT EXISTS recipient_field VARCHAR(10) DEFAULT 'to'"))
        except Exception:
            pass
    yield


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


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug/emails")
async def debug_emails(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Email).order_by(Email.created_at.desc()).limit(20)
    )
    emails = result.scalars().all()

    output = []
    for e in emails:
        opens_result = await db.execute(
            select(func.count(Open.id)).where(Open.email_id == e.id)
        )
        total_opens = opens_result.scalar() or 0

        verified_result = await db.execute(
            select(func.count(Open.id)).where(Open.email_id == e.id, Open.verified == True)
        )
        verified_opens = verified_result.scalar() or 0

        last_result = await db.execute(
            select(Open.opened_at).where(Open.email_id == e.id).order_by(Open.opened_at.desc()).limit(1)
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


@app.get("/status/sent")
async def get_sent_status(
    sender_email: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(
            Email,
            func.count(Open.id).label("total_opens"),
            func.count(Open.id).filter(Open.verified == True).label("verified_opens"),
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
            select(Open.opened_at).where(Open.email_id == email.id).order_by(Open.opened_at.desc()).limit(1)
        )
        last_opened = last_result.scalar()

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
        })

    return output
