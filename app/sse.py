"""GET /stores/{store_id}/stream — Server-Sent Events for real-time dashboard push.

Pushes a combined metrics+anomalies payload every 3 seconds.
React dashboard subscribes with EventSource API.

Event format
------------
    data: {"metrics": {...}, "anomalies": [...], "funnel": {...}}
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["stream"])


@router.get("/stores/{store_id}/stream")
async def store_stream(store_id: str):
    """SSE stream — one combined event every 3 seconds."""

    async def event_generator():
        while True:
            try:
                from app.metrics   import get_metrics
                from app.anomalies import get_anomalies
                from app.funnel    import get_funnel

                metrics   = await get_metrics(store_id)
                anomalies = await get_anomalies(store_id)
                funnel    = await get_funnel(store_id)

                payload = {
                    "metrics":   metrics.model_dump(mode="json"),
                    "anomalies": anomalies.model_dump(mode="json")["anomalies"],
                    "funnel":    funnel.model_dump(mode="json"),
                    "as_of":     datetime.now(timezone.utc).isoformat(),
                }
                yield f"data: {json.dumps(payload)}\n\n"

            except Exception as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"

            await asyncio.sleep(3)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",   # disable nginx buffering
            "Access-Control-Allow-Origin": "*",    # allow React dev server
        },
    )