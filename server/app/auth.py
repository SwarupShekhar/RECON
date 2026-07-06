import hashlib
import os
import time
from datetime import datetime, timezone

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .database import async_session, get_db
from .models import ApiKey, Email, User

# ---------------------------------------------------------------------------
# JWKS cache (module-level singleton)
# ---------------------------------------------------------------------------
_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0
_JWKS_CACHE_TTL = 3600  # 1 hour


async def warm_jwks_cache() -> None:
    """Pre-load JWKS on startup so the first dashboard request isn't blocked."""
    try:
        await _fetch_jwks()
    except Exception:
        pass


async def _fetch_jwks() -> dict:
    global _jwks_cache, _jwks_fetched_at
    now = time.time()
    if _jwks_cache and (now - _jwks_fetched_at) < _JWKS_CACHE_TTL:
        return _jwks_cache

    jwks_url = os.environ.get("CLERK_JWKS_URL", "").strip()
    secret = os.environ.get("CLERK_SECRET_KEY", "").strip()
    if not jwks_url and not secret:
        raise HTTPException(status_code=500, detail="CLERK_JWKS_URL or CLERK_SECRET_KEY required")

    # Try Clerk Backend API first (works even when custom Frontend API TLS is misconfigured).
    attempts: list[tuple[str, dict[str, str]]] = []
    if secret:
        attempts.append(("https://api.clerk.com/v1/jwks", {"Authorization": f"Bearer {secret}"}))
    if jwks_url:
        attempts.append((jwks_url, {}))

    last_exc: Exception | None = None
    for url, headers in attempts:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                _jwks_cache = resp.json()
                _jwks_fetched_at = now
                return _jwks_cache
        except Exception as exc:
            last_exc = exc
            continue

    if _jwks_cache:
        return _jwks_cache
    raise HTTPException(
        status_code=503,
        detail=f"Could not fetch Clerk JWKS: {last_exc}",
    ) from last_exc


def _get_signing_key(jwks: dict, kid: str):
    for key in jwks.get("keys", []):
        if key["kid"] == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key)
    raise HTTPException(status_code=401, detail="Unable to find matching signing key")


