"""
pipeline/emit.py
----------------
Event builder and batch emitter.
Writes to JSONL file and/or POSTs to API in batches of 100.

Multi-store changes
-------------------
build_event() now accepts optional ST1076 enrichment fields:
  zone_meta      — dict from ZoneMapper.get_zone_meta() carrying
                   zone_name, zone_type, is_revenue_zone
  zone_hotspot_x/y — pixel centroid from the detection box
  gender_pred, age_pred, age_bucket, is_face_hidden
  group_id, group_size
  queue_timing   — QueueTiming nested block for billing events

All new params are keyword-only and default to None so all
existing call-sites (Store 1) continue to work without changes.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import httpx

from app.models import StoreEvent, EventType, EventMetadata, QueueTiming


def build_event(
    store_id:    str,
    camera_id:   str,
    visitor_id:  str,
    event_type:  str,
    timestamp:   datetime,
    zone_id,
    dwell_ms:    int,
    is_staff:    bool,
    confidence:  float,
    session_seq: int,
    # ── Core metadata ──────────────────────────────────────────────────────
    queue_depth: int = None,
    sku_zone:    str = None,
    # ── Zone enrichment (ST1076) ───────────────────────────────────────────
    zone_meta:       dict  = None,   # from ZoneMapper.get_zone_meta()
    zone_hotspot_x:  float = None,   # detection centroid pixel X
    zone_hotspot_y:  float = None,   # detection centroid pixel Y
    # ── Visitor demographics (ST1076) ──────────────────────────────────────
    gender_pred:     str   = None,   # "M" | "F"
    age_pred:        int   = None,
    age_bucket:      str   = None,   # e.g. "25-34"
    is_face_hidden:  bool  = None,
    # ── Group entry (ST1076) ───────────────────────────────────────────────
    group_id:        str   = None,
    group_size:      int   = None,
    # ── Queue timing (ST1076 billing events) ──────────────────────────────
    queue_timing:    Optional[QueueTiming] = None,
) -> StoreEvent:
    """Build a validated StoreEvent ready for emission.

    zone_meta is expected to be the dict returned by
    ZoneMapper.get_zone_meta(zone_id).  When provided, zone_name,
    zone_type, and is_revenue_zone are extracted from it and stored
    in EventMetadata.  Falls back gracefully when zone_meta is None
    or missing keys (Store 1 / unknown zones).
    """
    zm = zone_meta or {}

    return StoreEvent(
        event_id   = uuid.uuid4(),
        store_id   = store_id,
        camera_id  = camera_id,
        visitor_id = visitor_id,
        event_type = EventType(event_type),
        timestamp  = timestamp,
        zone_id    = zone_id,
        dwell_ms   = dwell_ms,
        is_staff   = is_staff,
        confidence = round(min(max(confidence, 0.0), 1.0), 4),
        metadata   = EventMetadata(
            # ── Core ─────────────────────────────────────────────────────
            queue_depth     = queue_depth,
            sku_zone        = sku_zone or zm.get("zone_name") or zone_id,
            session_seq     = session_seq,
            # ── Zone enrichment ──────────────────────────────────────────
            zone_name       = zm.get("zone_name"),
            zone_type       = zm.get("zone_type"),
            is_revenue_zone = zm.get("is_revenue_zone"),
            zone_hotspot_x  = zone_hotspot_x,
            zone_hotspot_y  = zone_hotspot_y,
            # ── Demographics ─────────────────────────────────────────────
            gender_pred     = gender_pred,
            age_pred        = age_pred,
            age_bucket      = age_bucket,
            is_face_hidden  = is_face_hidden,
            # ── Group ────────────────────────────────────────────────────
            group_id        = group_id,
            group_size      = group_size,
            # ── Queue timing ─────────────────────────────────────────────
            queue_timing    = queue_timing,
        ),
    )


class EventEmitter:
    BATCH_SIZE = 100

    def __init__(self, output_path: str = None, api_url: str = None,
                 store_id: str = "", camera_id: str = ""):
        self.output_path = output_path
        self.api_url     = api_url
        self.store_id    = store_id
        self.camera_id   = camera_id
        self.buffer: list[StoreEvent] = []
        self._file = open(output_path, "a") if output_path else None

    def emit(self, event: StoreEvent) -> None:
        if self._file:
            self._file.write(event.model_dump_json() + "\n")
            self._file.flush()
        if self.api_url:
            self.buffer.append(event)
            if len(self.buffer) >= self.BATCH_SIZE:
                self.flush()

    def flush(self) -> None:
        if self._file:
            self._file.flush()
        if not self.buffer or not self.api_url:
            return
        payload = [e.model_dump(mode="json") for e in self.buffer]
        try:
            resp = httpx.post(
                f"{self.api_url}/events/ingest",
                json={"events": payload},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            print(f"[Emitter] Flushed {data.get('ingested',0)} events "
                  f"({data.get('duplicate_skipped',0)} dupes)")
        except Exception as exc:
            print(f"[Emitter] WARNING: POST failed: {exc}")
        finally:
            self.buffer.clear()

    def close(self) -> None:
        self.flush()
        if self._file:
            self._file.close()