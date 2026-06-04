"""
pipeline/staff_classifier.py
-----------------------------
Staff vs customer classifier — multi-store.

Design
------
Each store has a distinct staff uniform colour profile stored in
STORE_PROFILES.  The classifier selects the right profile at construction
time using the store_id from the layout JSON.

If no profile exists for a store_id, the DEFAULT profile is used as a
safe fallback (broad achromatic black — catches most dark uniforms).

Store profiles
--------------
ST1008 — Brigade Road, Bangalore
    Uniform: black / dark-navy shirt + dark trousers.
    Original thresholds kept exactly — no regression risk.

ST1076 — Purplle Store 1076, Mumbai
    Uniform: Purplle brand magenta/hot-pink shirt.
    HSV range for magenta: H≈145-175, S≈80-255, V≈60-255.
    Reject signals updated: dark-navy/black shirt is now a CUSTOMER signal
    (the opposite of Store 1), backpack and maroon rejection retained.

    NOTE: The B.O.H zone (PURPLLE_MUM_1076_Z_BOH) already hard-locks
    is_staff=True via the is_staff_zone flag in the layout — so the colour
    classifier only needs to catch staff on the F.O.H floor camera
    (e.g. a staff member walking across the main floor to assist a customer).
    Imperfect detection there is acceptable; zone-lock is the primary signal.

Zone-lock (both stores)
-----------------------
Any zone where is_staff_zone=True in the layout always returns
(is_staff=True, definitive=True) regardless of colour — this is checked
first in classify_detailed() before any pixel work.
"""
from __future__ import annotations

import numpy as np


# ── Per-store uniform colour profiles ────────────────────────────────────────

_PROFILE_ST1008 = {
    # Staff colours
    "staff_ranges": [
        # Achromatic black (relaxed for warm CCTV — was too tight at S<42 V<88)
        {"lo": (0,   0,   0),   "hi": (180, 58,  115), "label": "black"},
        # Dark navy
        {"lo": (95,  45,  18),  "hi": (132, 200, 125), "label": "navy"},
    ],
    # Customer reject signals
    "reject_ranges": [
        {"lo": (0,   75,  30),  "hi": (18,  255, 220), "label": "maroon",  "thresh": 0.09},
        {"lo": (158, 75,  30),  "hi": (180, 255, 220), "label": "maroon2", "thresh": 0.09},
        {"lo": (128, 60,  30),  "hi": (158, 255, 210), "label": "purple",  "thresh": 0.08},
        {"lo": (18,  90,  100), "hi": (50,  255, 255), "label": "backpack","thresh": 0.055,
         "region": "back"},
    ],
    # Classification thresholds
    "upper_staff_min":       0.37,
    "lower_staff_min":       0.30,
    "black_upper_min":       0.17,
    "upper_only_min":        0.41,
    "black_upper_only_min":  0.20,
    "min_crop_pixels":       380,
}

_PROFILE_ST1076 = {
    # Staff colours: Purplle brand magenta / hot-pink shirt
    # HSV: H≈145-175 (magenta), S≈80-255 (saturated), V≈60-255 (not too dark)
    "staff_ranges": [
        {"lo": (145, 80,  60),  "hi": (180, 255, 255), "label": "magenta"},
        # Wrap-around: very high H (175-180) also covered in range above.
        # Some cameras render Purplle pink closer to H≈155-165 — included.
        {"lo": (155, 70,  50),  "hi": (175, 255, 255), "label": "pink"},
    ],
    # Reject signals for ST1076:
    # - Black/dark shirt → customer (staff wear bright pink, not black)
    # - Backpack → customer (same logic as ST1008)
    "reject_ranges": [
        # Dark / achromatic shirts — customer signal for ST1076
        {"lo": (0,   0,   0),   "hi": (180, 50,  90),  "label": "dark_shirt", "thresh": 0.30},
        # Backpack accent (yellow-green strap)
        {"lo": (18,  90,  100), "hi": (50,  255, 255),  "label": "backpack",   "thresh": 0.055,
         "region": "back"},
    ],
    # Lower thresholds: bright uniform is easy to detect
    "upper_staff_min":       0.22,
    "lower_staff_min":       0.18,
    "black_upper_min":       0.15,   # re-used as "primary_colour_upper_min"
    "upper_only_min":        0.28,
    "black_upper_only_min":  0.15,
    "min_crop_pixels":       300,    # smaller crop OK — bright colour is detectable at smaller res
}

# Default: safe fallback for unknown stores — broad dark-colour detection
_PROFILE_DEFAULT = _PROFILE_ST1008


# ── Classifier ────────────────────────────────────────────────────────────────

