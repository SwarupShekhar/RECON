import hashlib
import io
import json
import os
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user, get_current_user_from_api_key
from ..database import get_db
from ..models import ApiKey, Email, Link, LinkClick, Open, User
from ..reports import build_report

router = APIRouter()

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

# Clerk's own JS SDK, loaded directly on our domain, is what actually
# establishes the `__session` cookie `get_current_user` reads (see auth.py).
# A bare server-side redirect to Clerk's hosted Account Portal never runs
# that JS, so no cookie was ever set — that's why the previous /login just
# looped back to a 401. Embedding <SignIn/> in-page is Clerk's documented
# path for a non-SPA backend that isn't using their Account Portal domain.
_clerk_jwks_url = os.environ.get("CLERK_JWKS_URL", "")
_clerk_frontend_api = _clerk_jwks_url.replace("/.well-known/jwks.json", "")
_clerk_frontend_api = _clerk_frontend_api.removeprefix("https://").removeprefix("http://")
templates.env.globals["clerk_publishable_key"] = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
templates.env.globals["clerk_frontend_api"] = _clerk_frontend_api

FOLLOWUP_AFTER_DAYS = 2


def _time_ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = (datetime.now(timezone.utc) - dt).total_seconds()
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    if diff < 604800:
        return f"{int(diff // 86400)}d ago"
    return dt.strftime("%b %d")


def _needs_followup(created_at: datetime | None, total_opens: int) -> bool:
    if total_opens > 0 or not created_at:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(days=FOLLOWUP_AFTER_DAYS)
    return created_at < cutoff


templates.env.filters["time_ago"] = _time_ago

EXTENSION_DIR = Path(__file__).resolve().parents[3] / "extension"
SKIP_EXTENSION_FILES = {".DS_Store"}


