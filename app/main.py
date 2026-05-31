"""FastAPI application entry point.

Phase 7: structured JSON logging middleware (structlog)
Phase 8: graceful degradation — OperationalError → 503, Exception → 500
         both include trace_id in response body
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.database import init_db, check_db

# ── structlog configuration ───────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

logger = structlog.get_logger()


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from app.pos_correlator import load_pos_csv
    loaded = await load_pos_csv()
    if loaded:
        logger.info("pos_csv_loaded", count=loaded)
    yield


# ── App instance ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    description="Real-time retail analytics from CCTV — Purplle challenge",
    lifespan=lifespan,
)


# ── Phase 7: Structured logging middleware ────────────────────────────────────

@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    trace_id = str(uuid.uuid4())
    start = time.perf_counter()
    request.state.trace_id = trace_id
    response = await call_next(request)
    latency = (time.perf_counter() - start) * 1000
    logger.info(
        "request",
        trace_id    = trace_id,
        endpoint    = str(request.url.path),
        method      = request.method,
        store_id    = request.path_params.get("store_id"),
        latency_ms  = round(latency, 2),
        status_code = response.status_code,
        event_count = getattr(request.state, "event_count", None),
    )
    return response


# ── Phase 8: Graceful degradation ────────────────────────────────────────────

@app.exception_handler(OperationalError)
async def db_unavailable_handler(request: Request, exc: OperationalError):
    return JSONResponse(
        status_code=503,
        content={
            "error":    "service_unavailable",
            "message":  "Database temporarily unavailable. Please retry.",
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )

@app.exception_handler(Exception)
async def generic_handler(request: Request, exc: Exception):
    logger.error(
        "unhandled_error",
        error    = str(exc),
        trace_id = getattr(request.state, "trace_id", None),
    )
    return JSONResponse(
        status_code=500,
        content={
            "error":    "internal_server_error",
            "message":  "An internal error occurred.",
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


# ── Lightweight health probe ──────────────────────────────────────────────────

@app.get("/health", tags=["health"])
async def health():
    db_ok = await check_db()
    return {
        "service":      "store-intelligence-api",
        "status":       "healthy" if db_ok else "degraded",
        "db_connected": db_ok,
    }


# ── Routers ───────────────────────────────────────────────────────────────────

from app.ingestion  import router as ingest_router
from app.metrics    import router as metrics_router
from app.funnel     import router as funnel_router
from app.heatmap    import router as heatmap_router
from app.anomalies  import router as anomalies_router
from app.health     import router as health_detail_router

app.include_router(ingest_router)
app.include_router(metrics_router)
app.include_router(funnel_router)
app.include_router(heatmap_router)
app.include_router(anomalies_router)
app.include_router(health_detail_router)

from app.sse import router as sse_router
app.include_router(sse_router)