"""GET /stores/{store_id}/funnel — session-based conversion funnel.

Session is the unit. Re-entries do NOT create new sessions.
drop_off_pct for stage N = (stage[N-1].count - stage[N].count) / stage[N-1].count * 100
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import func, select

from app.database import Event as EventRow, POSTransaction, get_db
from app.models import StoreFunnel, FunnelStage
from app.time_range import store_metrics_window

router = APIRouter(tags=["funnel"])


@router.get("/stores/{store_id}/funnel", response_model=StoreFunnel)
async def get_funnel(store_id: str) -> StoreFunnel:
    async with get_db() as db:
        range_start, range_end = await store_metrics_window(db, store_id)

        # ── Stage 1: sessions with at least one ENTRY today ───────────────
        entry_sessions_q = (
            select(func.distinct(EventRow.visitor_id))
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "ENTRY",
                EventRow.is_staff   == False,
                EventRow.timestamp  >= range_start,
                EventRow.timestamp  <= range_end,
            )
        )
        entry_result = await db.execute(
            select(func.count()).select_from(entry_sessions_q.subquery())
        )
        stage_entry: int = entry_result.scalar() or 0
        entry_sessions_subq = entry_sessions_q.subquery()

        stage_zone: int = zone_result.scalar() or 0

        # ── Stage 3: sessions with at least one BILLING_QUEUE_JOIN today and an ENTRY session ─
        billing_sessions_q = (
            select(func.distinct(EventRow.visitor_id))
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "BILLING_QUEUE_JOIN",
                EventRow.is_staff   == False,
                EventRow.timestamp  >= range_start,
                EventRow.timestamp  <= range_end,
                EventRow.visitor_id.in_(entry_sessions_subq),
            )
        )
        billing_result = await db.execute(
            select(func.count()).select_from(billing_sessions_q.subquery())
        )
        stage_billing: int = billing_result.scalar() or 0

        # ── Stage 4: sessions with a correlated POS transaction today and an ENTRY session ─
        purchase_result = await db.execute(
            select(func.count(func.distinct(POSTransaction.matched_visitor_id)))
            .where(
                POSTransaction.store_id           == store_id,
                POSTransaction.matched_visitor_id != None,
                POSTransaction.matched_visitor_id.in_(entry_sessions_subq),
                POSTransaction.timestamp          >= range_start,
                POSTransaction.timestamp          <= range_end,
            )
        )
        stage_purchase: int = purchase_result.scalar() or 0

    counts = [stage_entry, stage_zone, stage_billing, stage_purchase]
    labels = ["entry", "zone_visit", "billing_queue", "purchase"]

    def drop_off(prev: int, curr: int) -> float:
        if prev == 0:
            return 0.0
        return round((prev - curr) / prev * 100, 2)

    stages = []
    for i, (label, count) in enumerate(zip(labels, counts)):
        prev = counts[i - 1] if i > 0 else count
        stages.append(FunnelStage(
            stage=label,
            count=count,
            drop_off_pct=0.0 if i == 0 else drop_off(prev, count),
        ))

    return StoreFunnel(
        store_id      = store_id,
        as_of         = range_end,
        stages        = stages,
        session_count = stage_entry,   # unique sessions = unique ENTRY visitors
    )