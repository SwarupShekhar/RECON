from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_api_key
from ..database import get_db
from ..reports import build_report, render_report_html

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("/reports/weekly", response_class=HTMLResponse)
async def weekly_report(
    sender_email: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.now(timezone.utc) - timedelta(days=7)
    report_data = await build_report(db, sender_email, since)
    return HTMLResponse(content=render_report_html(report_data, "Weekly"))


@router.get("/reports/monthly", response_class=HTMLResponse)
async def monthly_report(
    sender_email: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.now(timezone.utc) - timedelta(days=30)
    report_data = await build_report(db, sender_email, since)
    return HTMLResponse(content=render_report_html(report_data, "Monthly"))
