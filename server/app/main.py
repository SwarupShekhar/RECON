import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

try:
    import sentry_sdk
except Exception:  # pragma: no cover - optional dependency fallback
    sentry_sdk = None

load_dotenv()

from .auth import resolve_sender_email
from .database import Base, async_session, engine, get_db
from .models import Email, Link, LinkClick, Open, User
from .notify import post_slack
from .reports import build_report
from .routes import links, mute, pixel, reports, status, track

scheduler = AsyncIOScheduler()
_last_digest_snapshot: dict[str, dict[str, int]] = {}


def _env_truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def init_sentry() -> None:
    if sentry_sdk is None:
        logger.warning("Sentry disabled: sentry_sdk package not installed")
        return

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("Sentry disabled: SENTRY_DSN not configured")
        return

    traces_sample_rate_raw = os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1").strip() or "0.1"
    try:
        traces_sample_rate = float(traces_sample_rate_raw)
    except ValueError:
        traces_sample_rate = 0.1

    sentry_sdk.init(
        dsn=dsn,
        send_default_pii=_env_truthy(os.environ.get("SENTRY_SEND_DEFAULT_PII"), default=False),
        traces_sample_rate=traces_sample_rate,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
    )
    logger.info("Sentry enabled")


async def _send_periodic_report(period_label: str, days: int) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with async_session() as db:
        senders_result = await db.execute(select(Email.sender_email).distinct())
        senders = [row[0] for row in senders_result.all()]

        for sender_email in senders:
            user_result = await db.execute(
                select(User).where(User.email == sender_email)
            )
            user = user_result.scalar_one_or_none()
            if user and not user.alerts_enabled:
                continue

            webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
            if not webhook_url:
                print(f"[scheduler] No team Slack webhook configured, skipping")
                continue

            report_data = await build_report(db, sender_email, since)
            rows = report_data.get("senders", [])
            if not rows:
                continue
            r = rows[0]
            digest_key = f"{period_label}:{sender_email}"
            current = {
                "emails_sent": r["emails_sent"],
                "opens": r["opens"],
                "verified_opens": r["verified_opens"],
                "link_clicks": r["link_clicks"],
            }
            prev = _last_digest_snapshot.get(digest_key)
            if prev == current:
                continue
            _last_digest_snapshot[digest_key] = current

            message = (
                f"\U0001f4ca {period_label} report for {sender_email}: "
                f"{r['emails_sent']} sent, {r['opens']} opens "
                f"({r['verified_opens']} verified), "
                f"{r['unique_recipients_opened']} unique recipients opened, "
                f"{r['link_clicks']} link clicks"
            )
            await post_slack(message, webhook_url=webhook_url)


async def _weekly_report_job() -> None:
    await _send_periodic_report("Weekly", 7)


async def _monthly_report_job() -> None:
    await _send_periodic_report("Monthly", 30)


