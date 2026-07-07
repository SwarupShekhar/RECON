import csv
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
import base64


def _frontend_api_from_publishable_key(publishable_key: str) -> str:
    """Clerk embeds the instance Frontend API host in the publishable key."""
    if not publishable_key or not publishable_key.startswith("pk_"):
        return ""
    encoded = publishable_key.split("_", 2)[-1]
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        host = base64.b64decode(padded).decode("utf-8").rstrip("$")
        return host.removeprefix("https://").removeprefix("http://").rstrip("/")
    except Exception:
        return ""


def _resolve_clerk_frontend_api() -> str:
    configured = os.environ.get("CLERK_FRONTEND_API", "").strip()
    if configured:
        return configured.removeprefix("https://").removeprefix("http://").rstrip("/")

    from_key = _frontend_api_from_publishable_key(os.environ.get("CLERK_PUBLISHABLE_KEY", ""))
    if from_key:
        return from_key

    jwks_url = os.environ.get("CLERK_JWKS_URL", "").strip()
    return jwks_url.replace("/.well-known/jwks.json", "").removeprefix("https://").removeprefix("http://").rstrip("/")


_clerk_frontend_api = _resolve_clerk_frontend_api()
templates.env.globals["clerk_publishable_key"] = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
templates.env.globals["clerk_frontend_api"] = _clerk_frontend_api
# Custom Clerk domains can have TLS issues before Clerk finishes provisioning.
# jsDelivr serves clerk-js reliably; the publishable key selects the right instance.
templates.env.globals["clerk_js_url"] = os.environ.get(
    "CLERK_JS_URL",
    "https://cdn.jsdelivr.net/npm/@clerk/clerk-js@5/dist/clerk.browser.js",
)

FOLLOWUP_AFTER_DAYS = 2
PROXY_UA_FRAGMENTS = (
    "GoogleImageProxy",
    "CloudImageProxy",
    "Barracuda",
    "Proofpoint",
    "Mimecast",
)


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


def _is_proxy_user_agent(user_agent: str | None) -> bool:
    if not user_agent:
        return False
    return any(frag in user_agent for frag in PROXY_UA_FRAGMENTS)


def _open_quality_label(human_likely_opens: int, proxy_or_preload_opens: int) -> str:
    if human_likely_opens > 0 and proxy_or_preload_opens > 0:
        return "Mixed"
    if human_likely_opens > 0:
        return "Human likely"
    if proxy_or_preload_opens > 0:
        return "Proxy/preload"
    return "—"


def _recipient_capture_badge(all_recipients_json: str | None, grouped_recipients: list[tuple[str, list[str]]]) -> str:
    fields = [field.upper() for field, addrs in grouped_recipients if addrs]
    if not fields:
        return "Captured: To"
    uniq = []
    for f in fields:
        if f not in uniq:
            uniq.append(f)
    prefix = "Captured"
    if not all_recipients_json:
        prefix = "Captured (fallback)"
    return f"{prefix}: {'/'.join(uniq)}"


def _followup_draft(email: dict) -> str:
    if not email.get("needs_followup"):
        return ""
    to_addrs = [r["email"] for r in email.get("recipients_flat", []) if r.get("field") == "to"]
    cc_addrs = [r["email"] for r in email.get("recipients_flat", []) if r.get("field") == "cc"]
    greet = "Hi there,"
    if to_addrs:
        greet = f"Hi {to_addrs[0].split('@')[0]},"
    subject = email.get("subject") or "(no subject)"
    cc_line = f"\n(Cc: {', '.join(cc_addrs)})" if cc_addrs else ""
    return (
        f"{greet}\n\n"
        f"Quick follow-up on \"{subject}\" — sharing this again in case it got buried.{cc_line}\n\n"
        "Happy to help if you have any questions.\n\n"
        "Best,\n"
    )


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
        # `var` (not `const`): the service worker re-injects this file into the
        # same isolated world on every Gmail SPA "complete" event. `const`
        # throws "already declared" on re-injection; `var` redeclaration is a
        # harmless no-op.
        defaults_js = f'var RECON_DEFAULT_SERVER_URL = "{safe_url}";\n'
        zf.writestr("recon-extension/config.defaults.js", defaults_js)

    buffer.seek(0)
    return buffer


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _merge_recipients_json(a: str | None, b: str | None) -> str | None:
    combined: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for src in (a, b):
        if not src:
            continue
        try:
            items = json.loads(src)
        except (ValueError, TypeError):
            continue
        for r in items:
            email = (r.get("email") or "").strip()
            field = r.get("field", "to") or "to"
            key = (email.lower(), field)
            if email and key not in seen:
                seen.add(key)
                combined.append({"email": email, "field": field})
    if not combined:
        return a or b
    return json.dumps(combined)


