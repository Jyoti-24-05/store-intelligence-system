"""POST /events/ingest — batch event ingestion endpoint."""
from __future__ import annotations

import orjson
from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError

from app.database import Event as EventRow, get_db
from app.models import IngestRequest, IngestResponse, IngestError, StoreEvent

router = APIRouter(tags=["ingestion"])


@router.post("/events/ingest", response_model=IngestResponse)
async def ingest_events(payload: IngestRequest, request: Request) -> IngestResponse:
    ingested          = 0
    duplicate_skipped = 0
    failed            = 0
    errors: list[IngestError] = []

    from app import pos_correlator, anomalies

    async with get_db() as db:
        for event in payload.events:
            try:
                existing = await db.get(EventRow, str(event.event_id))
                if existing is not None:
                    duplicate_skipped += 1
                    continue

                row = _to_row(event)
                db.add(row)
                await db.flush()
                ingested += 1

            except IntegrityError:
                await db.rollback()
                duplicate_skipped += 1

            except Exception as exc:
                await db.rollback()
                failed += 1
                errors.append(IngestError(
                    event_id=str(event.event_id),
                    reason=str(exc),
                ))

        await db.commit()

    # Expose event_count to the logging middleware
    request.state.event_count = ingested

    try:
        await pos_correlator.correlate_recent()
    except Exception:
        pass

    try:
        await anomalies.detect_and_upsert()
    except Exception:
        pass

    return IngestResponse(
        ingested=ingested,
        duplicate_skipped=duplicate_skipped,
        failed=failed,
        errors=errors,
    )


def _to_row(event: StoreEvent) -> EventRow:
    """Map a StoreEvent Pydantic model to an Event ORM row.

    All ST1076 enrichment fields are Optional — they will be None for
    ST1008 events and the column will store NULL.
    """
    m  = event.metadata          # shorthand
    qt = m.queue_timing          # None for non-billing / ST1008 events

    return EventRow(
        # ── Core ────────────────────────────────────────────────────────────
        event_id    = str(event.event_id),
        store_id    = event.store_id,
        camera_id   = event.camera_id,
        visitor_id  = event.visitor_id,
        event_type  = event.event_type.value,
        timestamp   = event.timestamp,
        zone_id     = event.zone_id,
        dwell_ms    = event.dwell_ms,
        is_staff    = event.is_staff,
        confidence  = event.confidence,

        # ── EventMetadata core ───────────────────────────────────────────────
        queue_depth = m.queue_depth,
        sku_zone    = m.sku_zone,
        session_seq = m.session_seq,

        # ── ST1076: visitor demographics ─────────────────────────────────────
        gender_pred    = m.gender_pred,
        age_pred       = m.age_pred,
        age_bucket     = m.age_bucket,
        is_face_hidden = m.is_face_hidden,

        # ── ST1076: group entry ───────────────────────────────────────────────
        group_id       = m.group_id,
        group_size     = m.group_size,

        # ── ST1076: zone enrichment ───────────────────────────────────────────
        zone_name       = m.zone_name,
        zone_type       = m.zone_type,
        is_revenue_zone = m.is_revenue_zone,
        zone_hotspot_x  = m.zone_hotspot_x,
        zone_hotspot_y  = m.zone_hotspot_y,

        # ── ST1076: queue timing (None for non-billing / ST1008 events) ───────
        queue_join_ts          = qt.queue_join_ts          if qt else None,
        queue_served_ts        = qt.queue_served_ts        if qt else None,
        queue_exit_ts          = qt.queue_exit_ts          if qt else None,
        wait_seconds           = qt.wait_seconds           if qt else None,
        queue_position_at_join = qt.queue_position_at_join if qt else None,
        queue_abandoned        = qt.abandoned              if qt else None,

        # ── Audit / replay ────────────────────────────────────────────────────
        raw_json    = orjson.dumps(event.model_dump(mode="json")).decode(),
    )