async def _keep_db_warm() -> None:
    """Ping the DB so Neon's serverless compute never idles into suspend.

    A suspended Neon compute takes ~2-3s to wake on the next request, which
    is the main source of dashboard-refresh latency. Running a trivial
    ``SELECT 1`` every few minutes keeps the compute active. Any transient
    DB blip is swallowed so it can never crash the scheduler.
    """
    try:
        # Bound the ping so a degraded/hung DB can't wedge the coroutine and its
        # pooled connection indefinitely.
        async with async_session() as db:
            await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=10)
        logger.debug("[scheduler] keep-warm ping ok")
    except Exception as exc:  # noqa: BLE001 - never let a blip kill the job
        # Warn (not debug) so repeated DB connectivity issues are visible in prod.
        logger.warning("[scheduler] keep-warm ping failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .auth import warm_jwks_cache

    await warm_jwks_cache()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            await conn.execute(text("ALTER TABLE emails ADD COLUMN IF NOT EXISTS recipient_field VARCHAR(10) DEFAULT 'to'"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE opens ADD COLUMN IF NOT EXISTS internal BOOLEAN DEFAULT FALSE"))
            await conn.execute(text("ALTER TABLE link_clicks ADD COLUMN IF NOT EXISTS internal BOOLEAN DEFAULT FALSE"))
            await conn.execute(text("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS token_enc TEXT"))
        except Exception:
            pass
        # create_all won't add indexes to already-existing tables, so create the
        # composite indexes explicitly (IF NOT EXISTS) — mirrors the model's
        # Index() declarations. Non-CONCURRENT so it's valid inside this txn.
        try:
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_emails_sender_created_at ON emails (sender_email, created_at DESC)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_opens_email_internal ON opens (email_id, internal)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_opens_email_internal_opened_at ON opens (email_id, internal, opened_at DESC)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_link_clicks_link_internal ON link_clicks (link_id, internal)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_link_clicks_link_internal_clicked_at ON link_clicks (link_id, internal, clicked_at DESC)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_links_email_created_at ON links (email_id, created_at)"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE emails ADD COLUMN IF NOT EXISTS user_id VARCHAR(64) REFERENCES users(id)"))
        except Exception:
            pass

    scheduler.add_job(_weekly_report_job, CronTrigger(day_of_week="mon", hour=9, minute=0))
    scheduler.add_job(_monthly_report_job, CronTrigger(day="1", hour=9, minute=0))
    scheduler.add_job(
        _keep_db_warm,
        IntervalTrigger(minutes=4),
        id="keep_db_warm",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()

    yield

    scheduler.shutdown(wait=False)


init_sentry()

app = FastAPI(
    title="Recon",
    description="Email intelligence API",
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


# ---------------------------------------------------------------------------
# Error handlers: HTML for dashboard, JSON for extension/API routes
# ---------------------------------------------------------------------------
def _api_style_response(request: Request) -> bool:
    """Extension + JSON API calls need JSON errors, not plain-text HTML."""
    if request.headers.get("x-api-key"):
        return True
    path = request.url.path
    return path.startswith((
        "/status", "/track", "/me", "/debug", "/reports", "/mute", "/health",
    )) or path.startswith("/t/") or path.startswith("/l/") or path == "/dashboard/data"


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/dashboard"):
        if exc.status_code == 401:
            return RedirectResponse(url="/login", status_code=302)
        return auth_templates.TemplateResponse(
            "error.html", {"request": request, "detail": str(exc.detail)}, status_code=exc.status_code
        )
    if _api_style_response(request):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return HTMLResponse(content=str(exc.detail), status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    if request.url.path.startswith("/dashboard") or request.url.path == "/":
        return auth_templates.TemplateResponse(
            "error.html",
            {"request": request, "detail": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )
    if _api_style_response(request):
        return JSONResponse(
            {"detail": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )
    return HTMLResponse(content="Internal Server Error", status_code=500)


# ---------------------------------------------------------------------------
# Dashboard + auth routes (registered first for priority)
# ---------------------------------------------------------------------------
from .routes.auth_routes import router as auth_router
from .routes.auth_routes import templates as auth_templates

app.include_router(auth_router)

# ---------------------------------------------------------------------------
# API routes (unchanged, some scoped to API key auth)
# ---------------------------------------------------------------------------
app.include_router(track.router)
app.include_router(pixel.router)
app.include_router(status.router)
app.include_router(links.router)
app.include_router(reports.router)
app.include_router(mute.router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/health")
async def health():
    return {"status": "ok"}


if _env_truthy(os.environ.get("SENTRY_DEBUG_ROUTE"), default=False):
    @app.get("/sentry-debug")
    async def trigger_sentry_error():
        raise RuntimeError("Sentry debug route triggered")


# ---------------------------------------------------------------------------
# Debug + status endpoints — scoped to API key auth
# ---------------------------------------------------------------------------
@app.get("/debug/emails")
async def debug_emails(
    sender_email: str = Depends(resolve_sender_email),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Email)
        .where(Email.sender_email == sender_email)
        .order_by(Email.created_at.desc())
        .limit(20)
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


@app.get("/status/sent")
async def get_sent_status(
    sender_email: str = Depends(resolve_sender_email),
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
    email_ids = [email.id for email, _, _ in rows]

    last_opened_map: dict = {}
    if email_ids:
        last_opened_result = await db.execute(
            select(Open.email_id, func.max(Open.opened_at))
            .where(Open.email_id.in_(email_ids), Open.internal == False)
            .group_by(Open.email_id)
        )
        last_opened_map = dict(last_opened_result.all())

    links_by_email: dict = {}
    if email_ids:
        links_result = await db.execute(
            select(Link).where(Link.email_id.in_(email_ids)).order_by(Link.created_at)
        )
        for link in links_result.scalars().all():
            links_by_email.setdefault(link.email_id, []).append(link)

    link_ids = [link.id for links in links_by_email.values() for link in links]
    clicks_map: dict = {}
    last_click_map: dict = {}
    if link_ids:
        clicks_result = await db.execute(
            select(LinkClick.link_id, func.count(LinkClick.id), func.max(LinkClick.clicked_at))
            .where(LinkClick.link_id.in_(link_ids), LinkClick.internal == False)
            .group_by(LinkClick.link_id)
        )
        for link_id, count, last_clicked_at in clicks_result.all():
            clicks_map[link_id] = count
            last_click_map[link_id] = last_clicked_at

    output = []
    for email, total_opens, verified_opens in rows:
        last_opened = last_opened_map.get(email.id)

        links_out = []
        for link in links_by_email.get(email.id, []):
            links_out.append({
                "link_id": link.id,
                "url": link.original_url,
                "type": link.link_type,
                "clicks": clicks_map.get(link.id, 0),
                "last_clicked_at": str(last_click_map.get(link.id)) if last_click_map.get(link.id) else None,
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
