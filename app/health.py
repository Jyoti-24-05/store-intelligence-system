"""GET /health/detail — full health response with per-store feed status.

Rules
-----
- Always returns HTTP 200 (engineers must read the body even when degraded)
- DB connectivity checked via SELECT 1
- Any store with last_event > 10 min ago → status = "STALE_FEED"
- Overall status = "degraded" if DB down OR any store is STALE_FEED
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter
from sqlalchemy import func, select

from app.database import Event as EventRow, check_db, get_db
from app.models import HealthResponse, StoreHealth

router = APIRouter(tags=["health"])

STALE_THRESHOLD_MINUTES = 10


@router.get("/health/detail", response_model=HealthResponse)
async def health_detail() -> HealthResponse:
    db_connected = await check_db()
    stores: list[StoreHealth] = []

    if db_connected:
        now = datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(minutes=STALE_THRESHOLD_MINUTES)

        async with get_db() as db:
            # Latest event timestamp per store
            result = await db.execute(
                select(
                    EventRow.store_id,
                    func.max(EventRow.timestamp).label("last_event_at"),
                )
                .group_by(EventRow.store_id)
            )
            for row in result.all():
                last_event_at = row.last_event_at
                if last_event_at and last_event_at.tzinfo is None:
                    last_event_at = last_event_at.replace(tzinfo=timezone.utc)
                status = (
                    "STALE_FEED"
                    if last_event_at is None or last_event_at < stale_cutoff
                    else "OK"
                )
                stores.append(StoreHealth(
                    store_id      = row.store_id,
                    last_event_at = last_event_at,
                    status        = status,
                ))

    any_stale = any(s.status == "STALE_FEED" for s in stores)
    overall   = "healthy" if (db_connected and not any_stale) else "degraded"

    return HealthResponse(
        service      = "store-intelligence-api",
        status       = overall,
        stores       = stores,
        db_connected = db_connected,
    )