def _dashboard_base_url(request: Request) -> str:
    configured = os.environ.get("DASHBOARD_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _build_extension_zip(server_url: str = "") -> io.BytesIO:
    if not EXTENSION_DIR.is_dir():
        raise FileNotFoundError(f"Extension folder not found at {EXTENSION_DIR}")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in EXTENSION_DIR.rglob("*"):
            if not path.is_file() or path.name in SKIP_EXTENSION_FILES:
                continue
            if path.name == "config.defaults.js":
                continue
            arcname = Path("recon-extension") / path.relative_to(EXTENSION_DIR)
            zf.write(path, arcname)

        safe_url = server_url.replace("\\", "\\\\").replace('"', '\\"')
        defaults_js = f'const RECON_DEFAULT_SERVER_URL = "{safe_url}";\n'
        zf.writestr("recon-extension/config.defaults.js", defaults_js)

    buffer.seek(0)
    return buffer


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _grouped_recipients(all_recipients_json: str | None, fallback_email: str):
    """Turn the stored all_recipients JSON (list of {email, field}) into an
    ordered [(field, [emails])] list for the dashboard, so Cc/Bcc show up
    instead of only the single primary `to` recipient. Falls back to the
    primary recipient for older rows that predate all_recipients capture."""
    recips = []
    if all_recipients_json:
        try:
            recips = json.loads(all_recipients_json)
        except (ValueError, TypeError):
            recips = []

    groups: dict[str, list[str]] = {"to": [], "cc": [], "bcc": []}
    for r in recips:
        email = (r.get("email") or "").strip()
        if not email:
            continue
        groups.setdefault(r.get("field", "to"), []).append(email)

    ordered = [(field, groups[field]) for field in ("to", "cc", "bcc") if groups.get(field)]
    if not ordered and fallback_email:
        ordered = [("to", [fallback_email])]
    return ordered


# ---------------------------------------------------------------------------
# Auth flow
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/auth/callback")
async def auth_callback():
    # Not on the critical path — Clerk's mounted <SignIn/> navigates to
    # /dashboard client-side once the session is confirmed. Kept as a
    # harmless fallback in case a Clerk dashboard setting ever targets it.
    return RedirectResponse(url="/dashboard")


@router.post("/auth/logout")
async def auth_logout():
    # Clerk's own session (on Clerk's domain) is what must actually be
    # invalidated — that happens client-side via Clerk.signOut() before this
    # is called (see base.html). This just clears our copy of the cookie.
    response = RedirectResponse(url="/login")
    response.delete_cookie("__session")
    return response


# ---------------------------------------------------------------------------
# Dashboard pages
# ---------------------------------------------------------------------------

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            Email,
            func.count(Open.id).filter(Open.internal == False).label("total_opens"),
            func.count(Open.id).filter(Open.verified == True, Open.internal == False).label("verified_opens"),
        )
        .outerjoin(Open, Email.id == Open.email_id)
        .where(Email.sender_email == user.email)
        .group_by(Email.id)
        .order_by(Email.created_at.desc())
    )
    rows = result.all()
    email_ids = [email.id for email, _, _ in rows]

    last_opened_map: dict = {}
    if email_ids:
        lo_result = await db.execute(
            select(Open.email_id, func.max(Open.opened_at))
            .where(Open.email_id.in_(email_ids), Open.internal == False)
            .group_by(Open.email_id)
        )
        last_opened_map = dict(lo_result.all())

    link_clicks_map: dict = {}
    if email_ids:
        lc_result = await db.execute(
            select(Link.email_id, func.count(LinkClick.id))
            .join(LinkClick, Link.id == LinkClick.link_id)
            .where(Link.email_id.in_(email_ids), LinkClick.internal == False)
            .group_by(Link.email_id)
        )
        link_clicks_map = dict(lc_result.all())

    emails = []
    stats = {"total": 0, "opened": 0, "needs_followup": 0, "clicked": 0}
    for email, total_opens, verified_opens in rows:
        total_opens = total_opens or 0
        verified_opens = verified_opens or 0
        link_clicks = link_clicks_map.get(email.id, 0) or 0
        followup = _needs_followup(email.created_at, total_opens)
        filter_tags = []
        if followup:
            filter_tags.append("followup")
        if total_opens > 0:
            filter_tags.append("opened")
        else:
            filter_tags.append("unopened")
        if link_clicks > 0:
            filter_tags.append("clicked")
        if verified_opens > 0 or (total_opens > 0 and verified_opens == 0):
            stats["opened"] += 1
        if followup:
            stats["needs_followup"] += 1
        if link_clicks > 0:
            stats["clicked"] += 1
        emails.append({
            "id": email.id,
            "subject": email.subject,
            "recipient_email": email.recipient_email,
            "recipients": _grouped_recipients(email.all_recipients, email.recipient_email),
            "created_at": email.created_at,
            "total_opens": total_opens,
            "verified_opens": verified_opens,
            "last_opened": last_opened_map.get(email.id),
            "link_clicks": link_clicks,
            "needs_followup": followup,
            "filter_tags": " ".join(filter_tags),
        })
        stats["total"] += 1

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "emails": emails, "stats": stats, "user": user,
    })


@router.get("/dashboard/email/{email_id}", response_class=HTMLResponse)
async def email_detail_page(
    request: Request,
    email_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Email).where(Email.id == email_id))
    email = result.scalar_one_or_none()
    if not email or email.sender_email != user.email:
        return RedirectResponse(url="/dashboard")

    opens_result = await db.execute(
        select(Open).where(Open.email_id == email_id).order_by(Open.opened_at.desc())
    )
    opens = opens_result.scalars().all()
    external_opens = [o for o in opens if not o.internal]
    verified_opens = sum(1 for o in external_opens if o.verified)

    links_result = await db.execute(
        select(
            Link,
            func.count(LinkClick.id).filter(LinkClick.internal == False).label("click_count"),
        )
        .outerjoin(LinkClick, Link.id == LinkClick.link_id)
        .where(Link.email_id == email_id)
        .group_by(Link.id)
        .order_by(Link.created_at)
    )
    link_rows = links_result.all()
    links = []
    for link, click_count in link_rows:
        clicks_result = await db.execute(
            select(LinkClick)
            .where(LinkClick.link_id == link.id, LinkClick.internal == False)
            .order_by(LinkClick.clicked_at.desc())
        )
        links.append({
            "original_url": link.original_url,
            "link_type": link.link_type,
            "click_count": click_count,
            "clicks": clicks_result.scalars().all(),
        })

    total_clicks = sum(l["click_count"] for l in links)

    return templates.TemplateResponse("email_detail.html", {
        "request": request,
        "user": user,
        "email": email,
        "opens": opens,
        "external_opens": external_opens,
        "verified_opens": verified_opens,
        "links": links,
        "total_clicks": total_clicks,
    })


