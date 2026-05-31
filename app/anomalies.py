"""GET /stores/{store_id}/anomalies — detect and return active anomalies.

Three anomaly types (ALL implemented):

BILLING_QUEUE_SPIKE
  Trigger  : current queue_depth > 5
  Severity : WARN (6–9), CRITICAL (>=10)

CONVERSION_DROP
  Trigger  : today conversion_rate < 7-day avg * 0.7
  Severity : WARN (drop >30%), CRITICAL (drop >50%)
  Guard    : skip if insufficient historical data

DEAD_ZONE
  Trigger  : zone with historical traffic → 0 ZONE_ENTER in last 30 min
  Severity : INFO
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter
from sqlalchemy import func, select, and_, distinct

from app.database import Event as EventRow, AnomalyLog, POSTransaction, get_db
from app.models import (
    Anomaly, AnomalySeverity, AnomalyType, StoreAnomalies,
)

router = APIRouter(tags=["anomalies"])


# ─────────────────────────────────────────────
# Public endpoint
# ─────────────────────────────────────────────

@router.get("/stores/{store_id}/anomalies", response_model=StoreAnomalies)
async def get_anomalies(store_id: str) -> StoreAnomalies:
    """Detect fresh anomalies, upsert to anomaly_log, return active list."""
    await detect_and_upsert(store_id=store_id)

    now = datetime.now(timezone.utc)
    async with get_db() as db:
        result = await db.execute(
            select(AnomalyLog)
            .where(
                AnomalyLog.store_id   == store_id,
                AnomalyLog.resolved_at == None,  # only unresolved
            )
            .order_by(AnomalyLog.detected_at.desc())
        )
        rows = result.scalars().all()

    anomalies = [
        Anomaly(
            anomaly_id       = row.anomaly_id,
            anomaly_type     = AnomalyType(row.anomaly_type),
            severity         = AnomalySeverity(row.severity),
            store_id         = row.store_id,
            detected_at      = row.detected_at,
            description      = row.description,
            suggested_action = row.suggested_action,
        )
        for row in rows
    ]

    return StoreAnomalies(store_id=store_id, as_of=now, anomalies=anomalies)


# ─────────────────────────────────────────────
# Detection engine (called by ingestion.py too)
# ─────────────────────────────────────────────

async def detect_and_upsert(store_id: str | None = None) -> None:
    """Run all detectors and upsert results into anomaly_log.

    If store_id is None, runs across ALL stores in the events table.
    """
    async with get_db() as db:
        # Resolve store list
        if store_id:
            store_ids = [store_id]
        else:
            result = await db.execute(
                select(func.distinct(EventRow.store_id))
            )
            store_ids = [r[0] for r in result.all()]

        for sid in store_ids:
            detected = []
            detected += await _detect_queue_spike(db, sid)
            detected += await _detect_conversion_drop(db, sid)
            detected += await _detect_dead_zones(db, sid)

            for anomaly in detected:
                await _upsert_anomaly(db, anomaly)

        await db.commit()


# ─────────────────────────────────────────────
# Detector: BILLING_QUEUE_SPIKE
# ─────────────────────────────────────────────

async def _detect_queue_spike(db, store_id: str) -> list[AnomalyLog]:
    result = await db.execute(
        select(EventRow.queue_depth)
        .where(
            EventRow.store_id    == store_id,
            EventRow.event_type  == "BILLING_QUEUE_JOIN",
            EventRow.queue_depth != None,
        )
        .order_by(EventRow.timestamp.desc())
        .limit(1)
    )
    depth = result.scalar()
    if depth is None or depth <= 5:
        return []

    severity = AnomalySeverity.CRITICAL if depth >= 10 else AnomalySeverity.WARN
    return [_make_anomaly(
        store_id         = store_id,
        anomaly_type     = AnomalyType.BILLING_QUEUE_SPIKE,
        severity         = severity,
        description      = f"Billing queue depth is {depth} (threshold: 5).",
        suggested_action = "Open additional billing counter immediately",
    )]


# ─────────────────────────────────────────────
# Detector: CONVERSION_DROP
# ─────────────────────────────────────────────

async def _detect_conversion_drop(db, store_id: str) -> list[AnomalyLog]:
    now         = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago    = today_start - timedelta(days=7)

    # Today's conversion rate
    uv = await db.execute(
        select(func.count(func.distinct(EventRow.visitor_id)))
        .where(EventRow.store_id == store_id, EventRow.event_type == "ENTRY",
               EventRow.is_staff == False, EventRow.timestamp >= today_start)
    )
    unique_today = uv.scalar() or 0
    if unique_today == 0:
        return []

    cv = await db.execute(
        select(func.count(func.distinct(POSTransaction.matched_visitor_id)))
        .where(POSTransaction.store_id == store_id,
               POSTransaction.matched_visitor_id != None,
               POSTransaction.timestamp >= today_start)
    )
    converted_today = cv.scalar() or 0
    rate_today = converted_today / unique_today

    # 7-day average conversion rate (historical, exclude today)
    uv7 = await db.execute(
        select(func.count(func.distinct(EventRow.visitor_id)))
        .where(EventRow.store_id == store_id, EventRow.event_type == "ENTRY",
               EventRow.is_staff == False,
               EventRow.timestamp >= week_ago, EventRow.timestamp < today_start)
    )
    unique_7d = uv7.scalar() or 0
    if unique_7d < 20:            # insufficient historical data — skip
        return []

    cv7 = await db.execute(
        select(func.count(func.distinct(POSTransaction.matched_visitor_id)))
        .where(POSTransaction.store_id == store_id,
               POSTransaction.matched_visitor_id != None,
               POSTransaction.timestamp >= week_ago,
               POSTransaction.timestamp < today_start)
    )
    converted_7d = cv7.scalar() or 0
    rate_7d_avg  = converted_7d / unique_7d

    if rate_7d_avg == 0 or rate_today >= rate_7d_avg * 0.7:
        return []

    drop_pct = (rate_7d_avg - rate_today) / rate_7d_avg
    severity = AnomalySeverity.CRITICAL if drop_pct > 0.5 else AnomalySeverity.WARN
    return [_make_anomaly(
        store_id         = store_id,
        anomaly_type     = AnomalyType.CONVERSION_DROP,
        severity         = severity,
        description      = (
            f"Today's conversion rate {rate_today:.1%} is "
            f"{drop_pct:.0%} below the 7-day average {rate_7d_avg:.1%}."
        ),
        suggested_action = "Check staff availability and queue times. Review last 2h events.",
    )]


# ─────────────────────────────────────────────
# Detector: DEAD_ZONE
# ─────────────────────────────────────────────

async def _detect_dead_zones(db, store_id: str) -> list[AnomalyLog]:
    now          = datetime.now(timezone.utc)
    window_30min = now - timedelta(minutes=30)
    window_24h   = now - timedelta(hours=24)

    # Zones that had traffic in the last 24h (normal zones)
    active_result = await db.execute(
        select(func.distinct(EventRow.zone_id))
        .where(
            EventRow.store_id   == store_id,
            EventRow.event_type == "ZONE_ENTER",
            EventRow.zone_id    != None,
            EventRow.timestamp  >= window_24h,
        )
    )
    active_zones = {r[0] for r in active_result.all()}

    # Zones with at least one ZONE_ENTER in the last 30 min
    recent_result = await db.execute(
        select(func.distinct(EventRow.zone_id))
        .where(
            EventRow.store_id   == store_id,
            EventRow.event_type == "ZONE_ENTER",
            EventRow.zone_id    != None,
            EventRow.timestamp  >= window_30min,
        )
    )
    recently_active = {r[0] for r in recent_result.all()}

    dead_zones = active_zones - recently_active
    return [
        _make_anomaly(
            store_id         = store_id,
            anomaly_type     = AnomalyType.DEAD_ZONE,
            severity         = AnomalySeverity.INFO,
            description      = f"Zone '{z}' has had no traffic for 30+ minutes.",
            suggested_action = f"Check camera feed for zone {z}. Send staff to investigate.",
            zone_id          = z,
        )
        for z in sorted(dead_zones)
    ]


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_anomaly(
    store_id: str,
    anomaly_type: AnomalyType,
    severity: AnomalySeverity,
    description: str,
    suggested_action: str,
    zone_id: str | None = None,
) -> AnomalyLog:
    anomaly_id = f"{anomaly_type.value}_{store_id}"
    if zone_id:
        anomaly_id += f"_{zone_id}"
    return AnomalyLog(
        anomaly_id       = anomaly_id,
        anomaly_type     = anomaly_type.value,
        severity         = severity.value,
        store_id         = store_id,
        detected_at      = datetime.now(timezone.utc),
        resolved_at      = None,
        description      = description,
        suggested_action = suggested_action,
    )


async def _upsert_anomaly(db, anomaly: AnomalyLog) -> None:
    """Insert if new; update detected_at + severity if already active."""
    existing = await db.get(AnomalyLog, anomaly.anomaly_id)
    if existing is None:
        db.add(anomaly)
    else:
        # Refresh detection time and severity on re-trigger
        existing.detected_at = anomaly.detected_at
        existing.severity    = anomaly.severity
        existing.description = anomaly.description