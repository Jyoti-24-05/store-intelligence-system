"""GET /stores/{store_id}/heatmap — zone traffic heatmap for last 24 hours.

Normalises visit_count to 0–100 (zone with max visits = 100).
Returns all zones from store_layout.json even if visit_count = 0.
data_confidence = False when fewer than 20 sessions in the window.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy import func, select

from app.database import Event as EventRow, get_db
from app.models import StoreHeatmap, ZoneHeatmapEntry
from app.time_range import store_metrics_window

router = APIRouter(tags=["heatmap"])

# Path to store_layout.json — overridable via env var
DEFAULT_LAYOUT_PATH = Path(__file__).resolve().parents[1] / "data" / "store_layout.json"
STORE_LAYOUT_PATH = os.getenv("STORE_LAYOUT", str(DEFAULT_LAYOUT_PATH))


def _parse_zone_ids(raw_zones) -> list[str]:
    if raw_zones is None:
        return []
    if isinstance(raw_zones, dict):
        return [zone_id for zone_id, value in raw_zones.items() if isinstance(zone_id, str)]
    if isinstance(raw_zones, list):
        ids = []
        for zone in raw_zones:
            if isinstance(zone, dict):
                ids.append(zone.get("zone_id") or zone.get("id") or next(iter(zone.keys()), None))
            elif isinstance(zone, str):
                ids.append(zone)
        return [zone_id for zone_id in ids if zone_id]
    return []


def _load_zone_ids() -> list[str]:
    """Return zone IDs from store_layout.json, empty list if file missing."""
    try:
        layout_path = Path(STORE_LAYOUT_PATH)
        if not layout_path.exists():
            return []
        data = json.loads(layout_path.read_text())
        if isinstance(data, dict):
            if "zones" in data:
                return _parse_zone_ids(data["zones"])
            if "cameras" in data:
                zone_ids: list[str] = []
                for camera in data["cameras"].values():
                    if isinstance(camera, dict):
                        zone_ids += _parse_zone_ids(camera.get("zones"))
                return zone_ids
            return _parse_zone_ids(data)
        return _parse_zone_ids(data)
    except Exception:
        pass
    return []


@router.get("/stores/{store_id}/heatmap", response_model=StoreHeatmap)
async def get_heatmap(store_id: str) -> StoreHeatmap:
    layout_zones = _load_zone_ids()

    async with get_db() as db:
        # ── Use event-anchored window, not wall-clock ─────────────────────
        window_start, now = await store_metrics_window(db, store_id)

        # ── Zone visit counts + dwell sums ────────────────────────────────
        zone_result = await db.execute(
            select(
                EventRow.zone_id,
                func.count(EventRow.event_id).label("visit_count"),
                func.avg(EventRow.dwell_ms).label("avg_dwell_ms"),
            )
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "ZONE_ENTER",
                EventRow.zone_id    != None,
                EventRow.timestamp  >= window_start,
                EventRow.timestamp  <= now,
            )
            .group_by(EventRow.zone_id)
        )
        db_zones = {
            row.zone_id: {"visit_count": row.visit_count, "avg_dwell_ms": row.avg_dwell_ms or 0.0}
            for row in zone_result.all()
        }

        # ── Session count for data_confidence check ───────────────────────
        session_result = await db.execute(
            select(func.count(func.distinct(EventRow.visitor_id)))
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "ENTRY",
                EventRow.is_staff   == False,
                EventRow.timestamp  >= window_start,
                EventRow.timestamp  <= now,
            )
        )
        session_count: int = session_result.scalar() or 0

    # ── Merge DB results with layout zones ────────────────────────────────
    all_zone_ids = list(dict.fromkeys(list(db_zones.keys()) + layout_zones))
    max_visits   = max((db_zones[z]["visit_count"] for z in db_zones), default=1)

    zones = []
    for zone_id in all_zone_ids:
        data        = db_zones.get(zone_id, {"visit_count": 0, "avg_dwell_ms": 0.0})
        visit_count = data["visit_count"]
        norm_score  = round((visit_count / max_visits) * 100, 2) if max_visits > 0 else 0.0
        zones.append(ZoneHeatmapEntry(
            zone_id          = zone_id,
            normalised_score = norm_score,
            avg_dwell_ms     = round(data["avg_dwell_ms"], 2),
            visit_count      = visit_count,
        ))

    zones.sort(key=lambda z: z.normalised_score, reverse=True)

    return StoreHeatmap(
        store_id        = store_id,
        as_of           = now,
        zones           = zones,
        data_confidence = session_count >= 20,
    )