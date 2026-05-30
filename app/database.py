"""Async SQLAlchemy database layer.

Tables
------
events            — every StoreEvent ingested from the pipeline
visitor_sessions  — one row per visit session (ENTRY → EXIT)
pos_transactions  — loaded from pos_transactions.csv; updated by correlator
anomaly_log       — anomalies detected and upserted by anomalies.py

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
    """Persisted StoreEvent — one row per pipeline event."""
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

    # EventMetadata fields (flattened for query performance)
    queue_depth = Column(Integer,     nullable=True)
    sku_zone    = Column(String(64),  nullable=True)
    session_seq = Column(Integer,     nullable=False, default=0)

    # Full event JSON for audit / replay
    raw_json    = Column(Text,        nullable=False)

    # Composite index for the most common query pattern: store + time range
    __table_args__ = (
        Index("ix_events_store_timestamp", "store_id", "timestamp"),
        Index("ix_events_store_type",      "store_id", "event_type"),
    )


# ─────────────────────────────────────────────
# Table: visitor_sessions
# ─────────────────────────────────────────────

class VisitorSession(Base):
    """One row per visit session.

    A session begins on ENTRY and ends on EXIT. REENTRY increments
    reentry_count on the existing session rather than creating a new one.
    """
    __tablename__ = "visitor_sessions"

    session_id    = Column(String(64),             primary_key=True, nullable=False)
    visitor_id    = Column(String(64),             nullable=False, index=True)
    store_id      = Column(String(64),             nullable=False, index=True)
    entry_time    = Column(DateTime(timezone=True), nullable=False)
    exit_time     = Column(DateTime(timezone=True), nullable=True)   # null until EXIT
    is_staff      = Column(Boolean,                nullable=False, default=False)
    converted     = Column(Boolean,                nullable=False, default=False)
    reentry_count = Column(Integer,                nullable=False, default=0)
    last_zone     = Column(String(64),             nullable=True)

    __table_args__ = (
        Index("ix_sessions_store_entry", "store_id", "entry_time"),
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