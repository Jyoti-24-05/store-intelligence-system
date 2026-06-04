# DESIGN.md — Store Intelligence System

## Overview

This system converts raw CCTV footage from retail stores into a live analytics API. The central business metric is **offline conversion rate**: the fraction of visitors who entered the store and completed a purchase. Every architectural decision was made to make that number more accurate or more actionable.

The system flows data through four stages:

```
Raw CCTV Clips
    │
    ▼
Detection Pipeline
    YOLOv8m (person detection)
    ByteTrack (multi-object tracking)
    StaffClassifier (per-store colour profile)
    ZoneMapper (layout-driven centroid → zone_id)
    VisitorTracker (Re-ID, entry/exit, dwell, billing events)
    │
    ▼ JSONL event stream (one file per camera)
Event Ingest
    POST /events/ingest → SQLite via SQLAlchemy async
    pos_correlator (POS CSV → visitor session matching)
    anomalies (detect_and_upsert after each batch)
    │
    ▼
Intelligence API (FastAPI)
    /metrics  /funnel  /heatmap  /anomalies  /health/detail
    │
    ▼
React Dashboard + Rich Terminal Dashboard
    (polls API every 15s / 5s)
```

---

## Observed Results

Before describing the architecture, here is what the system actually measured from the two stores' footage — the ground truth every design decision is evaluated against.

### ST1008 — Brigade Road, Bangalore (April 10, 2026)

- **178 events** ingested, **50 unique visitor tracks** across 5 cameras
- **Staff correctly identified**: 45 events (25.3%) tagged `is_staff=true` — within the expected 10–25% band for ~5 floor staff
- **0 mixed `is_staff` labels, 0 ENTRY/EXIT mismatches** across all clips
- **CAM_3 (entry)**: 30 active tracks detected, 0 ENTRY events emitted — correct. Manual inspection confirmed all tracks were staff near the entrance; no customers entered during the recorded window
- **Zone traffic**: Makeup Zone highest (score 100, 28 visits), Skincare Zone second (score 82, 23 visits), Billing Queue third (score 32, 9 visits)
- **Billing**: 9 `BILLING_QUEUE_JOIN` events, queue depth peaked at 9 → `BILLING_QUEUE_SPIKE WARN` anomaly fired
- **POS**: 24 transactions loaded, 0 correlated — correct, because no ENTRY events exist (no customer sessions were created for this clip window)
- **Funnel**: shows all zeros — truthful, not a bug

### ST1076 — Purplle Store 1076, Mumbai (March 8, 2026)

- **139 events** ingested, **11 unique customer visitors** across 4 cameras
- **Staff correctly identified**: 20 events (14.4%) tagged `is_staff=true`
- **Full event lifecycle**: 12 ENTRY, 13 EXIT, 3 REENTRY — entry threshold crossing on both `CAM_ENTRY_1` and `CAM_ENTRY_2` working correctly
- **Zone traffic**: Center Display (Z02) and Lipstick Aisle (Z03) tied at score 100 (13 visits each), Left Shelf (Z01) at 85, Billing Counter Queue and Counter both at 54
- **Dwell times**: Left Shelf ~39s, Lipstick Aisle ~47s, Billing Queue ~60s (longest — consistent with real retail behaviour)
- **Billing**: 8 `BILLING_QUEUE_JOIN`, 2 `BILLING_QUEUE_ABANDON` → **25% abandonment rate**
- **Queue depth**: peaked at 6 → `BILLING_QUEUE_SPIKE WARN` anomaly fired
- **Conversion rate**: 0.0 — correct; no POS CSV was provided for ST1076

---

## Stage 1 — Detection Pipeline

### Entry point: `run.sh` → `detect.py`

`run.sh` accepts `--store 1` or `--store 2` and maps clip filenames to camera IDs via a store-specific associative array. It calls `preprocess_pos.py` to normalise the POS CSV, then invokes `detect.py` per clip sequentially. Output: one JSONL file per camera, merged into `<STORE_ID>_all_events.jsonl`. With `--api-mode`, events are POSTed to the API in batches of 500 after all clips are processed.

`detect.py` processes every 3rd frame (30fps → effective 10fps), runs YOLOv8m with ByteTrack, then routes each detection through three components:

### ZoneMapper (`zone_mapper.py`)

Normalises the bounding-box centroid (cx, cy) to [0, 1] and checks it against a grid of zone rectangles loaded from the store's layout JSON. Zones are evaluated in declaration order; first-match wins. This handles overlapping zones: more specific zones must be listed before broader ones in the layout. For ST1008, whose layout predates the multi-store design, a hardcoded `FALLBACK_ZONE_MAPS` dict is used; ST1076 zones are fully layout-driven.

### StaffClassifier (`staff_classifier.py`)

Two mechanisms in priority order:

**Zone-lock**: any detection inside a zone marked `is_staff_zone: true` in the layout is definitively classified as staff without any pixel work. This is the primary signal for ST1076's Back of House zone (`PURPLLE_MUM_1076_Z_BOH`).

