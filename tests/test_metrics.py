# PROMPT: Write pytest-asyncio tests for GET /stores/{store_id}/metrics covering
#         empty store zeros, zero purchases, staff-only events, and abandonment rate.
# CHANGES MADE: Used conftest helpers; added assertion messages; tested exact
#               float rounding (0.333 vs 0.334) for abandonment rate.

import pytest
from tests.conftest import make_event, make_billing_event, STORE_ID


pytestmark = pytest.mark.asyncio


# ── 1. Empty store returns zeros (not null, not 500) ─────────────────────────

async def test_metrics_empty_store(client):
    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unique_visitors"]     == 0,   f"Got {body}"
    assert body["conversion_rate"]     == 0.0, f"Got {body}"
    assert body["current_queue_depth"] == 0,   f"Got {body}"
    assert body["abandonment_rate"]    == 0.0, f"Got {body}"
    assert body["avg_dwell_per_zone"]  == [],  f"Got {body}"


# ── 2. Zero purchases → conversion_rate = 0.0 ────────────────────────────────

async def test_metrics_zero_conversion(client):
    events = [make_event("ENTRY") for _ in range(5)]
    await client.post("/events/ingest", json={"events": events})

    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    assert resp.status_code == 200
    assert resp.json()["conversion_rate"] == 0.0
    assert resp.json()["unique_visitors"] == 5


# ── 3. Staff-only events → unique_visitors = 0 ───────────────────────────────

async def test_metrics_staff_only(client):
    events = [make_event("ENTRY", is_staff=True) for _ in range(10)]
    await client.post("/events/ingest", json={"events": events})

    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    assert resp.status_code == 200
    assert resp.json()["unique_visitors"] == 0


# ── 4. Abandonment rate: 3 JOIN + 1 ABANDON → ~0.333 ────────────────────────

async def test_metrics_abandonment_rate(client):
    events = [
        make_billing_event(f"VIS_join_{i}", queue_depth=2) for i in range(3)
    ] + [
        make_event("BILLING_QUEUE_ABANDON", visitor_id="VIS_abandon_0"),
    ]
    await client.post("/events/ingest", json={"events": events})

    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    assert resp.status_code == 200
    rate = resp.json()["abandonment_rate"]
    assert abs(rate - 0.3333) < 0.001, \
        f"Expected ~0.333 abandonment rate, got {rate}"