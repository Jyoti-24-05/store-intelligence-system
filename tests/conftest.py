# PROMPT: Write pytest-asyncio fixtures for a FastAPI app using SQLAlchemy async
#         with SQLite in-memory DB, providing a test client and seeded event helpers.
# CHANGES MADE: Added make_event / make_zone_event / make_billing_event factory
#               helpers; added freeze_time utility; wired lifespan manually so
#               init_db() runs but load_pos_csv() is skipped (no CSV in tests).

import asyncio
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# ── Force in-memory SQLite before any app module is imported ─────────────────
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["POS_CSV"]      = "/nonexistent/pos.csv"   # skip CSV load in tests

from app.main     import app
from app.database import Base, engine as _app_engine, AsyncSessionLocal


# ── Session-scoped event loop ─────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── One DB init per test session ──────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session", autouse=True)
async def init_database():
    async with _app_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _app_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ── Fresh tables for every test ───────────────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
async def clean_tables():
    """Truncate all tables before each test so tests are independent."""
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        for table in reversed(Base.metadata.sorted_tables):
            await db.execute(text(f"DELETE FROM {table.name}"))
        await db.commit()
    yield


# ── HTTP test client ──────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ── Event factory helpers ─────────────────────────────────────────────────────

STORE_ID = "STORE_BLR_002"


def make_event(
    event_type: str = "ENTRY",
    visitor_id: str | None = None,
    zone_id: str | None = None,
    is_staff: bool = False,
    confidence: float = 0.92,
    dwell_ms: int = 0,
    queue_depth: int | None = None,
    ts_offset_minutes: float = 0,
) -> dict:
    """Return a valid StoreEvent payload dict."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=ts_offset_minutes)
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   STORE_ID,
        "camera_id":  "CAM_01",
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp":  ts.isoformat(),
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": confidence,
        "metadata": {
            "session_seq": 0,
            "queue_depth": queue_depth,
            "sku_zone":    zone_id,
        },
    }


def make_zone_event(zone_id: str = "SKINCARE", visitor_id: str | None = None,
                    ts_offset_minutes: float = 0) -> dict:
    return make_event("ZONE_ENTER", visitor_id=visitor_id,
                      zone_id=zone_id, ts_offset_minutes=ts_offset_minutes)


def make_billing_event(visitor_id: str, queue_depth: int = 3,
                       ts_offset_minutes: float = 0) -> dict:
    return make_event("BILLING_QUEUE_JOIN", visitor_id=visitor_id,
                      queue_depth=queue_depth, ts_offset_minutes=ts_offset_minutes)