async def _fetch_clerk_email(clerk_user_id: str) -> str:
    """Clerk's default session JWT carries no `email` claim (only `sub`, the
    user id). Rather than require the user to hand-add a custom claim in the
    Clerk Dashboard, fetch the primary email from Clerk's Backend API using
    the server-side secret key. Returns "" if it can't be resolved."""
    secret = os.environ.get("CLERK_SECRET_KEY", "").strip()
    if not secret or not clerk_user_id:
        return ""

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.clerk.com/v1/users/{clerk_user_id}",
                headers={"Authorization": f"Bearer {secret}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return ""

    addresses = data.get("email_addresses") or []
    if not addresses:
        return ""
    primary_id = data.get("primary_email_address_id")
    for addr in addresses:
        if addr.get("id") == primary_id:
            return addr.get("email_address", "") or ""
    return addresses[0].get("email_address", "") or ""


def verify_clerk_session(token: str) -> dict:
    """Verify a Clerk-issued session JWT. Returns decoded claims or raises."""
    try:
        unverified = jwt.get_unverified_header(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Clerk session token")

    jwks = _jwks_cache
    if not jwks:
        raise HTTPException(status_code=401, detail="JWKS not loaded")

    key = _get_signing_key(jwks, unverified["kid"])
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            options={"verify_exp": True},
        )
        return claims
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Clerk session expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid Clerk session: {exc}")


# ---------------------------------------------------------------------------
# Dashboard auth dependency (Clerk session cookie)
# ---------------------------------------------------------------------------
async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Dashboard auth dependency. Reads the Clerk session token from the
    __session cookie or Authorization header, verifies it, loads/creates User."""
    session_token = request.cookies.get("__session", "")
    if not session_token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            session_token = auth_header[7:]

    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    await _fetch_jwks()

    claims = verify_clerk_session(session_token)
    clerk_user_id = claims.get("sub", "")
    email = claims.get("email", "")
    if not email and claims.get("email_addresses"):
        email = claims["email_addresses"][0].get("email_address", "")

    if not clerk_user_id:
        raise HTTPException(status_code=401, detail="Invalid Clerk session claims")

    result = await db.execute(select(User).where(User.id == clerk_user_id))
    user = result.scalar_one_or_none()

    if not user:
        if not email:
            # Token carries no email claim (Clerk's default) — resolve it
            # server-side via the Backend API using the user id in `sub`.
            email = await _fetch_clerk_email(clerk_user_id)
        if not email:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Could not resolve your email from Clerk. The session token has no "
                    "'email' claim and the Backend API lookup returned nothing. Check that "
                    "CLERK_SECRET_KEY is set correctly on the server."
                ),
            )
        result = await db.execute(select(User).where(User.email == email))
        existing = result.scalar_one_or_none()
        if existing:
            user = existing
        else:
            user = User(id=clerk_user_id, email=email)
            db.add(user)
        # Backfill existing emails matching this email address
        backfill_result = await db.execute(
            select(Email.id).where(Email.sender_email == email, Email.user_id.is_(None))
        )
        email_ids = [row[0] for row in backfill_result.all()]
        if email_ids:
            await db.execute(
                update(Email).where(Email.id.in_(email_ids)).values(user_id=user.id)
            )
        await db.commit()
        await db.refresh(user)

    return user


# ---------------------------------------------------------------------------
# API key auth dependency (for extension / API calls)
# ---------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _user_from_api_key(x_api_key: str, db: AsyncSession) -> User | None:
    token_hash = _hash_token(x_api_key)
    result = await db.execute(
        select(ApiKey).where(ApiKey.token_hash == token_hash, ApiKey.revoked == False)
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        return None

    api_key.last_used_at = datetime.now(timezone.utc)
    await db.commit()

    user_result = await db.execute(select(User).where(User.id == api_key.user_id))
    return user_result.scalar_one_or_none()


async def get_current_user_from_api_key(
    x_api_key: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extension/API auth dependency. Hashes x_api_key, looks up a
    non-revoked ApiKey row, returns the owning User."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    user = await _user_from_api_key(x_api_key, db)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid, revoked, or orphaned API key")

    return user


# ---------------------------------------------------------------------------
# Legacy auth gate (kept for backward compat during Phase A)
# ---------------------------------------------------------------------------
def require_api_key(x_api_key: str | None = Header(None)) -> None:
    """Optional, off-by-default auth gate. If API_KEY is unset/empty in the
    environment, this is a no-op. If API_KEY is set, X-API-Key must match."""
    expected = os.environ.get("API_KEY", "").strip()
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


async def resolve_sender_email(
    x_api_key: str | None = Header(None),
    sender_email: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Extension-compatibility dependency for the read endpoints the
    extension already calls (`/status`, `/status/sent`, `/reports/*`,
    `/debug/emails`). Prefers a real API key (Phase B, per-user); falls back
    to the pre-auth trust model (global API_KEY gate + explicit sender_email
    query param) so the extension — which has no API key yet — keeps
    working until Phase B ships. Remove this fallback once the extension is
    cut over (plan's Phase D)."""
    if x_api_key:
        user = await _user_from_api_key(x_api_key, db)
        if user:
            return user.email
        # Per-user key was sent but didn't match — don't fall through to the
        # legacy sender_email query param (that produced a confusing plain-text 401).
        expected = os.environ.get("API_KEY", "").strip()
        if expected and x_api_key == expected and sender_email:
            return sender_email
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    require_api_key(x_api_key)  # no-op unless API_KEY is configured in the env
    if not sender_email:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header or sender_email query param",
        )
    return sender_email
