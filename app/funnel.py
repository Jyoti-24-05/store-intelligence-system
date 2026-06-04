"""GET /stores/{store_id}/funnel — session-based conversion funnel.

Cross-camera visitor_id note
-----------------------------
The detection pipeline processes each camera clip independently. ByteTrack
assigns fresh integer track IDs per clip, so the same physical person gets a
different VIS_xxxxxx on CAM_ENTRY_1 vs CAM_ZONE. Re-ID bridging only works
within a single camera's 30-minute gallery window.

Mid-session footage handling
-----------------------------
When footage starts after the store is open (customer already inside, entry
camera never triggered), stage_entry = 0 from ENTRY events. In this case
we fall back to counting distinct ZONE_ENTER visitor_ids as the entry proxy —
the same logic used by metrics.py for unique_visitors consistency.

Stage independence (cross-camera)
----------------------------------
Stages 1→2 and 2→3 are NOT monotonically capped because they come from
different cameras with different visitor ID spaces. Each stage shows
"unique visitors observed at this stage" independently. The cap is only
applied at stage 3→4 (same billing context).
"""
from __future__ import annotations

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

        # ── Stage 1: entry ────────────────────────────────────────────────
        # Primary: distinct visitor_ids with ENTRY events
        entry_result = await db.execute(
            select(func.count(func.distinct(EventRow.visitor_id)))
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "ENTRY",
                EventRow.is_staff   == False,
                EventRow.timestamp  >= range_start,
                EventRow.timestamp  <= range_end,
            )
        )
        stage_entry: int = entry_result.scalar() or 0

        # Fallback: footage starts mid-session → use zone visitors as proxy
        using_zone_as_entry = False
        if stage_entry == 0:
            zone_entry_result = await db.execute(
                select(func.count(func.distinct(EventRow.visitor_id)))
                .where(
                    EventRow.store_id   == store_id,
                    EventRow.event_type == "ZONE_ENTER",
                    EventRow.is_staff   == False,
                    EventRow.timestamp  >= range_start,
                    EventRow.timestamp  <= range_end,
                )
            )
            zone_as_entry: int = zone_entry_result.scalar() or 0
            if zone_as_entry > 0:
                stage_entry         = zone_as_entry
                using_zone_as_entry = True

        # ── Stage 2: zone_visit ───────────────────────────────────────────
        # When zone visitors were used as entry proxy, stage_zone = same
        # number (avoids double-counting the same visitor pool).
        # For ST1076 where entry and zone are separate cameras, count independently.
        if using_zone_as_entry:
            stage_zone = stage_entry
        else:
            zone_result = await db.execute(
                select(func.count(func.distinct(EventRow.visitor_id)))
                .where(
                    EventRow.store_id   == store_id,
                    EventRow.event_type == "ZONE_ENTER",
                    EventRow.is_staff   == False,
                    EventRow.timestamp  >= range_start,
                    EventRow.timestamp  <= range_end,
                )
            )
            stage_zone = zone_result.scalar() or 0

        # ── Stage 3: billing_queue ────────────────────────────────────────
        billing_result = await db.execute(
            select(func.count(func.distinct(EventRow.visitor_id)))
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "BILLING_QUEUE_JOIN",
                EventRow.is_staff   == False,
                EventRow.timestamp  >= range_start,
                EventRow.timestamp  <= range_end,
            )
        )
        stage_billing: int = billing_result.scalar() or 0

        # ── Stage 4: purchase (correlated POS) ────────────────────────────
        purchase_result = await db.execute(
            select(func.count(func.distinct(POSTransaction.matched_visitor_id)))
            .where(
                POSTransaction.store_id           == store_id,
                POSTransaction.matched_visitor_id != None,
                POSTransaction.timestamp          >= range_start,
                POSTransaction.timestamp          <= range_end,
            )
        )
        stage_purchase: int = purchase_result.scalar() or 0

        # For mid-session stores: use total POS transaction count as purchase
        # stage when no visitor correlation exists but transactions do.
        if stage_purchase == 0 and using_zone_as_entry:
            total_pos_result = await db.execute(
                select(func.count(POSTransaction.transaction_id))
                .where(
                    POSTransaction.store_id  == store_id,
                    POSTransaction.timestamp >= range_start,
                    POSTransaction.timestamp <= range_end,
                )
            )
            stage_purchase = total_pos_result.scalar() or 0

        # Cap purchase at max of billing or entry (cross-camera safe)
        max_prior = max(stage_billing, stage_entry)
        if max_prior > 0:
            stage_purchase = min(stage_purchase, max_prior)

    session_count = max(stage_entry, stage_zone, stage_billing, stage_purchase, 0)

    counts = [stage_entry, stage_zone, stage_billing, stage_purchase]
    labels = ["entry", "zone_visit", "billing_queue", "purchase"]

    def drop_off(prev: int, curr: int) -> float:
        if prev == 0:
            return 0.0
        return round((prev - curr) / prev * 100, 2)

    stages = [
        FunnelStage(
            stage        = label,
            count        = count,
            drop_off_pct = 0.0 if i == 0 else drop_off(counts[i - 1], count),
        )
        for i, (label, count) in enumerate(zip(labels, counts))
    ]

    return StoreFunnel(
        store_id      = store_id,
        as_of         = range_end,
        stages        = stages,
        session_count = session_count,
    )