class StaffClassifier:
    """Classify a detection crop as staff or customer.

    Uses a per-store colour profile (selected from layout["store_id"]) with
    a zone-lock fast-path: any detection in a zone marked is_staff_zone=True
    immediately returns (True, definitive=True) without pixel work.
    """

    def __init__(self, layout: dict):
        store_id = layout.get("store_id", "")
        self.profile      = self._select_profile(store_id)
        self.staff_zones  = self._extract_staff_zones(layout)
        self._store_id    = store_id

    # ── Profile selection ─────────────────────────────────────────────────────

    @staticmethod
    def _select_profile(store_id: str) -> dict:
        """Return the uniform colour profile for the given store_id."""
        profiles = {
            "ST1008": _PROFILE_ST1008,
            "ST1076": _PROFILE_ST1076,
        }
        return profiles.get(store_id, _PROFILE_DEFAULT)

    # ── Layout helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _extract_staff_zones(layout: dict) -> set[str]:
        """Collect all zone_ids marked is_staff_zone=True across all cameras."""
        zones: set[str] = set()
        for cam_data in layout.get("cameras", {}).values():
            for zone_id, zone_data in cam_data.get("zones", {}).items():
                if zone_data.get("is_staff_zone", False):
                    zones.add(zone_id)
        return zones

    # ── Public API ────────────────────────────────────────────────────────────

    def classify(self, frame, box: list[float],
                 cx: float = 0.0, cy: float = 0.0,
                 current_zone: str = None) -> bool:
        """Return True if the detection is staff. Thin wrapper around classify_detailed."""
        is_staff, _ = self.classify_detailed(frame, box, cx, cy, current_zone)
        return is_staff

    def classify_detailed(self, frame, box: list[float],
                          cx: float = 0.0, cy: float = 0.0,
                          current_zone: str = None) -> tuple[bool, bool]:
        """Classify staff/customer with a definitiveness flag.

        Returns
        -------
        (is_staff: bool, definitive: bool)
            definitive=True means the label should be locked immediately
            (zone-lock or very high-confidence colour match).
            definitive=False means it feeds the majority-vote accumulator.
        """
        # ── Fast path: zone-lock ─────────────────────────────────────────────
        # Any detection in a staff-only zone is definitively staff regardless
        # of what they're wearing. Catches staff behind the counter or in B.O.H.
        if current_zone and current_zone in self.staff_zones:
            return True, True

        # ── Colour-based detection ────────────────────────────────────────────
        if frame is not None:
            x1, y1, x2, y2 = [int(v) for v in box]
            crop_pixels = max(0, x2 - x1) * max(0, y2 - y1)
            if crop_pixels >= self.profile["min_crop_pixels"]:
                return self._detect_uniform(frame, box), False

        return False, False

    # ── Pixel-level detection ─────────────────────────────────────────────────

    @staticmethod
    def _frac(mask: np.ndarray) -> float:
        """Fraction of pixels that are non-zero in a mask."""
        return float(np.sum(mask > 0) / mask.size) if mask.size else 0.0

    def _staff_frac(self, hsv: np.ndarray) -> tuple[float, float]:
        """Return (combined_staff_frac, primary_colour_frac) for the profile.

        For ST1008: primary = black fraction.
        For ST1076: primary = magenta fraction (stored in slot [0]).
        """
        import cv2
        combined = np.zeros(hsv.shape[:2], dtype=np.uint8)
        primary_mask = None
        for i, rng in enumerate(self.profile["staff_ranges"]):
            m = cv2.inRange(hsv, np.array(rng["lo"]), np.array(rng["hi"]))
            combined = combined | m
            if i == 0:
                primary_mask = m
        combined_frac = self._frac(combined)
        primary_frac  = self._frac(primary_mask) if primary_mask is not None else 0.0
        return combined_frac, primary_frac

    def _reject_frac(self, upper_hsv: np.ndarray, back: np.ndarray) -> bool:
        """Return True if any reject signal is above its threshold → customer."""
        import cv2
        for rng in self.profile["reject_ranges"]:
            region = rng.get("region", "upper")
            if region == "back":
                if back.size == 0:
                    continue
                back_hsv = cv2.cvtColor(back, cv2.COLOR_BGR2HSV)
                mask = cv2.inRange(back_hsv, np.array(rng["lo"]), np.array(rng["hi"]))
            else:
                mask = cv2.inRange(upper_hsv, np.array(rng["lo"]), np.array(rng["hi"]))
            if self._frac(mask) >= rng["thresh"]:
                return True   # reject → customer
        return False

    def _detect_uniform(self, frame, box: list[float]) -> bool:
        """Run colour-based uniform detection on the crop region.

        Splits the bounding box into upper (shirt), lower (trousers), and
        back (backpack region) sub-regions, then applies the store profile.
        """
        try:
            import cv2

            x1, y1, x2, y2 = [int(v) for v in box]
            h, w = y2 - y1, x2 - x1
            if h < 20 or w < 12:
                return False

            x1c, x2c = max(0, x1), x2
            upper = frame[y1:          y1 + int(h * 0.45), x1c:x2c]
            lower = frame[y1 + int(h * 0.46): y1 + int(h * 0.90), x1c:x2c]
            back  = frame[y1 + int(h * 0.26): y1 + int(h * 0.70), x1c:x2c]

            if upper.size == 0:
                return False

            upper_hsv = cv2.cvtColor(upper, cv2.COLOR_BGR2HSV)

            # ── Reject signals first (quick exit) ───────────────────────────
            if self._reject_frac(upper_hsv, back):
                return False

            # ── Staff colour fractions ───────────────────────────────────────
            upper_staff, primary_upper = self._staff_frac(upper_hsv)

            lower_staff = 0.0
            if lower.size > 0:
                lower_hsv   = cv2.cvtColor(lower, cv2.COLOR_BGR2HSV)
                lower_staff, _ = self._staff_frac(lower_hsv)

            p = self.profile

            # Path A: full uniform (shirt + trousers both visible)
            if (upper_staff  >= p["upper_staff_min"]
                    and lower_staff  >= p["lower_staff_min"]
                    and primary_upper >= p["black_upper_min"]):
                return True

            # Path B: upper-body only (common on shelf / counter cameras)
            if (upper_staff  >= p["upper_only_min"]
                    and primary_upper >= p["black_upper_only_min"]):
                return True

        except Exception:
            pass

        return False