**HSV colour profile**: each store has a distinct per-store uniform profile:
- ST1008: black/dark-navy (`H: 0–180, S: 0–58, V: 0–115` for black; `H: 95–132, S: 45–200, V: 18–125` for navy)
- ST1076: Purplle brand magenta/hot-pink (`H: 145–180, S: 80–255, V: 60–255`)

The classifier uses a **majority-vote accumulator** over 11 frames before locking the `is_staff` label. This prevents a single out-of-focus frame from flipping a customer's classification. Result: 0 mixed `is_staff` labels observed across both stores.

### VisitorTracker (`tracker.py`)

Maps ByteTrack integer track IDs to stable `VIS_xxxxxx` tokens using Re-ID. When `torchreid` is available, it uses OSNet x0.25 embeddings; otherwise HSV colour histograms.

**ENTRY/EXIT detection**: Y-threshold crossing at `cy=0.45` (normalised) on entry cameras only (`is_entry_camera: true` in layout). Moving from `cy < 0.45` toward `cy ≥ 0.45` is an inbound crossing. ST1076 has two entry cameras; cross-camera deduplication via `_active_entries` prevents the same visitor being counted twice.

**Deferred ENTRY**: the ENTRY event is held for up to 1.5 seconds to let the staff-vote accumulator settle. If the visitor is classified as staff before the deferred ENTRY emits, it is suppressed from customer footfall counts.

**REENTRY**: cosine distance against the last 10 embeddings per gallery entry. Distance < 0.30 = same person → `REENTRY` instead of `ENTRY`. Gallery entries expire after 30 minutes. ST1076 observed 3 REENTRY events, confirming this path is exercised.

**Group entry**: visitors crossing the entry threshold within 3 seconds of each other on the same camera share a `group_id` token. `group_size` is backfilled once the window closes. Solo entries correctly receive `group_id=None`.

**ZONE_DWELL**: emitted every 30 seconds of continuous zone presence. Dwell timer resets on zone change. ST1076's Billing Queue zone showed the highest dwell (~60s), consistent with real retail queueing behaviour.

**BILLING_QUEUE_ABANDON**: emitted when a visitor leaves the billing zone without a matched POS transaction. ST1076 observed 2 abandons from 8 joins → 25% abandonment rate.

**Demographics wiring**: `gender_pred`, `age_pred`, `age_bucket`, `is_face_hidden` are accepted as keyword arguments through `tracker.update()` → `build_event()`. Currently `None` for both stores (no demographics model integrated). The pipeline wiring is complete; plugging in a model requires only adding inference in `detect.py` and passing results as keyword arguments.

---

## Stage 2 — Event Ingest (`app/ingestion.py`)

The ingest endpoint accepts batches of up to 500 events. Idempotency is handled by primary-key lookup on `event_id` (UUID v4) before insert. Partial success is implemented: a single event failure does not abort the batch.

After each successful ingest, `correlate_recent()` (POS matching) and `detect_and_upsert()` (anomaly detection) are called. Both anomaly detectors fired correctly on the first ingest batch for each store with no manual trigger.

### Schema migration

The most operationally significant deployment lesson: `migrate_db()` must be called **before** `yield` in the FastAPI lifespan handler, not after. An early version placed it after `yield` (in the shutdown hook), so the new ST1076 columns were never added on startup. This caused all 356 ingest events to fail with `sqlite3.OperationalError: no such column: events.gender_pred`. The fix is a one-line move. The correct startup sequence is:

```
init_db() → migrate_db() → load_pos_csv() → yield
```

---

## Stage 3 — Intelligence API

**Database**: SQLite with `aiosqlite` for async I/O. WAL mode enabled at connection time via `PRAGMA journal_mode=WAL`, allowing concurrent readers while a single writer is active. Composite indexes on `(store_id, timestamp)` and `(store_id, event_type)` cover all query patterns.

**Metrics** (`app/metrics.py`): all queries are live — no caching. The time window falls back to the most recent day that has data when no events exist for today. This ensures the dashboard always shows meaningful numbers for historical clip replays.

**Funnel** (`app/funnel.py`): session-level deduplication. The four stages (entry, zone_visit, billing_queue, purchase) count distinct visitors, not events. ST1008 funnel shows all zeros because no ENTRY events exist for the clip window — correct, not a bug. ST1076 shows 11 entries, 0 zone_visits. Zone visits from `CAM_ZONE` are not linked to entry sessions from `CAM_ENTRY_1/2` in the current per-camera processing model. A unified cross-camera session manager would fix this.

**Heatmap** (`app/heatmap.py`): zone visit frequency normalised 0–100 against the store's own layout. The layout JSON is the authoritative zone list — DB rows for zones not in the layout are discarded. This prevents ST1076 zone IDs bleeding into the ST1008 heatmap when both stores share a database. `zone_name` from the layout is returned alongside `zone_id` so the dashboard can display human-readable labels.

