"""Async SQLAlchemy database layer.

Tables
------
events            — every StoreEvent ingested from the pipeline
visitor_sessions  — one row per visit session (ENTRY → EXIT)
pos_transactions  — loaded from pos_transactions.csv; updated by correlator
anomaly_log       — anomalies detected and upserted by anomalies.py

Multi-store columns
-------------------
The events table carries optional enrichment columns added for ST1076
(Store 2, Mumbai).  All new columns are nullable so existing ST1008 rows
remain valid without them:

  Visitor demographics  : gender_pred, age_pred, age_bucket, is_face_hidden
  Group entry           : group_id, group_size
  Zone enrichment       : zone_name, zone_type, is_revenue_zone,
                          zone_hotspot_x, zone_hotspot_y
  Queue timing (billing): queue_join_ts, queue_served_ts, queue_exit_ts,
                          wait_seconds, queue_position_at_join, queue_abandoned

  visitor_sessions also gains gender_pred + age_bucket for demographic
  aggregation at the session level (populated from the ENTRY event).

Migration note
--------------
If you have an existing DB from Store 1 only, run migrate_db() once after
deploying this version.  It issues ALTER TABLE … ADD COLUMN IF NOT EXISTS
for each new column, which is a no-op on fresh databases and safe on SQLite
(SQLite ignores IF NOT EXISTS but the try/except in migrate_db handles that).

Usage
-----
    async with get_db() as db:
        result = await db.execute(select(Event))

SQLAlchemy OperationalError is caught at the FastAPI exception-handler level
(registered in app/main.py) and returned as HTTP 503 with a structured JSON body.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator

import orjson
from sqlalchemy import (
    Boolean, Column, DateTime, Float, Index,
    Integer, String, Text, UniqueConstraint,
    event as sa_event, text,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# ─────────────────────────────────────────────
# Engine setup
# ─────────────────────────────────────────────

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./db/store_intelligence.db",
)

# pool_pre_ping=True re-validates connections before use — prevents stale-connection
# 500s after a DB restart. echo=False keeps logs clean in production.
engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    echo=False,
    # SQLite-specific: WAL mode gives concurrent readers + one writer
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)

# Enable WAL mode for SQLite (no-op on Postgres)
@sa_event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_conn, _connection_record):
    if "sqlite" in DATABASE_URL:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,   # keep ORM objects usable after commit
    autoflush=False,
    autocommit=False,
)


# ─────────────────────────────────────────────
# ORM Base
# ─────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────
# Table: events
# ─────────────────────────────────────────────

class Event(Base):
    """Persisted StoreEvent — one row per pipeline event.

    Columns marked '# ST1076' are nullable enrichment fields introduced for
    Store 2.  They are always None for ST1008 events.
    """
    __tablename__ = "events"

    # Primary key — UUID v4 stored as TEXT (SQLite has no native UUID type)
    event_id    = Column(String(36), primary_key=True, nullable=False)

    # Core identification
    store_id    = Column(String(64),  nullable=False, index=True)
    camera_id   = Column(String(64),  nullable=False)
    visitor_id  = Column(String(64),  nullable=False, index=True)
    event_type  = Column(String(32),  nullable=False, index=True)

    # Timing
    timestamp   = Column(DateTime(timezone=True), nullable=False)

    # Zone / dwell
    zone_id     = Column(String(64),  nullable=True)
    dwell_ms    = Column(Integer,     nullable=False, default=0)

    # Classification
    is_staff    = Column(Boolean,     nullable=False, default=False)
    confidence  = Column(Float,       nullable=False)

    # ── EventMetadata core fields (flattened for query performance) ──────────
    queue_depth = Column(Integer,     nullable=True)
    sku_zone    = Column(String(64),  nullable=True)
    session_seq = Column(Integer,     nullable=False, default=0)

    # ── ST1076: visitor demographics ─────────────────────────────────────────
    gender_pred    = Column(String(4),   nullable=True)   # "M" | "F"
    age_pred       = Column(Integer,     nullable=True)   # predicted age in years
    age_bucket     = Column(String(16),  nullable=True)   # e.g. "25-34"
    is_face_hidden = Column(Boolean,     nullable=True)   # True when face obscured

    # ── ST1076: group entry ───────────────────────────────────────────────────
    group_id       = Column(String(32),  nullable=True)   # shared group token e.g. "G_10"
    group_size     = Column(Integer,     nullable=True)   # total group headcount

    # ── ST1076: zone enrichment ───────────────────────────────────────────────
    zone_name       = Column(String(128), nullable=True)  # e.g. "Left Shelf"
    zone_type       = Column(String(32),  nullable=True)  # e.g. "SHELF" | "DISPLAY"
    is_revenue_zone = Column(Boolean,     nullable=True)  # True if revenue zone
    zone_hotspot_x  = Column(Float,       nullable=True)  # centroid pixel X
    zone_hotspot_y  = Column(Float,       nullable=True)  # centroid pixel Y

    # ── ST1076: queue timing (billing events only) ────────────────────────────
    queue_join_ts           = Column(DateTime(timezone=True), nullable=True)
    queue_served_ts         = Column(DateTime(timezone=True), nullable=True)
    queue_exit_ts           = Column(DateTime(timezone=True), nullable=True)
    wait_seconds            = Column(Integer, nullable=True)
    queue_position_at_join  = Column(Integer, nullable=True)
    queue_abandoned         = Column(Boolean, nullable=True)  # True = BILLING_QUEUE_ABANDON

    # Full event JSON for audit / replay
    raw_json    = Column(Text, nullable=False)

    # Composite indexes for the most common query patterns
    __table_args__ = (
        Index("ix_events_store_timestamp", "store_id", "timestamp"),
        Index("ix_events_store_type",      "store_id", "event_type"),
        # ST1076: enables demographic breakdowns per store without full scan
        Index("ix_events_store_age_bucket", "store_id", "age_bucket"),
        # ST1076: enables group-entry queries (count events with same group_id)
        Index("ix_events_group_id",         "group_id"),
    )


# ─────────────────────────────────────────────
# Table: visitor_sessions
# ─────────────────────────────────────────────

class VisitorSession(Base):
    """One row per visit session.

    A session begins on ENTRY and ends on EXIT. REENTRY increments
    reentry_count on the existing session rather than creating a new one.

    ST1076 additions
    ----------------
    gender_pred  — carried from the ENTRY event's metadata
    age_bucket   — carried from the ENTRY event's metadata
    group_id     — populated if the visitor entered as part of a group
    """
    __tablename__ = "visitor_sessions"

    session_id    = Column(String(64),              primary_key=True, nullable=False)
    visitor_id    = Column(String(64),              nullable=False, index=True)
    store_id      = Column(String(64),              nullable=False, index=True)
    entry_time    = Column(DateTime(timezone=True), nullable=False)
    exit_time     = Column(DateTime(timezone=True), nullable=True)   # null until EXIT
    is_staff      = Column(Boolean,                 nullable=False, default=False)
    converted     = Column(Boolean,                 nullable=False, default=False)
    reentry_count = Column(Integer,                 nullable=False, default=0)
    last_zone     = Column(String(64),              nullable=True)

    # ── ST1076: session-level demographics (from ENTRY event) ─────────────────
    gender_pred   = Column(String(4),  nullable=True)   # "M" | "F"
    age_bucket    = Column(String(16), nullable=True)   # e.g. "25-34"
    group_id      = Column(String(32), nullable=True)   # group token if part of a group

    __table_args__ = (
        Index("ix_sessions_store_entry", "store_id", "entry_time"),
        # ST1076: demographic breakdown queries on sessions
        Index("ix_sessions_store_gender", "store_id", "gender_pred"),
    )


# ─────────────────────────────────────────────
# Table: pos_transactions
# ─────────────────────────────────────────────

class POSTransaction(Base):
    """POS transaction loaded from pos_transactions.csv.

    matched_visitor_id is set by pos_correlator once a visitor session
    is correlated with this transaction.
    """
    __tablename__ = "pos_transactions"

    transaction_id      = Column(String(64),              primary_key=True, nullable=False)
    store_id            = Column(String(64),              nullable=False, index=True)
    timestamp           = Column(DateTime(timezone=True), nullable=False, index=True)
    basket_value_inr    = Column(Float,                   nullable=False)
    matched_visitor_id  = Column(String(64),              nullable=True)   # null = unmatched


# ─────────────────────────────────────────────
# Table: anomaly_log
# ─────────────────────────────────────────────

class AnomalyLog(Base):
    """Detected anomalies — upserted by anomalies.detect_and_upsert()."""
    __tablename__ = "anomaly_log"

    anomaly_id       = Column(String(64),              primary_key=True, nullable=False)
    anomaly_type     = Column(String(64),              nullable=False, index=True)
    severity         = Column(String(16),              nullable=False)
    store_id         = Column(String(64),              nullable=False, index=True)
    detected_at      = Column(DateTime(timezone=True), nullable=False)
    resolved_at      = Column(DateTime(timezone=True), nullable=True)    # null = unresolved
    description      = Column(Text,                    nullable=False)
    suggested_action = Column(Text,                    nullable=False)

    __table_args__ = (
        Index("ix_anomaly_store_detected", "store_id", "detected_at"),
        # One active anomaly of each type per store at a time
        UniqueConstraint("store_id", "anomaly_type", "resolved_at",
                         name="uq_anomaly_store_type_active"),
    )


# ─────────────────────────────────────────────
# DB initialisation
# ─────────────────────────────────────────────

async def init_db() -> None:
    """Create all tables if they do not exist.

    Called once at application startup (lifespan handler in main.py).
    Safe to call repeatedly — uses CREATE TABLE IF NOT EXISTS semantics.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def migrate_db() -> None:
    """Add new ST1076 columns to an existing Store 1–only database.

    Safe to call on a fresh database (columns already exist → silently ignored).
    Safe to call multiple times (idempotent).

    New columns are all nullable, so existing ST1008 rows are unaffected.
    Called automatically by init_db flow in main.py after create_all.
    """
    # All ALTER TABLE statements needed to upgrade a Store-1-only schema.
    # SQLite does not support IF NOT EXISTS on ALTER TABLE, so we catch
    # OperationalError ("duplicate column name") and continue.
    new_event_columns = [
        "ALTER TABLE events ADD COLUMN gender_pred      TEXT",
        "ALTER TABLE events ADD COLUMN age_pred         INTEGER",
        "ALTER TABLE events ADD COLUMN age_bucket       TEXT",
        "ALTER TABLE events ADD COLUMN is_face_hidden   BOOLEAN",
        "ALTER TABLE events ADD COLUMN group_id         TEXT",
        "ALTER TABLE events ADD COLUMN group_size       INTEGER",
        "ALTER TABLE events ADD COLUMN zone_name        TEXT",
        "ALTER TABLE events ADD COLUMN zone_type        TEXT",
        "ALTER TABLE events ADD COLUMN is_revenue_zone  BOOLEAN",
        "ALTER TABLE events ADD COLUMN zone_hotspot_x   REAL",
        "ALTER TABLE events ADD COLUMN zone_hotspot_y   REAL",
        "ALTER TABLE events ADD COLUMN queue_join_ts    DATETIME",
        "ALTER TABLE events ADD COLUMN queue_served_ts  DATETIME",
        "ALTER TABLE events ADD COLUMN queue_exit_ts    DATETIME",
        "ALTER TABLE events ADD COLUMN wait_seconds     INTEGER",
        "ALTER TABLE events ADD COLUMN queue_position_at_join INTEGER",
        "ALTER TABLE events ADD COLUMN queue_abandoned  BOOLEAN",
    ]
    new_session_columns = [
        "ALTER TABLE visitor_sessions ADD COLUMN gender_pred  TEXT",
        "ALTER TABLE visitor_sessions ADD COLUMN age_bucket   TEXT",
        "ALTER TABLE visitor_sessions ADD COLUMN group_id     TEXT",
    ]

    async with engine.begin() as conn:
        for stmt in new_event_columns + new_session_columns:
            try:
                await conn.execute(text(stmt))
            except Exception:
                # Column already exists — safe to ignore
                pass


async def check_db() -> bool:
    """Return True if DB is reachable, False otherwise.

    Used by the /health endpoint — never raises.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────
# Session dependency
# ─────────────────────────────────────────────

@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that yields a database session.

    Commits on clean exit, rolls back on any exception, always closes.

    Example
    -------
        async with get_db() as db:
            db.add(event_row)
            # commit happens automatically on __aexit__

    SQLAlchemy OperationalError propagates up to the FastAPI
    exception handler registered in main.py, which returns HTTP 503.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()