"""
pipeline/emit.py
----------------
Event builder and batch emitter.
Writes to JSONL file and/or POSTs to API in batches of 100.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import httpx

from app.models import StoreEvent, EventType, EventMetadata


def build_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: datetime,
    zone_id,
    dwell_ms: int,
    is_staff: bool,
    confidence: float,
    session_seq: int,
    queue_depth: int = None,
    sku_zone: str = None,
) -> StoreEvent:
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
            queue_depth = queue_depth,
            sku_zone    = sku_zone or zone_id,
            session_seq = session_seq,
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