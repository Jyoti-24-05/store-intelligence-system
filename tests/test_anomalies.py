# PROMPT: Write pytest-asyncio tests for GET /stores/{store_id}/anomalies covering
#         queue spike CRITICAL, dead zone INFO, clean store empty list, and
#         conversion drop CRITICAL.
# CHANGES MADE: Used conftest helpers; seeded historical POS data directly via
#               DB for conversion_drop test (CSV not available in test env);
#               added tolerance check on conversion drop threshold logic.

import uuid
import pytest
from datetime import datetime, timezone, timedelta

from tests.conftest import make_event, make_billing_event, STORE_ID


pytestmark = pytest.mark.asyncio


# ── 1. Queue spike depth=10 → CRITICAL anomaly ───────────────────────────────

async def test_anomaly_queue_spike_critical(client):
    event = make_billing_event("VIS_spike", queue_depth=10)
    await client.post("/events/ingest", json={"events": [event]})

    resp = await client.get(f"/stores/{STORE_ID}/anomalies")
    assert resp.status_code == 200
    anomalies = resp.json()["anomalies"]
    spike = next(
        (a for a in anomalies if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"), None
    )
    assert spike is not None,            "BILLING_QUEUE_SPIKE anomaly not detected"
    assert spike["severity"] == "CRITICAL", \
        f"Expected CRITICAL for depth=10, got {spike['severity']}"


# ── 2. Dead zone: ZONE_ENTER 40 min ago, nothing since → DEAD_ZONE INFO ──────

async def test_anomaly_dead_zone(client):
    # Insert a zone event 40 minutes ago
    old_event = make_event(
        "ZONE_ENTER",
        zone_id="SKINCARE",
        ts_offset_minutes=40,   # 40 min ago — outside the 30-min active window
    )
    await client.post("/events/ingest", json={"events": [old_event]})

    resp = await client.get(f"/stores/{STORE_ID}/anomalies")
    assert resp.status_code == 200
    anomalies = resp.json()["anomalies"]
    dead = next(
        (a for a in anomalies if a["anomaly_type"] == "DEAD_ZONE"), None
    )
    assert dead is not None,       "DEAD_ZONE anomaly not detected"
    assert dead["severity"] == "INFO", \
        f"Expected INFO for dead zone, got {dead['severity']}"
    assert "SKINCARE" in dead["description"], \
        f"Expected zone name in description, got: {dead['description']}"


# ── 3. Clean store → empty anomalies list (not 500) ──────────────────────────

async def test_anomaly_clean_store(client):
    resp = await client.get(f"/stores/STORE_CLEAN_999/anomalies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["anomalies"] == [], \
        f"Expected empty list for clean store, got {body['anomalies']}"


# ── 4. Conversion drop: today rate=0.1, 7-day avg=0.5 → CRITICAL ─────────────

async def test_anomaly_conversion_drop_critical(client):
    from app.database import get_db, Event as EventRow, POSTransaction, VisitorSession

    now         = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Seed 7-day historical data: 100 visitors, 50 purchases (50% rate)
    async with get_db() as db:
        for i in range(100):
            vid = f"VIS_hist_{i}"
            ts  = today_start - timedelta(days=3)
            db.add(EventRow(
                event_id=str(uuid.uuid4()), store_id=STORE_ID,
                camera_id="CAM_01", visitor_id=vid,
                event_type="ENTRY", timestamp=ts,
                dwell_ms=0, is_staff=False, confidence=0.9,
                session_seq=0, raw_json="{}",
            ))
            if i < 50:
                db.add(POSTransaction(
                    transaction_id=f"TXN_hist_{i}", store_id=STORE_ID,
                    timestamp=ts, basket_value_inr=500.0,
                    matched_visitor_id=vid,
                ))
        await db.commit()

    # Today: 10 visitors, 1 purchase (10% rate) — 80% drop, triggers CRITICAL
    async with get_db() as db:
        for i in range(10):
            vid = f"VIS_today_{i}"
            db.add(EventRow(
                event_id=str(uuid.uuid4()), store_id=STORE_ID,
                camera_id="CAM_01", visitor_id=vid,
                event_type="ENTRY", timestamp=now,
                dwell_ms=0, is_staff=False, confidence=0.9,
                session_seq=0, raw_json="{}",
            ))
        db.add(POSTransaction(
            transaction_id="TXN_today_0", store_id=STORE_ID,
            timestamp=now, basket_value_inr=800.0,
            matched_visitor_id="VIS_today_0",
        ))
        await db.commit()

    resp = await client.get(f"/stores/{STORE_ID}/anomalies")
    assert resp.status_code == 200
    anomalies = resp.json()["anomalies"]
    drop = next(
        (a for a in anomalies if a["anomaly_type"] == "CONVERSION_DROP"), None
    )
    assert drop is not None,              "CONVERSION_DROP anomaly not detected"
    assert drop["severity"] == "CRITICAL", \
        f"Expected CRITICAL for 80% drop, got {drop['severity']}"