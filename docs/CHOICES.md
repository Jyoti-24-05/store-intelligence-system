# CHOICES.md — Design Decisions

Three decisions that shaped the system most significantly. For each: the options I considered, what AI suggested, what I chose, and what actually happened when the system ran on real footage.

---

## Decision 1 — Detection Model: YOLOv8m + ByteTrack

### What I needed

A person detector that could run on 1080p CCTV at an effective 10fps, handle partial occlusion in billing queue footage, distinguish individuals in groups of 2–4 entering together, and produce bounding boxes accurate enough to determine entry direction from Y-centroid motion.

### Options considered

**YOLOv8n (nano)**: fastest, lowest memory. Drops accuracy significantly on partial occlusion. In billing queue footage where people are shoulder-to-shoulder, YOLOv8n frequently merges two adjacent bounding boxes into one, directly undercounting group entries. Rejected for this reason.

**YOLOv8m (medium)**: 43M parameters. Runs at ~22fps on a mid-range GPU for 1080p, well above the 10fps target after 3-frame skip. Handles partial occlusion substantially better — correctly splits adjacent people in roughly 85% of frames where nano failed. **Chosen.**

**YOLOv8x (extra-large)**: highest accuracy but ~3× slower than medium. Requires a higher-end GPU to meet real-time throughput. The accuracy gain over medium for standing/walking people in wide-angle retail CCTV is marginal. Not worth the compute cost.

**RT-DETR**: transformer-based, strong on occlusion and small objects. Harder to integrate with ByteTrack's persist-tracking mode. Less community tooling than the YOLO ecosystem. Rejected in favour of reliability.

**MediaPipe Pose**: pose estimation, not detection. Gives body keypoints but is not designed for multi-person tracking in wide-angle CCTV. Rejected.

### Tracking: ByteTrack

Natively integrated with YOLOv8 via `model.track(tracker="bytetrack.yaml", persist=True)`. The `persist=True` flag maintains track history across frames, giving stable integer track IDs that survive brief occlusion (~2 seconds). Alternative trackers (DeepSORT, StrongSORT) require separate Re-ID inference per frame, doubling the compute footprint. ByteTrack handles within-session tracking; the gallery-based Re-ID in `tracker.py` handles the cross-session re-entry case (same person leaving and returning minutes later), which is beyond ByteTrack's window.

### Confidence threshold: 0.25 (below default 0.5)

The spec states low-confidence detections must be emitted, never suppressed. At 0.25, the model occasionally fires on shadows or reflections near the entry camera. These appear as tracks that never cross the threshold or immediately disappear, producing no ENTRY events. The `confidence` field is faithfully carried on every event so the API layer can filter or weight by it.

### What AI suggested

AI suggested starting with YOLOv8s (small) as a faster baseline and upgrading only if occlusion problems appeared. I disagreed: the billing queue footage was described as intentionally challenging, and reworking the tracking pipeline after observing failure is a worse use of development time than starting one size up. I started with medium and would have optimised down if inference speed was a bottleneck — it was not.

### Observed results

**ST1008**: 178 events, 50 unique tracks, 25.3% staff rate — within the expected band. 0 mixed `is_staff` labels, 0 ENTRY/EXIT mismatches. CAM_3 produced 0 ENTRY events: confirmed correct by manual inspection — no customers entered during the clip window; all 30 tracks were staff.

**ST1076**: 256 events across validation, 31 unique visitors, 15.6% staff rate. Full lifecycle: 22 ENTRY, 20 EXIT, 2 REENTRY, 104 ZONE_ENTER, 14 BILLING_QUEUE_JOIN, 4 BILLING_QUEUE_ABANDON. The 3-frame skip was sufficient — no missed crossings observed.

---

## Decision 2 — Event Schema: Single Model, All Optional

### The problem

The spec defines a core event schema with 10 fields and a 3-field `metadata` block. Store 2 (ST1076) requires additional fields from `sample_events.jsonl`: demographics (`gender_pred`, `age_bucket`, `is_face_hidden`), group entry (`group_id`, `group_size`), zone enrichment (`zone_name`, `zone_type`, `is_revenue_zone`, `zone_hotspot_x/y`), and a queue timing block for billing events. One codebase must handle both.

### Options considered

**Two separate Pydantic models (ST1008Event, ST1076Event)**: clean separation, no optional fields. Downside: two ingest endpoints, two ORM mappers, every query needing store-aware branching. A scoring harness would need to know which endpoint to call per store — fragile.

**Discriminated union on `store_id`**: one endpoint, validators that require certain fields when `store_id == "ST1076"`. Cleaner API surface but couples the schema to specific store IDs, making it brittle when adding Store 3.

**Single model, all new fields Optional with None defaults**: one ingest endpoint, one `_to_row()` mapper, one set of API queries. ST1008 events simply have `None` for all enrichment fields — semantically correct (we don't know the visitor's age because no demographics model exists for that store). **Chosen.**

### Event type naming

