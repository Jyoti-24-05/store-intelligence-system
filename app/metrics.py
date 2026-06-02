"""GET /stores/{store_id}/metrics — live store metrics from DB.

All queries are live (no caching). Returns zero/empty values when a store
has no traffic — never 500 or null fields.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import func, select

from app.database import Event as EventRow, POSTransaction, get_db
from app.models import StoreMetrics, ZoneDwellMetric
from app.time_range import store_metrics_window

router = APIRouter(tags=["metrics"])


@router.get("/stores/{store_id}/metrics", response_model=StoreMetrics)
async def get_metrics(store_id: str) -> StoreMetrics:
    async with get_db() as db:
        range_start, range_end = await store_metrics_window(db, store_id)

        # ── unique_visitors (ENTRY events, non-staff, today) ──────────────
        uv_result = await db.execute(
            select(func.count(func.distinct(EventRow.visitor_id)))
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "ENTRY",
                EventRow.is_staff   == False,
                EventRow.timestamp  >= range_start,
                EventRow.timestamp  <= range_end,
            )
        )
        unique_visitors: int = uv_result.scalar() or 0

        # ── conversion_rate ────────────────────────────────────────────────
        # Count visitor_ids that have a matched POS transaction today
        conv_result = await db.execute(
            select(func.count(func.distinct(POSTransaction.matched_visitor_id)))
            .where(
                POSTransaction.store_id           == store_id,
                POSTransaction.matched_visitor_id != None,
                POSTransaction.timestamp          >= range_start,
                POSTransaction.timestamp          <= range_end,
            )
        )
        converted_count: int = conv_result.scalar() or 0
        conversion_rate = (converted_count / unique_visitors) if unique_visitors > 0 else 0.0

        # ── avg_dwell_per_zone (ZONE_DWELL events, today) ─────────────────
        dwell_result = await db.execute(
            select(
                EventRow.zone_id,
                func.avg(EventRow.dwell_ms).label("avg_dwell_ms"),
                func.count(EventRow.event_id).label("visit_count"),
            )
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "ZONE_DWELL",
                EventRow.zone_id    != None,
                EventRow.timestamp  >= range_start,
                EventRow.timestamp  <= range_end,
            )
            .group_by(EventRow.zone_id)
        )
        avg_dwell_per_zone = [
            ZoneDwellMetric(
                zone_id=row.zone_id,
                avg_dwell_ms=round(row.avg_dwell_ms, 2),
                visit_count=row.visit_count,
            )
            for row in dwell_result.all()
        ]

        # ── current_queue_depth (latest BILLING_QUEUE_JOIN queue_depth) ───
        queue_result = await db.execute(
            select(EventRow.queue_depth)
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "BILLING_QUEUE_JOIN",
                EventRow.queue_depth != None,
            )
            .order_by(EventRow.timestamp.desc())
            .limit(1)
        )
        current_queue_depth: int = queue_result.scalar() or 0

        # ── abandonment_rate (ABANDON / JOIN, today) ───────────────────────
        join_result = await db.execute(
            select(func.count(EventRow.event_id))
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "BILLING_QUEUE_JOIN",
                EventRow.timestamp  >= range_start,
                EventRow.timestamp  <= range_end,
            )
        )
        abandon_result = await db.execute(
            select(func.count(EventRow.event_id))
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "BILLING_QUEUE_ABANDON",
                EventRow.timestamp  >= range_start,
                EventRow.timestamp  <= range_end,
            )
        )
        total_joins:   int = join_result.scalar() or 0
        total_abandons: int = abandon_result.scalar() or 0
        abandonment_rate = (total_abandons / total_joins) if total_joins > 0 else 0.0

    return StoreMetrics(
        store_id            = store_id,
        as_of               = range_end,
        unique_visitors     = unique_visitors,
        conversion_rate     = round(conversion_rate, 4),
        avg_dwell_per_zone  = avg_dwell_per_zone,
        current_queue_depth = current_queue_depth,
        abandonment_rate    = round(abandonment_rate, 4),
    )