def _dedupe_dashboard_rows(rows: list) -> list:
    """Collapse accidental double-/track rows from the same Gmail send."""
    kept: list = []
    index_by_key: dict = {}

    for row in rows:
        email, total_opens, verified_opens = row[0], row[1], row[2]
        created = email.created_at
        bucket = None
        if created:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            bucket = created.replace(second=(created.second // 30) * 30, microsecond=0)
        key = (email.subject, email.thread_id, bucket)
        if key in index_by_key:
            prev_idx = index_by_key[key]
            prev_email, prev_opens, prev_verified = kept[prev_idx][0], kept[prev_idx][1], kept[prev_idx][2]
            merged = _merge_recipients_json(prev_email.all_recipients, email.all_recipients)
            if merged:
                prev_email.all_recipients = merged
            if (total_opens or 0) > (prev_opens or 0):
                kept[prev_idx] = (prev_email, total_opens, verified_opens) + tuple(row[3:])
            continue
        index_by_key[key] = len(kept)
        kept.append(row)
    return kept


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
    seen_emails: set[tuple[str, str]] = set()
    for r in recips:
        email = (r.get("email") or "").strip()
        if not email:
            continue
        field = r.get("field", "to") or "to"
        key = (email.lower(), field)
        if key in seen_emails:
            continue
        seen_emails.add(key)
        groups.setdefault(field, []).append(email)

    ordered = [(field, groups[field]) for field in ("to", "cc", "bcc") if groups.get(field)]
    if not ordered and fallback_email:
        ordered = [("to", [fallback_email])]
    return ordered


async def _load_dashboard_data(user: User, db: AsyncSession) -> tuple[list[dict], dict]:
    """Single round-trip dashboard query with correlated subqueries for last-open and clicks."""
    last_opened_sq = (
        select(func.max(Open.opened_at))
        .where(Open.email_id == Email.id, Open.internal == False)
        .correlate(Email)
        .scalar_subquery()
    )
    link_clicks_sq = (
        select(func.count(LinkClick.id))
        .select_from(Link)
        .join(LinkClick, Link.id == LinkClick.link_id)
        .where(Link.email_id == Email.id, LinkClick.internal == False)
        .correlate(Email)
        .scalar_subquery()
    )

    result = await db.execute(
        select(
            Email,
            func.count(Open.id).filter(Open.internal == False).label("total_opens"),
            func.count(Open.id).filter(Open.verified == True, Open.internal == False).label("verified_opens"),
            last_opened_sq.label("last_opened"),
            link_clicks_sq.label("link_clicks"),
        )
        .outerjoin(Open, Email.id == Open.email_id)
        .where(Email.sender_email == user.email)
        .group_by(Email.id)
        .order_by(Email.created_at.desc())
        .limit(200)
    )
    rows = _dedupe_dashboard_rows(result.all())
    email_ids = [row[0].id for row in rows]

    open_quality_counts: dict[str, dict[str, int]] = {}
    if email_ids:
        opens_result = await db.execute(
            select(Open.email_id, Open.verified, Open.user_agent)
            .where(Open.email_id.in_(email_ids), Open.internal == False)
        )
        for email_id, verified, user_agent in opens_result.all():
            bucket = open_quality_counts.setdefault(
                email_id, {"human_likely_opens": 0, "proxy_or_preload_opens": 0}
            )
            if verified and not _is_proxy_user_agent(user_agent):
                bucket["human_likely_opens"] += 1
            else:
                bucket["proxy_or_preload_opens"] += 1

    emails: list[dict] = []
    stats = {
        "total": 0,
        "opened": 0,
        "needs_followup": 0,
        "clicked": 0,
        "human_likely_opened": 0,
        "proxy_or_preload_only": 0,
    }
    for email, total_opens, verified_opens, last_opened, link_clicks in rows:
        total_opens = total_opens or 0
        verified_opens = verified_opens or 0
        link_clicks = link_clicks or 0
        followup = _needs_followup(email.created_at, total_opens)
        quality = open_quality_counts.get(
            email.id, {"human_likely_opens": 0, "proxy_or_preload_opens": 0}
        )
        grouped_recipients = _grouped_recipients(email.all_recipients, email.recipient_email)
        recipients_flat = []
        for field, addrs in grouped_recipients:
            for addr in addrs:
                recipients_flat.append({"field": field, "email": addr})
        filter_tags = []
        if followup:
            filter_tags.append("followup")
        # "Read" (opened) means a confirmed human-likely open. A proxy/preload-
        # only row is neither Read nor Unread — it lives under the Proxy filter
        # so it isn't miscounted as read OR hidden as never-touched.
        if quality["human_likely_opens"] > 0:
            filter_tags.append("opened")
        elif quality["proxy_or_preload_opens"] == 0:
            filter_tags.append("unopened")
        if link_clicks > 0:
            filter_tags.append("clicked")
        if quality["human_likely_opens"] > 0:
            filter_tags.append("human")
        if quality["proxy_or_preload_opens"] > 0:
            filter_tags.append("proxy")
        if quality["human_likely_opens"] > 0:
            stats["opened"] += 1
        if followup:
            stats["needs_followup"] += 1
        if link_clicks > 0:
            stats["clicked"] += 1
        if quality["human_likely_opens"] > 0:
            stats["human_likely_opened"] += 1
        elif quality["proxy_or_preload_opens"] > 0:
            stats["proxy_or_preload_only"] += 1
        search_blob = " ".join(
            [email.subject or "", email.recipient_email or ""]
            + [r["email"] for r in recipients_flat]
            + [r["field"] for r in recipients_flat]
        ).lower()
        emails.append({
            "id": email.id,
            "subject": email.subject,
            "recipient_email": email.recipient_email,
            "recipients": grouped_recipients,
            "recipients_flat": recipients_flat,
            "created_at": email.created_at,
            "total_opens": total_opens,
            "verified_opens": verified_opens,
            "last_opened": last_opened,
            "link_clicks": link_clicks,
            "needs_followup": followup,
            "filter_tags": " ".join(filter_tags),
            "human_likely_opens": quality["human_likely_opens"],
            "proxy_or_preload_opens": quality["proxy_or_preload_opens"],
            "open_quality_label": _open_quality_label(
                quality["human_likely_opens"], quality["proxy_or_preload_opens"]
            ),
            "recipient_capture_badge": _recipient_capture_badge(email.all_recipients, grouped_recipients),
            "search_blob": search_blob,
        })
        stats["total"] += 1

    for idx, email in enumerate(emails):
        email["is_latest"] = idx == 0
        email["followup_draft"] = _followup_draft(email)

    return emails, stats


def _serialize_dashboard_row(e: dict) -> dict:
    """JSON-safe row for /dashboard/data (pre-formatted display strings)."""
    created = e["created_at"]
    last_opened = e["last_opened"]
    opens_display = "—"
    if e["total_opens"] > 0:
        opens_display = str(e["total_opens"])
        if last_opened:
            opens_display += f" · {_time_ago(last_opened)}"

    status = "unread"
    if e["human_likely_opens"] > 0:
        status = "read"
    elif e["proxy_or_preload_opens"] > 0:
        status = "preloaded"
    elif e["needs_followup"]:
        status = "followup"

    recip_flat = []
    for field, addrs in e["recipients"]:
        for addr in addrs:
            recip_flat.append({"field": field, "email": addr})

    return {
        "id": e["id"],
        "subject": e["subject"] or "(no subject)",
        "recipients": recip_flat,
        "sent_ago": _time_ago(created) if created else "—",
        "sent_title": created.strftime("%b %d, %Y %H:%M") if created else "",
        "opens": opens_display,
        "clicks": str(e["link_clicks"]) if e["link_clicks"] > 0 else "—",
        "status": status,
        "has_clicks": e["link_clicks"] > 0,
        "needs_followup": e["needs_followup"],
        "filter_tags": e["filter_tags"],
        "human_likely_opens": e["human_likely_opens"],
        "proxy_or_preload_opens": e["proxy_or_preload_opens"],
        "open_quality_label": e["open_quality_label"],
        "recipient_capture_badge": e["recipient_capture_badge"],
        "is_latest": e.get("is_latest", False),
        "followup_draft": e.get("followup_draft", ""),
        "search_blob": e.get("search_blob", ""),
    }


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
    # Clears the session cookie set by Clerk during sign-in on /login.
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
    emails, stats = await _load_dashboard_data(user, db)
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "emails": emails, "stats": stats, "user": user,
    })


