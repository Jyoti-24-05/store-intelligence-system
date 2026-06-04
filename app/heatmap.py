"""GET /stores/{store_id}/heatmap — zone traffic heatmap for last 24 hours.

Normalises visit_count to 0–100 (zone with max visits = 100).
Returns only zones belonging to this store's layout — never bleeds zones
from another store's layout or from stale DB rows of a different store.
data_confidence = False when fewer than 20 sessions in the window.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy import func, select

from app.database import Event as EventRow, get_db
from app.models import StoreHeatmap, ZoneHeatmapEntry
from app.time_range import store_metrics_window

router = APIRouter(tags=["heatmap"])

# ── Layout file paths ─────────────────────────────────────────────────────────

STORE_LAYOUT_MAP: dict[str, str] = {
    "ST1008": os.getenv("STORE_LAYOUT",        "/app/data/store_layout.json"),
    "ST1076": os.getenv("STORE_LAYOUT_ST1076", "/app/data/store2_layout.json"),
}

# ── Hardcoded fallback zone lists (used when layout file is missing) ──────────
# These are the zones that *actually* appear in pipeline events for each store.
# They are used ONLY when the layout JSON cannot be read.

_FALLBACK_ZONES: dict[str, list[str]] = {
    "ST1008": [
        "MAKEUP_ZONE", "SKINCARE_ZONE", "BILLING_QUEUE", "BILLING_COUNTER",
        "HAIR_ZONE", "BATH_BODY_ZONE", "PERSONAL_CARE_ZONE", "FRAGRANCE_ZONE",
    ],
    "ST1076": [
        "PURPLLE_MUM_1076_Z01", "PURPLLE_MUM_1076_Z02", "PURPLLE_MUM_1076_Z03",
        "PURPLLE_MUM_1076_Z04", "PURPLLE_MUM_1076_Z05",
        "PURPLLE_MUM_1076_Z_BILLING_01", "PURPLLE_MUM_1076_Z_BILLING_COUNTER",
        # Intentionally exclude Z_ENTRY and Z_BOH — they are not revenue zones
        # and add noise to the heatmap (entry is always high, BOH is staff-only).
    ],
}


# ── Zone ID extraction from layout JSON ──────────────────────────────────────

# Zone types that should be excluded from the heatmap.
# Entry zones inflate scores trivially; staff/BOH zones are not customer-facing.
_EXCLUDE_ZONE_TYPES = {"entry", "entry_exit", "staff_area", "boh", "back_of_house"}


def _load_layout_zone_names(store_id: str) -> dict[str, str]:
    """Return a mapping of zone_id → zone_name from the store's layout JSON.

    Falls back to an empty dict if the layout is missing or unparseable —
    callers should use zone_id as the display label in that case.
    """
    layout_path = Path(STORE_LAYOUT_MAP.get(store_id, ""))
    if not layout_path or not layout_path.exists():
        return {}
    try:
        data = json.loads(layout_path.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict) or "cameras" not in data:
        return {}
    names: dict[str, str] = {}
    for camera in data["cameras"].values():
        if not isinstance(camera, dict):
            continue
        for zone_id, meta in camera.get("zones", {}).items():
            if isinstance(meta, dict) and "zone_name" in meta:
                names[zone_id] = meta["zone_name"]
    return names


def _load_layout_zone_ids(store_id: str) -> list[str]:
    """Return an ordered list of zone IDs for this store from its layout JSON.

    Excludes entry-threshold and staff-only zones so the heatmap shows only
    customer-facing, revenue-relevant zones.

    Falls back to _FALLBACK_ZONES[store_id] if the layout file is missing or
    cannot be parsed.
    """
    fallback = _FALLBACK_ZONES.get(store_id, [])
    layout_path = Path(STORE_LAYOUT_MAP.get(store_id, ""))

    if not layout_path or not layout_path.exists():
        return fallback

    try:
        data = json.loads(layout_path.read_text())
    except Exception:
        return fallback

    if not isinstance(data, dict) or "cameras" not in data:
        return fallback

    zone_ids: list[str] = []
    seen: set[str] = set()

    for camera in data["cameras"].values():
        if not isinstance(camera, dict):
            continue
        zones = camera.get("zones", {})
        if not isinstance(zones, dict):
            continue
        for zone_id, meta in zones.items():
            if zone_id in seen:
                continue
            if not isinstance(meta, dict):
                continue
            # Exclude by zone_type field (handles both "type" and "zone_type" keys)
            zone_type = (
                meta.get("zone_type") or meta.get("type") or ""
            ).lower().replace(" ", "_")
            if zone_type in _EXCLUDE_ZONE_TYPES:
                continue
            # Exclude by is_staff_zone flag
            if meta.get("is_staff_zone"):
                continue
            # Exclude entry zones by ID heuristic as a belt-and-braces check
            if "entry" in zone_id.lower() and "boh" not in zone_id.lower():
                # Allow zones that merely contain the word in a product sense
                # (e.g. "ENTRY_DISPLAY") by checking is_entry_camera on the camera
                if camera.get("is_entry_camera"):
                    continue
            seen.add(zone_id)
            zone_ids.append(zone_id)

    return zone_ids if zone_ids else fallback


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get("/stores/{store_id}/heatmap", response_model=StoreHeatmap)
async def get_heatmap(store_id: str) -> StoreHeatmap:
    # Authoritative zone list + human-readable names for this store
    layout_zones     = _load_layout_zone_ids(store_id)
    layout_zone_set  = set(layout_zones)
    layout_zone_names = _load_layout_zone_names(store_id)

    async with get_db() as db:
        window_start, now = await store_metrics_window(db, store_id)

        # ── Zone visit counts + avg dwell ─────────────────────────────────
        zone_result = await db.execute(
            select(
                EventRow.zone_id,
                func.count(EventRow.event_id).label("visit_count"),
                func.avg(EventRow.dwell_ms).label("avg_dwell_ms"),
            )
            .where(
                EventRow.store_id   == store_id,
                EventRow.event_type == "ZONE_ENTER",
                EventRow.is_staff   == False,          # exclude staff zone visits
                EventRow.zone_id    != None,
                EventRow.timestamp  >= window_start,
                EventRow.timestamp  <= now,
            )
            .group_by(EventRow.zone_id)
        )

        # Only keep DB rows whose zone_id belongs to THIS store's layout.
        # This is the fix for ST1008 showing ST1076 zone IDs — any zone in the
        # DB that isn't in layout_zone_set is silently discarded here.
        db_zones: dict[str, dict] = {}
        for row in zone_result.all():
            if row.zone_id in layout_zone_set:
                db_zones[row.zone_id] = {
                    "visit_count":  row.visit_count,
                    "avg_dwell_ms": row.avg_dwell_ms or 0.0,
                }

        # ── Session count for data_confidence ─────────────────────────────
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

    # ── Normalise and build response ──────────────────────────────────────
    # Use layout_zones (not db_zones keys) as the canonical ordered list so
    # the heatmap always shows all zones, not just zones with traffic.
    max_visits = max(
        (db_zones[z]["visit_count"] for z in db_zones),
        default=1,
    )

    zones: list[ZoneHeatmapEntry] = []
    for zone_id in layout_zones:
        data        = db_zones.get(zone_id, {"visit_count": 0, "avg_dwell_ms": 0.0})
        visit_count = data["visit_count"]
        norm_score  = round((visit_count / max_visits) * 100, 2) if max_visits > 0 else 0.0
        zones.append(ZoneHeatmapEntry(
            zone_id          = zone_id,
            zone_name        = layout_zone_names.get(zone_id),
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