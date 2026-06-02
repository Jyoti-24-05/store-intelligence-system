"""
pipeline/staff_classifier.py
-----------------------------
Staff vs customer for Brigade Road CCTV.

Staff: black / dark-navy shirt (+ usually dark trousers).
Customers: often dark too — use maroon/purple/backpack to reject, not brown
(warm store lights make black uniforms look slightly brown on camera).

Balanced mode: between the old 38% (too loose) and 0% (reject rules too harsh).
"""
from __future__ import annotations

import numpy as np


class StaffClassifier:
    # Achromatic black — relaxed for warm CCTV (was too tight at S<42 V<88)
    BLACK_STAFF = ((0, 0, 0), (180, 58, 115))
    NAVY_STAFF = ((95, 45, 18), (132, 200, 125))

    MAROON_CUSTOMER = [
        ((0, 75, 30), (18, 255, 220)),
        ((158, 75, 30), (180, 255, 220)),
    ]
    PURPLE_CUSTOMER = ((128, 60, 30), (158, 255, 210))
    BACKPACK_ACCENT = ((18, 90, 100), (50, 255, 255))

    # Path A: shirt + trousers visible
    UPPER_STAFF_MIN = 0.37
    LOWER_STAFF_MIN = 0.30
    BLACK_UPPER_MIN = 0.17
    # Path B: waist-up / counter shot (lower body occluded)
    UPPER_ONLY_MIN = 0.41
    BLACK_UPPER_ONLY_MIN = 0.20

    MAROON_REJECT_MIN = 0.09
    PURPLE_REJECT_MIN = 0.08
    BACKPACK_REJECT_MIN = 0.055
    MIN_CROP_PIXELS = 380

    def __init__(self, layout: dict):
        self.staff_zones = self._extract_staff_zones(layout)

    def _extract_staff_zones(self, layout: dict) -> set[str]:
        zones = set()
        for cam_data in layout.get("cameras", {}).values():
            for zone_id, zone_data in cam_data.get("zones", {}).items():
                if zone_data.get("is_staff_zone", False):
                    zones.add(zone_id)
        return zones

    def classify(self, frame, box: list[float],
                 cx: float = 0.0, cy: float = 0.0,
                 current_zone: str = None) -> bool:
        is_staff, _ = self.classify_detailed(frame, box, cx, cy, current_zone)
        return is_staff

    def classify_detailed(self, frame, box: list[float],
                          cx: float = 0.0, cy: float = 0.0,
                          current_zone: str = None) -> tuple[bool, bool]:
        if current_zone and current_zone in self.staff_zones:
            return True, True
        if frame is not None:
            x1, y1, x2, y2 = [int(v) for v in box]
            if max(0, x2 - x1) * max(0, y2 - y1) >= self.MIN_CROP_PIXELS:
                return self._detect_uniform(frame, box), False
        return False, False

    @staticmethod
    def _frac(mask: np.ndarray) -> float:
        return float(np.sum(mask > 0) / mask.size) if mask.size else 0.0

    def _staff_masks(self, hsv: np.ndarray):
        import cv2
        black = cv2.inRange(hsv, np.array(self.BLACK_STAFF[0]), np.array(self.BLACK_STAFF[1]))
        navy = cv2.inRange(hsv, np.array(self.NAVY_STAFF[0]), np.array(self.NAVY_STAFF[1]))
        return black, navy, black | navy

    def _reject_signals(self, upper_hsv, back) -> tuple[float, float, float]:
        import cv2
        maroon = 0.0
        for lo, hi in self.MAROON_CUSTOMER:
            m = cv2.inRange(upper_hsv, np.array(lo), np.array(hi))
            maroon = max(maroon, self._frac(m))
        purple = self._frac(
            cv2.inRange(upper_hsv, np.array(self.PURPLE_CUSTOMER[0]),
                        np.array(self.PURPLE_CUSTOMER[1]))
        )
        backpack = 0.0
        if back.size > 0:
            back_hsv = cv2.cvtColor(back, cv2.COLOR_BGR2HSV)
            backpack = self._frac(
                cv2.inRange(back_hsv, np.array(self.BACKPACK_ACCENT[0]),
                            np.array(self.BACKPACK_ACCENT[1]))
            )
        return maroon, purple, backpack

    def _detect_uniform(self, frame, box: list[float]) -> bool:
        try:
            import cv2

            x1, y1, x2, y2 = [int(v) for v in box]
            h, w = y2 - y1, x2 - x1
            if h < 20 or w < 12:
                return False

            x1c, x2c = max(0, x1), x2
            upper = frame[y1: y1 + int(h * 0.45), x1c: x2c]
            lower = frame[y1 + int(h * 0.46): y1 + int(h * 0.90), x1c: x2c]
            back = frame[y1 + int(h * 0.26): y1 + int(h * 0.70), x1c: x2c]
            if upper.size == 0:
                return False

            upper_hsv = cv2.cvtColor(upper, cv2.COLOR_BGR2HSV)
            u_black, _, u_staff = self._staff_masks(upper_hsv)
            upper_staff = self._frac(u_staff)
            black_upper = self._frac(u_black)

            lower_staff = 0.0
            if lower.size > 0:
                lower_hsv = cv2.cvtColor(lower, cv2.COLOR_BGR2HSV)
                _, _, l_staff = self._staff_masks(lower_hsv)
                lower_staff = self._frac(l_staff)

            maroon, purple, backpack = self._reject_signals(upper_hsv, back)
            if maroon >= self.MAROON_REJECT_MIN:
                return False
            if purple >= self.PURPLE_REJECT_MIN:
                return False
            if backpack >= self.BACKPACK_REJECT_MIN:
                return False

            # Full uniform (top + trousers)
            if (upper_staff >= self.UPPER_STAFF_MIN
                    and lower_staff >= self.LOWER_STAFF_MIN
                    and black_upper >= self.BLACK_UPPER_MIN):
                return True

            # Upper-body only (common on shelf cameras)
            if (upper_staff >= self.UPPER_ONLY_MIN
                    and black_upper >= self.BLACK_UPPER_ONLY_MIN):
                return True

        except Exception:
            pass
        return False