The spec defines 8 event types in uppercase (`ENTRY`, `ZONE_ENTER`, etc.). The ST1076 `sample_events.jsonl` uses different names (`entry`, `zone_entered`, `queue_completed`). I normalised to the spec's uppercase format in the `EventType` enum. The pipeline always emits uppercase. Accepting both forms would require a custom validator that could silently hide bugs where the wrong type was emitted.

### Timestamps

All timestamps are stored as UTC-aware datetimes. The pipeline derives them from `clip_start_time + frame_number / fps` — deterministic and reproducible, which matters for the idempotency guarantee on ingest.

### What AI suggested

AI suggested a discriminated union (`Annotated[Union[ST1008Event, ST1076Event], Discriminator("store_id")]`) as the most correct design pattern. I overrode this for three reasons: (1) a test harness submitting events with `store_id: "ST1076"` but no demographics fields would fail validation under a strict union; (2) all-optional is simpler to explain and review; (3) one model means one test assertion path rather than store-specific branches.

### Observed results

352 events ingested across both stores with 0 schema validation failures after resolving a `QueueTiming` import issue. The single-model approach meant the same ingest endpoint handled both stores with no store-conditional logic in `ingestion.py` or `_to_row()`.

One deployment issue surfaced: `QueueTiming` was defined in `models.py` but the live container was running an older image predating that class. This was caught immediately by `ImportError` on pipeline startup — a concrete argument for centralising all type definitions in one `models.py` rather than duplicating them across modules.

---

## Decision 3 — Storage: SQLite + Async SQLAlchemy

### The problem

The API must answer live queries across multiple stores while ingesting up to 500 events per request. The storage layer must be reliable for a `docker compose up` acceptance gate and inspectable by an on-call engineer without specialist tooling.

### Options considered

**PostgreSQL**: the right answer for a 40-store production system. Concurrent writers, row-level locking, Alembic migrations. Downside for this submission: adds a second Docker service (~400MB image), increases cold-start time, adds postgres healthcheck complexity to `docker-compose.yml`, and makes `git clone → docker compose up` more likely to fail on the first attempt. Unnecessary at this data volume.

**Redis for live metrics + SQLite for persistence**: Redis is excellent for real-time counters (HyperLogLog for unique visitors, sorted sets for zone scores). This is the architecture I would use at 40 stores with live streams. For this submission the volume — a few hundred events per 20-minute clip — is well within SQLite's capability, and adding Redis adds service dependency, serialisation complexity, and a harder test suite (mock Redis vs mock SQLite).

**SQLite + WAL mode**: single file, zero configuration, built into Python. WAL mode (`PRAGMA journal_mode=WAL`) allows concurrent readers while a single writer is active, which means the API can serve GET requests during ingest. The `api_db` Docker named volume persists the database across container restarts. **Chosen.**

### Indexing strategy

Based on actual query patterns:

- `(store_id, timestamp)` — used by every metrics, funnel, heatmap, and anomaly query
- `(store_id, event_type)` — used by funnel and anomaly queries filtering specific types within a store
- `(store_id, age_bucket)` — enables ST1076 demographic breakdowns without a full table scan
- `visitor_id` — supports funnel deduplication and Re-ID gallery queries

### What AI suggested

AI suggested a Redis cache layer with a 30-second TTL for the metrics endpoint. I rejected this: the spec explicitly requires live metrics, not cached. A 30-second TTL would mean a freshly ingested ENTRY event does not appear in metrics for up to 30 seconds — directly contrary to the spec. Observed query latency on the live system: all metrics endpoints responded under 50ms with 352 events in the database. No caching needed.

The caching discussion did surface one real bug: the `store_metrics_window()` helper was computing `start_of_today` in the server's local timezone, which would give wrong results if the server's timezone differed from the store's timezone. Fixed to always use `datetime.now(timezone.utc)`. This is a case where I rejected the AI's suggestion but the discussion identified a real correctness issue.

### Observed results — schema migration friction

The SQLite choice created friction during schema migration. The `migrate_db()` function issues `ALTER TABLE … ADD COLUMN` for 17 new ST1076 columns at startup. An existing `api_db` volume from a Store-1-only deployment had the old schema, and `migrate_db()` was initially placed after `yield` (shutdown hook) rather than before `yield` (startup). This caused all 356 ingest events to fail with:

```
sqlite3.OperationalError: no such column: events.gender_pred
```

The fix: move `migrate_db()` to the startup path. The correct sequence is `init_db() → migrate_db() → load_pos_csv() → yield`.

With PostgreSQL and Alembic, `alembic upgrade head` at server start would have caught this loudly before the first request was served, rather than silently failing at runtime. This is the concrete production argument for Alembic that the SQLite inline-migration approach trades away in exchange for simpler deployment. For a challenge submission with a one-command acceptance gate, the trade-off was correct. For a production team deployment, it would not be.

### Final API performance

All endpoints under 50ms with 352 events in the database. Both `BILLING_QUEUE_SPIKE` anomalies fired correctly on first ingest with no manual trigger — ST1008 at queue depth 9, ST1076 at queue depth 6, both above the threshold of 5.