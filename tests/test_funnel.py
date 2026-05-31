# PROMPT: Write pytest-asyncio tests for GET /stores/{store_id}/funnel covering
#         empty store zeros, full funnel progression, drop-off percentage accuracy,
#         re-entry deduplication, and staff exclusion from session count.
# CHANGES MADE: Used conftest helpers; seeded POS transactions directly into DB
#               for purchase stage (no CSV in test env); verified exact drop_off_pct
#               arithmetic; added staff exclusion test.

import uuid
import pytest
from datetime import datetime, timezone

from tests.conftest import make_event, make_zone_event, make_billing_event, STORE_ID


pytestmark = pytest.mark.asyncio


# ── 1. Empty store → all stages present with count=0 ─────────────────────────

async def test_funnel_empty_store(client):
    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    assert resp.status_code == 200
    body = resp.json()
    stages = {s["stage"]: s for s in body["stages"]}
    assert set(stages.keys()) == {"entry", "zone_visit", "billing_queue", "purchase"}, \
        f"Expected 4 stages, got {list(stages.keys())}"
    for stage in stages.values():
        assert stage["count"] == 0,        f"Empty store should have count=0: {stage}"
        assert stage["drop_off_pct"] == 0.0, f"Empty store drop_off should be 0.0: {stage}"
    assert body["session_count"] == 0


# ── 2. Full funnel progression ────────────────────────────────────────────────

async def test_funnel_full_progression(client):
    """10 entry → 8 zone → 5 billing → 3 purchase."""
    from app.database import get_db, POSTransaction

    visitor_ids = [f"VIS_full_{i}" for i in range(10)]

    events = []
    # All 10 enter
    for vid in visitor_ids:
        events.append(make_event("ENTRY", visitor_id=vid))
    # 8 visit a zone
    for vid in visitor_ids[:8]:
        events.append(make_zone_event("SKINCARE", visitor_id=vid))
    # 5 join billing queue
    for vid in visitor_ids[:5]:
        events.append(make_billing_event(vid, queue_depth=2))

    await client.post("/events/ingest", json={"events": events})

    # 3 purchase (seed POS directly)
    async with get_db() as db:
        for vid in visitor_ids[:3]:
            db.add(POSTransaction(
                transaction_id=f"TXN_full_{vid}", store_id=STORE_ID,
                timestamp=datetime.now(timezone.utc),
                basket_value_inr=500.0,
                matched_visitor_id=vid,
            ))
        await db.commit()

    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    assert resp.status_code == 200
    stages = {s["stage"]: s for s in resp.json()["stages"]}

    assert stages["entry"]["count"]         == 10
    assert stages["zone_visit"]["count"]    == 8
    assert stages["billing_queue"]["count"] == 5
    assert stages["purchase"]["count"]      == 3


# ── 3. Drop-off percentage accuracy ──────────────────────────────────────────

async def test_funnel_dropoff_accuracy(client):
    """10 entry → 5 zone → drop_off for zone_visit = (10-5)/10*100 = 50.0%"""
    events = (
        [make_event("ENTRY", visitor_id=f"VIS_drop_{i}") for i in range(10)] +
        [make_zone_event("LIPSTICK", visitor_id=f"VIS_drop_{i}") for i in range(5)]
    )
    await client.post("/events/ingest", json={"events": events})

    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    stages = {s["stage"]: s for s in resp.json()["stages"]}

    assert stages["entry"]["drop_off_pct"] == 0.0, \
        "First stage drop_off must always be 0.0"
    assert abs(stages["zone_visit"]["drop_off_pct"] - 50.0) < 0.1, \
        f"Expected 50% drop-off, got {stages['zone_visit']['drop_off_pct']}"


# ── 4. Re-entry does not double-count sessions ────────────────────────────────

async def test_funnel_reentry_no_double_count(client):
    vid = "VIS_reentry_funnel"
    events = [
        make_event("ENTRY",   visitor_id=vid),
        make_event("EXIT",    visitor_id=vid),
        make_event("REENTRY", visitor_id=vid),
    ]
    await client.post("/events/ingest", json={"events": events})

    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    body = resp.json()
    assert body["session_count"] == 1, \
        f"Re-entry should count as 1 session, got {body['session_count']}"
    entry_stage = next(s for s in body["stages"] if s["stage"] == "entry")
    assert entry_stage["count"] == 1, \
        f"Re-entry must not inflate entry count, got {entry_stage['count']}"


# ── 5. Staff excluded from session count ─────────────────────────────────────

async def test_funnel_staff_excluded(client):
    events = (
        [make_event("ENTRY", visitor_id=f"VIS_cust_{i}") for i in range(4)] +
        [make_event("ENTRY", visitor_id=f"VIS_staff_{i}", is_staff=True) for i in range(6)]
    )
    await client.post("/events/ingest", json={"events": events})

    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    body = resp.json()
    assert body["session_count"] == 4, \
        f"Staff must be excluded from session_count, got {body['session_count']}"
    entry_stage = next(s for s in body["stages"] if s["stage"] == "entry")
    assert entry_stage["count"] == 4, \
        f"Staff must be excluded from entry count, got {entry_stage['count']}"