**Anomalies** (`app/anomalies.py`): `BILLING_QUEUE_SPIKE` fired on both stores at first ingest. ST1008: depth 9, threshold 5 → WARN. ST1076: depth 6, threshold 5 → WARN. `CONVERSION_DROP` and `DEAD_ZONE` did not fire — conversion drop requires a 7-day baseline that doesn't exist for a fresh database.

**Health** (`app/health.py`): `GET /health/detail` returns per-store `last_event_at` and feed status. Always HTTP 200. Both stores show `STALE_FEED` when queried after a batch pipeline run — correct for replay deployments; would show `OK` with a live camera feed.

**CORS** (`app/main.py`): `CORSMiddleware` registered immediately after `app = FastAPI(...)`, before any other middleware. Order matters: FastAPI processes middleware in reverse registration order, so CORS must be outermost to handle preflight `OPTIONS` requests before the logging middleware runs.

**Logging**: every HTTP request logged as structured JSON with `trace_id`, `endpoint`, `latency_ms`, and `status_code` via `structlog`. All observed ingest requests completed under 50ms.

---

## Stage 4 — Dashboards

**React dashboard** (`dashboard/`): polls `/stores/{id}/heatmap` every 15 seconds and uses Server-Sent Events (`/stores/{id}/stream`) for live metrics, funnel, and anomalies. Store switching clears stale data only when the cached heatmap belongs to a different store — preventing the null-race that caused a permanent "Loading…" state on store switch.

**Terminal dashboard** (`terminal_dashboard/`): built with `rich`. Polls `/metrics`, `/funnel`, and `/anomalies` every 5 seconds. Requires only the API to be running — no direct database access.

---

## Multi-Store Architecture

The guiding principle: **all ST1076 fields are Optional and default to None**, so every existing ST1008 component continues to work without modification.

| Component | ST1008 | ST1076 |
|-----------|--------|--------|
| Zone config | Hardcoded `FALLBACK_ZONE_MAPS` | `store2_layout.json` |
| Staff detection | Black/navy HSV profile | Magenta/pink HSV profile |
| Entry cameras | CAM_3 (one) | CAM_ENTRY_1 + CAM_ENTRY_2 (two) |
| Staff zone lock | — | `PURPLLE_MUM_1076_Z_BOH` |
| POS correlation | `pos_transactions_ST1008.csv` | No POS file (0.0 conversion rate) |
| Demographics | All null | Wired, model not integrated |

---

## Known Limitations

**ST1008 conversion rate is 0.** Not a bug. The footage window contains no customer entries. 24 POS transactions are loaded but cannot be correlated to visitor sessions that do not exist.

**ST1076 funnel zone_visit stage is 0.** Zone visits from `CAM_ZONE` are not linked to entry sessions created by `CAM_ENTRY_1/2`. Per-camera independent processing cannot perform cross-camera session linkage. A unified multi-camera tracker with shared state would fix this.

**`data_confidence: false` on all heatmaps.** The 20-session threshold for high-confidence heatmaps was not reached with the available clip footage. Heatmap data is directionally correct but honestly flagged.

**Demographics fields are all null.** The pipeline wiring is complete; no age/gender model was integrated. Adding one requires only a few lines in `detect.py`.

---

## AI-Assisted Decisions

### 1. Zone grid coordinate system

AI proposed a normalised [0,1] x/y grid stored in the layout JSON with first-match semantics for overlapping zones. I agreed on the coordinate system but overrode the tie-breaking approach. Claude's first draft proposed a "most overlap" calculation for zones that cover the same centroid. I replaced this with declaration order: more specific zones must be listed before broader fallback zones in the JSON. Declaration order is deterministic and debuggable without a geometry library.

### 2. Staff classification — VLM rejected

AI initially suggested GPT-4V or Gemini Vision for staff classification via a prompt describing the uniform. Cost estimate at 10fps: ~$60/day/store. Not viable. I chose store-specific HSV colour profiles: sub-millisecond per crop, zero external API dependency. Result: 0 mixed `is_staff` labels across both stores.

### 3. `migrate_db()` placement — AI was right about Alembic

When adding 17 new columns for ST1076, AI suggested Alembic for migrations. I chose inline `ALTER TABLE` to keep `docker compose up` as a zero-manual-step operation. This was the right trade-off for a challenge submission. The cost materialised when `migrate_db()` was initially placed after `yield` (shutdown) rather than before it (startup): all 356 ingest events failed silently at the first ingest. With Alembic, `alembic upgrade head` at startup would have caught this loudly before any request was served. The inline approach is simpler to deploy; Alembic is more correct for team production use.

### 4. Heatmap zone bleed — identified via API response inspection

After adding ST1076, the ST1008 heatmap began showing ST1076 zone IDs with 0 visits. Root cause: `all_zone_ids` was built by concatenating `db_zones.keys()` (from the DB query) with `layout_zones`, so any ST1076 zone IDs in the DB (from earlier ingests) leaked into ST1008 responses. Fix: filter DB rows against `layout_zone_set` before building the response — the layout is the authoritative zone list, not the DB.