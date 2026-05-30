"""FastAPI application entry point.

Startup sequence
----------------
1. init_db()  — creates all tables (idempotent)
2. Routers registered for every endpoint group
3. SQLAlchemy OperationalError → HTTP 503 (structured JSON, never a stack trace)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import orjson
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.database import init_db, check_db


# ─────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run startup tasks before serving, cleanup on shutdown."""
    await init_db()
    yield
    # Nothing to tear down for SQLite; connection pool closes automatically


# ─────────────────────────────────────────────
# App instance
# ─────────────────────────────────────────────

app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    description="Real-time retail analytics from CCTV — Purplle challenge",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────
# Exception handlers
# ─────────────────────────────────────────────

@app.exception_handler(OperationalError)
async def db_operational_error_handler(request: Request, exc: OperationalError):
    """Return structured 503 instead of a raw SQLAlchemy stack trace."""
    return JSONResponse(
        status_code=503,
        content={
            "error": "database_unavailable",
            "detail": "The database is temporarily unavailable. Please retry shortly.",
        },
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all: return 500 with a safe message, never expose internals."""
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "detail": "An unexpected error occurred.",
        },
    )


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.get("/health", tags=["health"])
async def health():
    """Lightweight health probe used by Docker healthcheck.

    Full health details (per-store feed status) are at GET /health/detail.
    """
    db_ok = await check_db()
    return {
        "service": "store-intelligence-api",
        "status": "healthy" if db_ok else "degraded",
        "db_connected": db_ok,
    }


# Phase 4 routers registered here once implemented:
# from app.ingestion import router as ingest_router
# from app.metrics   import router as metrics_router
# from app.funnel    import router as funnel_router
# from app.heatmap   import router as heatmap_router
# from app.anomalies import router as anomalies_router
# from app.health    import router as health_router
# app.include_router(ingest_router)
# app.include_router(metrics_router)
# ...