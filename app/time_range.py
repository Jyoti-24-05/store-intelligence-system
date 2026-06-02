# time_range.py — replace your current implementation with this

from __future__ import annotations
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from app.database import Event as EventRow


async def store_metrics_window(db, store_id: str):
    """
    Returns (range_start, range_end) based on the LATEST event date for the store.
    Falls back to today if no events exist.
    This prevents empty results when replaying historical/test data.
    """
    result = await db.execute(
        select(func.max(EventRow.timestamp))
        .where(EventRow.store_id == store_id)
    )
    latest: datetime | None = result.scalar()

    if latest is None:
        # No data at all — use today
        now = datetime.now(timezone.utc)
    else:
        now = latest.replace(tzinfo=timezone.utc) if latest.tzinfo is None else latest

    # Window = entire calendar day of the latest event
    range_start = now.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    range_end   = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    return range_start, range_end