@router.get("/dashboard/data")
async def dashboard_data(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    emails, stats = await _load_dashboard_data(user, db)
    return JSONResponse({
        "stats": stats,
        "emails": [_serialize_dashboard_row(e) for e in emails],
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
    human_likely_opens = sum(
        1 for o in external_opens if o.verified and not _is_proxy_user_agent(o.user_agent)
    )
    proxy_or_preload_opens = len(external_opens) - human_likely_opens

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
    recipients = _grouped_recipients(email.all_recipients, email.recipient_email)

    timeline = []
    for o in opens:
        if o.internal:
            quality = "Self (excluded)"
        elif o.verified and not _is_proxy_user_agent(o.user_agent):
            quality = "Human likely"
        else:
            quality = "Proxy/preload"
        timeline.append({
            "ts": o.opened_at,
            "kind": "open",
            "quality": quality,
            "label": "Email opened",
            "meta": o.ip or "No IP",
        })
    for link in links:
        for c in link["clicks"]:
            timeline.append({
                "ts": c.clicked_at,
                "kind": "click",
                "quality": "Human likely" if c.verified else "Unverified",
                "label": f"Link click ({link['link_type']})",
                "meta": link["original_url"],
            })
    timeline.sort(key=lambda item: item["ts"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    return templates.TemplateResponse("email_detail.html", {
        "request": request,
        "user": user,
        "email": email,
        "recipients": recipients,
        "opens": opens,
        "external_opens": external_opens,
        "verified_opens": verified_opens,
        "human_likely_opens": human_likely_opens,
        "proxy_or_preload_opens": proxy_or_preload_opens,
        "open_quality_label": _open_quality_label(human_likely_opens, proxy_or_preload_opens),
        "links": links,
        "total_clicks": total_clicks,
        "timeline": timeline,
        "recipient_capture_badge": _recipient_capture_badge(email.all_recipients, recipients),
    })


@router.get("/dashboard/reports.csv")
async def reports_csv(
    days: int = 7,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    days = min(max(days, 1), 90)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    report = await build_report(db, user.email, since)
    rows = report.get("senders", [])

    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow([
        "sender_email",
        "emails_sent",
        "emails_opened",
        "opens",
        "verified_opens",
        "unique_recipients_opened",
        "link_clicks",
        "since_utc",
    ])
    since_label = since.strftime("%Y-%m-%d %H:%M:%S")
    for row in rows:
        writer.writerow([
            row["sender_email"],
            row["emails_sent"],
            row["emails_opened"],
            row["opens"],
            row["verified_opens"],
            row["unique_recipients_opened"],
            row["link_clicks"],
            since_label,
        ])

    filename = f"recon-weekly-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
