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
    return EventRow(
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
        queue_depth = event.metadata.queue_depth,
        sku_zone    = event.metadata.sku_zone,
        session_seq = event.metadata.session_seq,
        raw_json    = orjson.dumps(event.model_dump(mode="json")).decode(),
    )