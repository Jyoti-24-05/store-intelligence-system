"""GET /stores/{store_id}/funnel — session-based conversion funnel.

Session is the unit. Re-entries do NOT create new sessions.
drop_off_pct for stage N = (stage[N-1].count - stage[N].count) / stage[N-1].count * 100
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import func, select, distinct

from app.database import Event as EventRow, POSTransaction, VisitorSession, get_db
from app.models import StoreFunnel, FunnelStage

router = APIRouter(tags=["funnel"])


def _today_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@router.get("/stores/{store_id}/funnel", response_model=StoreFunnel)
async def get_funnel(store_id: str) -> StoreFunnel:
    now         = datetime.now(timezone.utc)
    today_start = _today_start()

    async with get_db() as db:

        # ── Stage 1: sessions with at least one ENTRY today ───────────────
        entry_sessions_q = (
            select(func.distinct(EventRow.visitor_id))
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "ENTRY",
                EventRow.is_staff   == False,
                EventRow.timestamp  >= today_start,
                EventRow.timestamp  <= now,
            )
        )
        entry_result = await db.execute(
            select(func.count()).select_from(entry_sessions_q.subquery())
        )
        stage_entry: int = entry_result.scalar() or 0

        # ── Stage 2: sessions with at least one ZONE_ENTER today ──────────
        zone_sessions_q = (
            select(func.distinct(EventRow.visitor_id))
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "ZONE_ENTER",
                EventRow.is_staff   == False,
                EventRow.timestamp  >= today_start,
                EventRow.timestamp  <= now,
            )
        )
        zone_result = await db.execute(
            select(func.count()).select_from(zone_sessions_q.subquery())
        )
        stage_zone: int = zone_result.scalar() or 0

        # ── Stage 3: sessions with at least one BILLING_QUEUE_JOIN today ──
        billing_sessions_q = (
            select(func.distinct(EventRow.visitor_id))
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "BILLING_QUEUE_JOIN",
                EventRow.is_staff   == False,
                EventRow.timestamp  >= today_start,
                EventRow.timestamp  <= now,
            )
        )
        billing_result = await db.execute(
            select(func.count()).select_from(billing_sessions_q.subquery())
        )
        stage_billing: int = billing_result.scalar() or 0

        # ── Stage 4: sessions with a correlated POS transaction today ─────
        purchase_result = await db.execute(
            select(func.count(func.distinct(POSTransaction.matched_visitor_id)))
            .where(
                POSTransaction.store_id           == store_id,
                POSTransaction.matched_visitor_id != None,
                POSTransaction.timestamp          >= today_start,
                POSTransaction.timestamp          <= now,
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
        as_of         = now,
        stages        = stages,
        session_count = stage_entry,   # unique sessions = unique ENTRY visitors
    )