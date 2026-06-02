"""Staff classification and footfall consistency (no video required)."""
import numpy as np
from datetime import datetime, timezone

from pipeline.staff_classifier import StaffClassifier
from pipeline.tracker import VisitorTracker, TrackState

LAYOUT = {
    "cameras": {
        "CAM_4": {
            "zones": {
                "REST_AREA": {"is_staff_zone": True},
            }
        }
    }
}


def test_rest_area_is_definitive_staff():
    clf = StaffClassifier(LAYOUT)
    is_staff, definitive = clf.classify_detailed(
        frame=None, box=[0, 0, 10, 10], current_zone="REST_AREA"
    )
    assert is_staff is True
    assert definitive is True


def test_unknown_zone_defaults_customer():
    clf = StaffClassifier(LAYOUT)
    is_staff, definitive = clf.classify_detailed(
        frame=None, box=[0, 0, 10, 10], current_zone="SKIN_ZONE"
    )
    assert is_staff is False
    assert definitive is False


def test_footfall_requires_locked_staff():
    tracker = VisitorTracker(store_id="ST1008", layout=LAYOUT)
    track = TrackState(
        track_id=1,
        visitor_id="VIS_test",
        store_id="ST1008",
        is_staff=True,
        last_zone=None,
        last_pos=(0.5, 0.5),
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
        staff_locked=False,
    )
    assert tracker._footfall_is_staff(track) is False
    tracker._lock_staff(track, True)
    assert tracker._footfall_is_staff(track) is True


def _frame_with_torso(bgr_color: tuple[int, int, int], h: int = 200, w: int = 80) -> np.ndarray:
    """Solid-color upper region on grey background (simulates torso crop)."""
    frame = np.full((h, w, 3), 40, dtype=np.uint8)
    frame[0 : h // 2, :] = bgr_color
    return frame


def test_black_torso_classified_staff():
    pytest = __import__("pytest")
    pytest.importorskip("cv2")
    clf = StaffClassifier({})
    # Full-body dark (shirt + trousers) like store uniform
    frame = np.full((200, 80, 3), (20, 20, 25), dtype=np.uint8)
    assert clf.classify(frame, [10, 10, 70, 190]) is True


def test_maroon_torso_classified_customer():
    pytest = __import__("pytest")
    pytest.importorskip("cv2")
    clf = StaffClassifier({})
    frame = _frame_with_torso((40, 25, 80))
    assert clf.classify(frame, [10, 10, 70, 190]) is False


def test_visitor_staff_label_is_sticky():
    tracker = VisitorTracker(store_id="ST1008", layout={})
    track = TrackState(
        track_id=1, visitor_id="VIS_sticky", store_id="ST1008",
        is_staff=False, last_zone=None, last_pos=(0.5, 0.5),
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )
    tracker._lock_staff(track, True)
    assert tracker._visitor_is_staff(track) is True
    track.is_staff = False  # even if track flips, visitor label holds
    assert tracker._visitor_is_staff(track) is True


def test_staff_vote_locks_majority():
    tracker = VisitorTracker(store_id="ST1008", layout=LAYOUT)
    track = TrackState(
        track_id=1,
        visitor_id="VIS_vote",
        store_id="ST1008",
        is_staff=False,
        last_zone=None,
        last_pos=(0.5, 0.5),
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )
    for _ in range(7):
        tracker._update_staff_vote(track, True)
    for _ in range(3):
        tracker._update_staff_vote(track, False)
    assert track.staff_locked is True
    assert track.is_staff is True
