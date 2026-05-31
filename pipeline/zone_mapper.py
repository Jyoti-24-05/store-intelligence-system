"""
pipeline/zone_mapper.py
-----------------------
Maps bounding-box centroids to store zones using grid-based division.

Brigade Road store — no pixel-level polygons were provided in the layout xlsx
(it contains embedded floor plan images, not coordinate data). Grid-based
division is used instead. This assumption is documented in CHOICES.md.

Zone maps per camera (normalised 0-1 coordinates):
  CAM_1: left half = MAKEUP_ZONE, right half = SKIN_ZONE
  CAM_2: four equal vertical strips
  CAM_3: full frame = ENTRY_THRESHOLD (threshold crossing handled by tracker)
  CAM_4: full frame = REST_AREA (staff-only)
  CAM_5: top half = BILLING_QUEUE, bottom half = BILLING_COUNTER
"""
from __future__ import annotations
from typing import Optional


class ZoneMapper:
    CAMERA_ZONE_MAPS: dict[str, list[tuple]] = {
        "CAM_1": [
            ((0.0, 1.0), (0.0, 0.5), "MAKEUP_ZONE"),
            ((0.0, 1.0), (0.5, 1.0), "SKIN_ZONE"),
        ],
        "CAM_2": [
            ((0.0, 1.0), (0.00, 0.25), "HAIR_ZONE"),
            ((0.0, 1.0), (0.25, 0.50), "BATH_BODY_ZONE"),
            ((0.0, 1.0), (0.50, 0.75), "PERSONAL_CARE_ZONE"),
            ((0.0, 1.0), (0.75, 1.00), "FRAGRANCE_ZONE"),
        ],
        "CAM_3": [
            ((0.0, 1.0), (0.0, 1.0), "ENTRY_THRESHOLD"),
        ],
        "CAM_4": [
            ((0.0, 1.0), (0.0, 1.0), "REST_AREA"),
        ],
        "CAM_5": [
            ((0.0, 0.5), (0.0, 1.0), "BILLING_QUEUE"),
            ((0.5, 1.0), (0.0, 1.0), "BILLING_COUNTER"),
        ],
    }

    def __init__(self, camera_id: str, layout: dict):
        # Normalise: "CAM3", "cam3", "CAM_3" → "CAM_3"
        cam = camera_id.upper().strip()
        if cam.startswith("CAM") and "_" not in cam:
            cam = "CAM_" + cam[3:]
        self.camera_id = cam
        self.zone_map = self.CAMERA_ZONE_MAPS.get(cam, [])

    def get_zone(self, cx: float, cy: float) -> Optional[str]:
        """Return zone_id for normalised centroid (cx, cy) in [0, 1]."""
        for (y_min, y_max), (x_min, x_max), zone_id in self.zone_map:
            if y_min <= cy < y_max and x_min <= cx < x_max:
                return zone_id
        return None