@router.get("/dashboard/reports", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    weekly_since = datetime.now(timezone.utc) - timedelta(days=7)
    monthly_since = datetime.now(timezone.utc) - timedelta(days=30)

    weekly_data = await build_report(db, user.email, weekly_since)
    monthly_data = await build_report(db, user.email, monthly_since)

    return templates.TemplateResponse("reports.html", {
        "request": request,
        "user": user,
        "weekly_senders": weekly_data.get("senders", []),
        "monthly_senders": monthly_data.get("senders", []),
    })


@router.get("/dashboard/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    saved: str | None = None,
    user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(
        "settings.html", {"request": request, "user": user, "saved": saved == "1"}
    )


@router.post("/dashboard/settings")
async def settings_update(
    request: Request,
    alerts_enabled: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user.alerts_enabled = alerts_enabled == "true"
    await db.commit()
    return RedirectResponse(url="/dashboard/settings?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Setup + extension download
# ---------------------------------------------------------------------------

@router.get("/dashboard/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    user: User = Depends(get_current_user),
):
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "user": user,
        "base_url": _dashboard_base_url(request),
    })


@router.get("/dashboard/extension/download")
async def download_extension(
    request: Request,
    user: User = Depends(get_current_user),
):
    try:
        buffer = _build_extension_zip(_dashboard_base_url(request))
    except FileNotFoundError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)

    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="recon-extension.zip"'},
    )


# ---------------------------------------------------------------------------
# Extension key management
# ---------------------------------------------------------------------------

@router.get("/dashboard/extension-key", response_class=HTMLResponse)
async def extension_key_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    keys = []
    for k in result.scalars().all():
        masked = k.token_hash[:8] + "..." + k.token_hash[-4:] if len(k.token_hash) > 12 else k.token_hash
        keys.append({
            "id": k.id,
            "masked": masked,
            "created_at": k.created_at,
            "last_used_at": k.last_used_at,
            "revoked": k.revoked,
        })
    return templates.TemplateResponse("extension_key.html", {
        "request": request, "user": user, "keys": keys, "new_key": None,
        "base_url": _dashboard_base_url(request),
    })


@router.post("/dashboard/extension-key")
async def extension_key_generate(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    raw_key = "recon_" + secrets.token_hex(32)
    token_hash = _hash_token(raw_key)
    api_key = ApiKey(user_id=user.id, token_hash=token_hash)
    db.add(api_key)
    await db.commit()

    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    keys = []
    for k in result.scalars().all():
        masked = k.token_hash[:8] + "..." + k.token_hash[-4:] if len(k.token_hash) > 12 else k.token_hash
        keys.append({
            "id": k.id,
            "masked": masked,
            "created_at": k.created_at,
            "last_used_at": k.last_used_at,
            "revoked": k.revoked,
        })

    return templates.TemplateResponse("extension_key.html", {
        "request": request, "user": user, "keys": keys, "new_key": raw_key,
        "base_url": _dashboard_base_url(request),
    })


@router.post("/dashboard/extension-key/{key_id}/revoke")
async def extension_key_revoke(
    key_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    api_key = result.scalar_one_or_none()
    if api_key:
        api_key.revoked = True
        await db.commit()
    return RedirectResponse(url="/dashboard/extension-key", status_code=303)


# ---------------------------------------------------------------------------
# API endpoint
# ---------------------------------------------------------------------------

@router.get("/me")
async def me(user: User = Depends(get_current_user_from_api_key)):
    return JSONResponse({"email": user.email, "alerts_enabled": user.alerts_enabled})
