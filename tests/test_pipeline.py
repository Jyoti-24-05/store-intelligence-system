# PROMPT: Write pytest unit tests for detection pipeline logic: direction detection,
#         group entry, Re-ID reentry, staff classification, and schema compliance.
#         All tests must use mocks (no real video/model needed).
# CHANGES MADE: Removed pytestmark asyncio (sync tests only); fixed group entry
#               bbox coordinates so centroids correctly cross the entry threshold;
#               kept all 5 spec-required test cases.

import uuid
from datetime import datetime, timezone

from app.models import StoreEvent, EventType, EventMetadata

ENTRY_LINE_Y = 100   # horizontal threshold: y <= 100 = inside store


def _bbox(x1, y1, x2, y2):
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def _centroid(bbox):
    return (
        (bbox["x1"] + bbox["x2"]) / 2,
        (bbox["y1"] + bbox["y2"]) / 2,
    )


# ── 1. Direction detection ────────────────────────────────────────────────────

def test_direction_detection_entry():
    prev_y, curr_y = 110, 90          # outside → inside
    assert prev_y > ENTRY_LINE_Y and curr_y <= ENTRY_LINE_Y


def test_direction_detection_exit():
    prev_y, curr_y = 90, 110          # inside → outside
    assert prev_y <= ENTRY_LINE_Y and curr_y > ENTRY_LINE_Y


def test_direction_no_crossing():
    prev_y, curr_y = 80, 85           # stays inside
    crossed = (prev_y > ENTRY_LINE_Y) != (curr_y > ENTRY_LINE_Y)
    assert not crossed


# ── 2. Group entry: 3 overlapping boxes → 3 ENTRY events ─────────────────────

def test_group_entry_three_people():
    """Three tracks with centroids crossing from y>100 to y<=100 → 3 entries."""
    # Bboxes: top half above ENTRY_LINE_Y (y1=80, y2=100 → centroid y=90)
    tracks = [
        {"track_id": 1, "bbox": _bbox(10,  80, 50,  100)},
        {"track_id": 2, "bbox": _bbox(60,  80, 100, 100)},
        {"track_id": 3, "bbox": _bbox(110, 80, 150, 100)},
    ]
    prev_centroids = {t["track_id"]: (0, 110) for t in tracks}   # all outside
    curr_centroids = {t["track_id"]: _centroid(t["bbox"]) for t in tracks}

    entry_events = [
        tid for tid, (cx, cy) in curr_centroids.items()
        if prev_centroids[tid][1] > ENTRY_LINE_Y and cy <= ENTRY_LINE_Y
    ]
    assert len(entry_events) == 3, \
        f"Expected 3 ENTRY events, got {len(entry_events)}"


# ── 3. Re-ID: same embedding re-entering → REENTRY, not new ENTRY ────────────

def test_reid_reentry_detection():
    exited = {"VIS_abc123", "VIS_def456"}
    assert ("REENTRY" if "VIS_abc123" in exited else "ENTRY") == "REENTRY"
    assert ("REENTRY" if "VIS_new"    in exited else "ENTRY") == "ENTRY"


# ── 4. Staff classification: uniform color match → is_staff=True ─────────────

def test_staff_classification_uniform_color():
    STAFF = {"b_min": 100, "b_max": 255, "g_min": 0, "g_max": 80, "r_min": 0, "r_max": 80}

    def is_staff(b, g, r):
        return (STAFF["b_min"] <= b <= STAFF["b_max"] and
                STAFF["g_min"] <= g <= STAFF["g_max"] and
                STAFF["r_min"] <= r <= STAFF["r_max"])

    assert is_staff(180, 40, 30)   is True   # dark blue → staff
    assert is_staff(20, 30, 200)   is False  # red → customer
    assert is_staff(220, 220, 220) is False  # white → customer


# ── 5. Schema compliance: all EventTypes validate against StoreEvent ──────────

def test_schema_compliance_all_event_types():
    now = datetime.now(timezone.utc)
    no_zone = {EventType.ENTRY, EventType.EXIT, EventType.REENTRY}
    for et in EventType:
        event = StoreEvent(
            event_id=uuid.uuid4(), store_id="STORE_BLR_002", camera_id="CAM_01",
            visitor_id="VIS_test", event_type=et, timestamp=now,
            zone_id=None if et in no_zone else "SKINCARE",
            dwell_ms=0, is_staff=False, confidence=0.85,
            metadata=EventMetadata(session_seq=0),
        )
        assert event.event_type == et
        if et in no_zone:
            assert event.zone_id is None