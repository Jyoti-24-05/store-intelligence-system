"""app/time_range.py — time window helper for all store metric queries.

Uses the LATEST event timestamp for the store as the anchor date, not
server-local "today". This is critical for replaying historical CCTV clips
(e.g. footage from April 10) — without this fix every metric query returns
zero because "today" (June) has no events.
"""
from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import select, func
from app.database import Event as EventRow


async def store_metrics_window(db, store_id: str):
    """Return (range_start, range_end) spanning the calendar day of the
    store's most recent event.

    Falls back to today UTC when no events exist yet (fresh DB).
    """
    result = await db.execute(
        select(func.max(EventRow.timestamp))
        .where(EventRow.store_id == store_id)
    )
    latest: datetime | None = result.scalar()

    if latest is None:
        now = datetime.now(timezone.utc)
    else:
        now = latest.replace(tzinfo=timezone.utc) if latest.tzinfo is None else latest

    range_start = now.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    range_end   = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    return range_start, range_end