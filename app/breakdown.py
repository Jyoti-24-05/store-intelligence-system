from fastapi import APIRouter
from sqlalchemy import func, select
from app.database import Event as EventRow, get_db
from app.time_range import store_metrics_window

router = APIRouter(tags=["breakdown"])

@router.get("/stores/{store_id}/events/breakdown")
async def get_breakdown(store_id: str):
    async with get_db() as db:
        start, end = await store_metrics_window(db, store_id)
        rows = await db.execute(
            select(
                EventRow.event_type,
                EventRow.is_staff,
                func.count(EventRow.event_id).label("cnt"),
            )
            .where(
                EventRow.store_id  == store_id,
                EventRow.timestamp >= start,
                EventRow.timestamp <= end,
            )
            .group_by(EventRow.event_type, EventRow.is_staff)
        )
        by_type = {}
        customer_events = 0
        staff_events = 0
        for row in rows.all():
            by_type[row.event_type] = by_type.get(row.event_type, 0) + row.cnt
            if row.is_staff:
                staff_events += row.cnt
            else:
                customer_events += row.cnt
        total = customer_events + staff_events
        return {
            "store_id": store_id,
            "total": total,
            "customer_events": customer_events,
            "staff_events": staff_events,
            "by_type": by_type,
        }