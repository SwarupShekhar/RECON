import os

from fastapi import Header, HTTPException


def require_api_key(x_api_key: str | None = Header(None)) -> None:
    """Optional, off-by-default auth gate. If API_KEY is unset/empty in the
    environment, this is a no-op (dev-friendly default, fully backward
    compatible). If API_KEY is set, the X-API-Key header must match it."""
    expected = os.environ.get("API_KEY", "").strip()
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
