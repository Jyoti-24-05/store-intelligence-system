"""Pydantic v2 event schema and all API response models.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────

class EventType(str, Enum):
    ENTRY                  = "ENTRY"
    EXIT                   = "EXIT"
    ZONE_ENTER             = "ZONE_ENTER"
    ZONE_EXIT              = "ZONE_EXIT"
    ZONE_DWELL             = "ZONE_DWELL"
    BILLING_QUEUE_JOIN     = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON  = "BILLING_QUEUE_ABANDON"
    REENTRY                = "REENTRY"


class AnomalySeverity(str, Enum):
    INFO     = "INFO"
    WARN     = "WARN"
    CRITICAL = "CRITICAL"


class AnomalyType(str, Enum):
    BILLING_QUEUE_SPIKE = "BILLING_QUEUE_SPIKE"
    CONVERSION_DROP     = "CONVERSION_DROP"
    DEAD_ZONE           = "DEAD_ZONE"
    STALE_FEED          = "STALE_FEED"


# ─────────────────────────────────────────────
# CORE EVENT SCHEMA
# ─────────────────────────────────────────────

class EventMetadata(BaseModel):
    """Nested metadata block — fields are event-type specific."""
    queue_depth: Optional[int] = None   # populated for BILLING_QUEUE_JOIN
    sku_zone:    Optional[str] = None   # zone label from store_layout.json
    session_seq: int = Field(ge=0)      # ordinal position in visitor session


class StoreEvent(BaseModel):
    """Canonical event emitted by the detection pipeline.

    Hard constraints (enforced by validators):
    - event_id must be UUID v4
    - zone_id must be None for ENTRY / EXIT / REENTRY events
    - confidence is retained even when low (never filtered out)
    - timestamp must be timezone-aware ISO-8601 UTC
    """
    event_id:   UUID                                    # UUID v4 — globally unique
    store_id:   str                                     # e.g. "STORE_BLR_002"
    camera_id:  str                                     # e.g. "CAM_ENTRY_01"
    visitor_id: str                                     # e.g. "VIS_c8a2f1" — stable per session
    event_type: EventType
    timestamp:  datetime                                # ISO-8601 UTC
    zone_id:    Optional[str]  = None                  # null for ENTRY / EXIT / REENTRY
    dwell_ms:   int            = Field(ge=0, default=0) # 0 for instantaneous events
    is_staff:   bool           = False
    confidence: float          = Field(ge=0.0, le=1.0) # never suppressed
    metadata:   EventMetadata

    @field_validator("zone_id")
    @classmethod
    def entry_exit_zone_null(cls, v, info):
        """zone_id MUST be null for ENTRY, EXIT, and REENTRY events."""
        if info.data.get("event_type") in (
            EventType.ENTRY,
            EventType.EXIT,
            EventType.REENTRY,
        ):
            return None
        return v

    @field_validator("visitor_id")
    @classmethod
    def visitor_id_format(cls, v: str) -> str:
        """visitor_id must be non-empty and follow VIS_ prefix convention."""
        if not v or not v.strip():
            raise ValueError("visitor_id must not be empty")
        return v

    @field_validator("store_id", "camera_id")
    @classmethod
    def no_empty_strings(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field must not be empty")
        return v

    model_config = {"json_encoders": {datetime: lambda dt: dt.isoformat()}}


# ─────────────────────────────────────────────
# BATCH INGEST REQUEST / RESPONSE
# ─────────────────────────────────────────────

class IngestRequest(BaseModel):
    """POST /events/ingest — accepts up to 500 events per call."""
    events: list[StoreEvent] = Field(
        min_length=1,
        max_length=500,
        description="Batch of store events (1–500 per request)",
    )


class IngestError(BaseModel):
    """Per-event error detail returned on partial failure."""
    event_id: Optional[str] = None   # None if event_id itself was unparseable
    reason:   str


class IngestResponse(BaseModel):
    """POST /events/ingest response — always 200, even on partial failure."""
    ingested:          int
    duplicate_skipped: int
    failed:            int
    errors:            list[IngestError] = []


# ─────────────────────────────────────────────
# METRICS RESPONSE
# ─────────────────────────────────────────────

class ZoneDwellMetric(BaseModel):
    zone_id:      str
    avg_dwell_ms: float
    visit_count:  int


class StoreMetrics(BaseModel):
    store_id:           str
    as_of:              datetime
    unique_visitors:    int    # excludes is_staff=True
    conversion_rate:    float  # 0.0–1.0
    avg_dwell_per_zone: list[ZoneDwellMetric]
    current_queue_depth: int
    abandonment_rate:   float  # 0.0–1.0


# ─────────────────────────────────────────────
# FUNNEL RESPONSE
# ─────────────────────────────────────────────

class FunnelStage(BaseModel):
    stage:        str    # "entry" | "zone_visit" | "billing_queue" | "purchase"
    count:        int
    drop_off_pct: float  # percentage dropped off BEFORE this stage


class StoreFunnel(BaseModel):
    store_id:      str
    as_of:         datetime
    stages:        list[FunnelStage]
    session_count: int   # unique sessions — re-entries NOT double-counted


# ─────────────────────────────────────────────
# HEATMAP RESPONSE
# ─────────────────────────────────────────────

class ZoneHeatmapEntry(BaseModel):
    zone_id:          str
    normalised_score: float  # 0–100
    avg_dwell_ms:     float
    visit_count:      int


class StoreHeatmap(BaseModel):
    store_id:        str
    as_of:           datetime
    zones:           list[ZoneHeatmapEntry]
    data_confidence: bool   # False if fewer than 20 sessions in window


# ─────────────────────────────────────────────
# ANOMALY RESPONSE
# ─────────────────────────────────────────────

class Anomaly(BaseModel):
    anomaly_id:      str
    anomaly_type:    AnomalyType
    severity:        AnomalySeverity
    store_id:        str
    detected_at:     datetime
    description:     str
    suggested_action: str


class StoreAnomalies(BaseModel):
    store_id:  str
    as_of:     datetime
    anomalies: list[Anomaly]


# ─────────────────────────────────────────────
# HEALTH RESPONSE
# ─────────────────────────────────────────────

class StoreHealth(BaseModel):
    store_id:      str
    last_event_at: Optional[datetime]
    status:        str   # "OK" | "STALE_FEED"


class HealthResponse(BaseModel):
    service:      str  = "store-intelligence-api"
    status:       str               # "healthy" | "degraded"
    stores:       list[StoreHealth]
    db_connected: bool


# ─────────────────────────────────────────────
# POS TRANSACTION (internal — used by pos_correlator)
# ─────────────────────────────────────────────

class POSTransaction(BaseModel):
    """Represents a row from pos_transactions.csv."""
    transaction_id:     str
    store_id:           str
    timestamp:          datetime
    basket_value_inr:   float
    matched_visitor_id: Optional[str] = None