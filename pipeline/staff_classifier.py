"""
pipeline/staff_classifier.py
-----------------------------
Classifies detections as staff vs customer.

Brigade Road context:
  - 5 known staff: Zufishan Khazra, kasthuri v, Priya v, Shashikala ., Naziya Begum
  - Staff wear Purplle branded uniform — typically black/dark with branding
  - CAM_4 (REST_AREA) is staff-only → any detection there = staff
  - Staff move across ALL zones; color detection is primary signal for other cameras

Two methods (both applied; OR logic):
  1. Zone-based: REST_AREA → always staff
  2. Color-based: detect dark/black uniform in upper-body crop
"""
from __future__ import annotations
import numpy as np


class StaffClassifier:
    # HSV ranges for Purplle staff uniform (dark/black, or dark blue alternate)
    STAFF_HSV_RANGES = [
        ((0, 0, 0),     (180, 255, 60)),    # black / very dark
        ((100, 50, 50), (130, 255, 150)),   # dark blue alternate
    ]
    UNIFORM_THRESHOLD = 0.40   # 40% of upper body matching = staff

    def __init__(self, layout: dict):
        self.staff_zones = self._extract_staff_zones(layout)

    def _extract_staff_zones(self, layout: dict) -> set[str]:
        zones = set()
        for cam_data in layout.get("cameras", {}).values():
            for zone_id, zone_data in cam_data.get("zones", {}).items():
                if zone_data.get("is_staff_zone", False):
                    zones.add(zone_id)
        return zones  # {"REST_AREA"}

    def classify(self, frame, box: list[float],
                 cx: float = 0.0, cy: float = 0.0,
                 current_zone: str = None) -> bool:
        """Return True if classified as staff. Errs toward customer if uncertain."""
        # Method 1: Zone-based (definitive)
        if current_zone and current_zone in self.staff_zones:
            return True

        # Method 2: Uniform color
        if frame is not None:
            return self._detect_uniform(frame, box)

        return False

    def _detect_uniform(self, frame, box: list[float]) -> bool:
        try:
            import cv2
            x1, y1, x2, y2 = [int(v) for v in box]
            h = y2 - y1
            crop = frame[y1: y1 + int(h * 0.4), max(0, x1): x2]
            if crop.size == 0:
                return False
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            total = crop.shape[0] * crop.shape[1]
            if total == 0:
                return False
            for lower, upper in self.STAFF_HSV_RANGES:
                mask = cv2.inRange(hsv, np.array(lower), np.array(upper))
                if np.sum(mask > 0) / total >= self.UNIFORM_THRESHOLD:
                    return True
        except Exception:
            pass
        return False