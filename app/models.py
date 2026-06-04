"""Pydantic v2 event schema and all API response models.

Multi-store support
-------------------
This schema covers both stores:
  ST1008  — Brigade Road, Bangalore  (Store 1, original)
  ST1076  — Purplle Store 1076, Mumbai  (Store 2, added)

ST1076 introduces additional enrichment fields (gender, age, group, zone
hotspot coordinates, queue timing) that are all Optional so Store 1 events
remain fully valid without them.  The queue timing block (QueueTiming) is
only populated for BILLING_QUEUE_JOIN / BILLING_QUEUE_ABANDON events on
ST1076 — it is None for all other event types and stores.
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


class AgeBucket(str, Enum):
    """Age buckets produced by the ST1076 detection pipeline."""
    UNDER_18    = "under-18"
    AGE_18_24   = "18-24"
    AGE_25_34   = "25-34"
    AGE_35_44   = "35-44"
    AGE_45_54   = "45-54"
    AGE_55_PLUS = "55+"


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

class QueueTiming(BaseModel):
    """Detailed queue timing — populated only for ST1076 billing events.

    Mirrors the richer queue schema introduced in sample_events.jsonl for
    Store 2 (queue_completed / queue_abandoned events).  All fields are
    Optional so a partially-known record can still be stored.
    """
    queue_join_ts:           Optional[datetime] = None  # when visitor joined queue
    queue_served_ts:         Optional[datetime] = None  # when they reached counter (null if abandoned)
    queue_exit_ts:           Optional[datetime] = None  # when they left the billing zone
    wait_seconds:            Optional[int]      = None  # total wait from join → served/exit
    queue_position_at_join:  Optional[int]      = None  # depth at the moment they joined
    abandoned:               bool               = False  # True = BILLING_QUEUE_ABANDON


class EventMetadata(BaseModel):
    """Nested metadata block — fields are event-type specific.

    Core fields (both stores)
    -------------------------
    queue_depth  — current queue length; populated for BILLING_QUEUE_JOIN
    sku_zone     — human-readable zone label from store_layout.json
    session_seq  — ordinal position of this event within the visitor session

    ST1076 enrichment fields (optional, None for ST1008)
    -----------------------------------------------------
    gender_pred    — predicted gender: "M" | "F" | None
    age_pred       — predicted age in years (integer)
    age_bucket     — bucketed age range e.g. "25-34"
    is_face_hidden — True when face was obscured (mask, angle, blur artefact)
    group_id       — shared token for visitors who entered as a group; None if solo
    group_size     — total headcount in the group; None if solo

    Zone enrichment (ST1076 zone events)
    ------------------------------------
    zone_name       — human-readable zone name e.g. "Left Shelf"
    zone_type       — structural type e.g. "SHELF" | "DISPLAY" | "BILLING"
    is_revenue_zone — whether the zone is a revenue-generating area
    zone_hotspot_x  — pixel X of detection centroid in the camera frame
    zone_hotspot_y  — pixel Y of detection centroid in the camera frame

    Queue timing (ST1076 billing events only)
    -----------------------------------------
    queue_timing   — nested QueueTiming block; None for all other events/stores
    """
    # ── Core (both stores) ──────────────────────────────────────────────────
    queue_depth:     Optional[int]   = None          # populated for BILLING_QUEUE_JOIN
    sku_zone:        Optional[str]   = None          # zone label from store_layout.json
    session_seq:     int             = Field(default=0, ge=0)  # ordinal position in session

    # ── Visitor demographics (ST1076) ───────────────────────────────────────
    gender_pred:     Optional[str]   = None          # "M" | "F"
    age_pred:        Optional[int]   = None          # predicted age in years
    age_bucket:      Optional[str]   = None          # e.g. "25-34" — free string for forward compat
    is_face_hidden:  Optional[bool]  = None          # True when face was obscured

    # ── Group entry (ST1076) ────────────────────────────────────────────────
    group_id:        Optional[str]   = None          # shared token e.g. "G_10"; None if solo
    group_size:      Optional[int]   = None          # total group headcount; None if solo

    # ── Zone enrichment (ST1076 zone events) ────────────────────────────────
    zone_name:       Optional[str]   = None          # e.g. "Left Shelf"
    zone_type:       Optional[str]   = None          # e.g. "SHELF" | "DISPLAY" | "BILLING"
    is_revenue_zone: Optional[bool]  = None          # True if revenue-generating zone
    zone_hotspot_x:  Optional[float] = None          # detection centroid pixel X
    zone_hotspot_y:  Optional[float] = None          # detection centroid pixel Y

    # ── Queue timing (ST1076 billing events only) ────────────────────────────
    queue_timing:    Optional[QueueTiming] = None


class StoreEvent(BaseModel):
    """Canonical event emitted by the detection pipeline.

    Hard constraints (enforced by validators):
    - event_id must be UUID v4
    - zone_id must be None for ENTRY / EXIT / REENTRY events
    - confidence is retained even when low (never filtered out)
    - timestamp must be timezone-aware ISO-8601 UTC

    Multi-store notes:
    - ST1008 events: metadata enrichment fields will be None (backwards compat)
    - ST1076 events: metadata carries gender, age, group, zone enrichment, and
      queue_timing for billing events
    """
    event_id:   UUID                                     # UUID v4 — globally unique
    store_id:   str                                      # e.g. "ST1008" | "ST1076"
    camera_id:  str                                      # e.g. "CAM_3" | "CAM_ENTRY_1"
    visitor_id: str                                      # e.g. "VIS_c8a2f1" — stable per session
    event_type: EventType
    timestamp:  datetime                                 # ISO-8601 UTC
    zone_id:    Optional[str]  = None                   # null for ENTRY / EXIT / REENTRY
    dwell_ms:   int            = Field(ge=0, default=0)  # 0 for instantaneous events
    is_staff:   bool           = False
    confidence: float          = Field(ge=0.0, le=1.0)  # never suppressed
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
        """visitor_id must be non-empty."""
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
    store_id:            str
    as_of:               datetime
    unique_visitors:     int    # excludes is_staff=True
    conversion_rate:     float  # 0.0–1.0
    avg_dwell_per_zone:  list[ZoneDwellMetric]
    current_queue_depth: int
    abandonment_rate:    float  # 0.0–1.0


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
    zone_name:        Optional[str]  = None
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
    anomaly_id:       str
    anomaly_type:     AnomalyType
    severity:         AnomalySeverity
    store_id:         str
    detected_at:      datetime
    description:      str
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