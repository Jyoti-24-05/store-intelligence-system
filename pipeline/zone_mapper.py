"""
pipeline/zone_mapper.py
-----------------------
Maps bounding-box centroids to store zones using grid-based division.

Multi-store design
------------------
Zone maps are now driven by the store's layout JSON rather than being
hardcoded in this file.  Each camera entry in the layout carries a
"zones" dict where every zone has a "grid" sub-object with normalised
(0-1) y_min/y_max/x_min/x_max bounds.

ZoneMapper reads the layout at construction time:
  1. Looks up the camera entry by camera_id.
  2. Builds an ordered list of (y_min, y_max, x_min, x_max, zone_id) tuples.
  3. get_zone() iterates them in order and returns the first match.

Fallback (legacy / no layout provided)
---------------------------------------
If a camera_id is not found in the layout the mapper falls back to the
hardcoded FALLBACK_ZONE_MAPS dict (Store 1 / Brigade Road originals).
This preserves backwards compatibility so Store 1 clips still work even
if passed a minimal or missing layout.

Zone maps — Store 1 (Brigade Road, ST1008) — FALLBACK_ZONE_MAPS
  CAM_1  : left half = MAKEUP_ZONE, right half = SKIN_ZONE
  CAM_2  : four vertical strips (HAIR, BATH_BODY, PERSONAL_CARE, FRAGRANCE)
  CAM_3  : full frame = ENTRY_THRESHOLD  (threshold crossing handled by tracker)
  CAM_4  : full frame = REST_AREA (staff-only back room)
  CAM_5  : top half = BILLING_QUEUE, bottom half = BILLING_COUNTER

Zone maps — Store 2 (Purplle 1076, ST1076) — loaded from store2_layout.json
  CAM_ENTRY_1 : full frame = PURPLLE_MUM_1076_Z_ENTRY
  CAM_ENTRY_2 : full frame = PURPLLE_MUM_1076_Z_ENTRY
  CAM_ZONE    : 5 zones (Left Shelf, Center Display, Lipstick Aisle,
                          Makeup Point, Back Wall Display)
  CAM_BILLING : 3 zones (Billing Counter Queue, Billing Counter, Back of House)
"""
from __future__ import annotations
from typing import Optional


class ZoneMapper:
    """Maps a normalised (cx, cy) centroid to a zone_id for one camera.

    Parameters
    ----------
    camera_id : str
        The camera identifier (e.g. "CAM_3", "CAM_ENTRY_1").
        Normalised to uppercase with underscore separator on construction.
    layout : dict
        The parsed store_layout.json for the store being processed.
        If the camera is found in layout["cameras"], zone grids are read
        from there.  Otherwise FALLBACK_ZONE_MAPS is used.
    """

    # ── Store 1 / Brigade Road fallback ─────────────────────────────────────
    # Tuple format: (y_min, y_max, x_min, x_max, zone_id)
    FALLBACK_ZONE_MAPS: dict[str, list[tuple]] = {
        "CAM_1": [
            (0.0, 1.0, 0.0, 0.5, "MAKEUP_ZONE"),
            (0.0, 1.0, 0.5, 1.0, "SKIN_ZONE"),
        ],
        "CAM_2": [
            (0.0, 1.0, 0.00, 0.25, "HAIR_ZONE"),
            (0.0, 1.0, 0.25, 0.50, "BATH_BODY_ZONE"),
            (0.0, 1.0, 0.50, 0.75, "PERSONAL_CARE_ZONE"),
            (0.0, 1.0, 0.75, 1.00, "FRAGRANCE_ZONE"),
        ],
        "CAM_3": [
            (0.0, 1.0, 0.0, 1.0, "ENTRY_THRESHOLD"),
        ],
        "CAM_4": [
            (0.0, 1.0, 0.0, 1.0, "REST_AREA"),
        ],
        "CAM_5": [
            (0.0, 0.5, 0.0, 1.0, "BILLING_QUEUE"),
            (0.5, 1.0, 0.0, 1.0, "BILLING_COUNTER"),
        ],
    }

    def __init__(self, camera_id: str, layout: dict):
        # ── Normalise camera_id ──────────────────────────────────────────────
        # "CAM3" → "CAM_3", "cam_entry_1" → "CAM_ENTRY_1", already-correct unchanged
        cam = camera_id.upper().strip()
        if cam.startswith("CAM") and "_" not in cam:
            cam = "CAM_" + cam[3:]
        self.camera_id = cam

        # ── Try to build zone map from layout JSON ───────────────────────────
        self.zone_map: list[tuple[float, float, float, float, str]] = []
        self.zone_meta: dict[str, dict] = {}  # zone_id → {zone_name, zone_type, …}

        cameras: dict = layout.get("cameras", {})
        cam_entry: dict = cameras.get(self.camera_id, {})

        if cam_entry:
            # Layout-driven path — used for Store 2 (ST1076) and any future store
            self._build_from_layout(cam_entry)
        else:
            # Fallback path — Store 1 hardcoded grids
            self._build_from_fallback()

    # ── Private builders ─────────────────────────────────────────────────────

    def _build_from_layout(self, cam_entry: dict) -> None:
        """Build zone_map from a camera entry in the layout JSON.

        Each zone under cam_entry["zones"] must have a "grid" object:
            {
              "y_min": 0.0, "y_max": 0.55,
              "x_min": 0.0, "x_max": 1.0
            }
        Zones are added in JSON key order (insertion order in Python 3.7+),
        which matches the priority declared in the layout file.
        """
        zones: dict = cam_entry.get("zones", {})
        for zone_id, zone_info in zones.items():
            grid: dict = zone_info.get("grid", {})
            y_min = float(grid.get("y_min", 0.0))
            y_max = float(grid.get("y_max", 1.0))
            x_min = float(grid.get("x_min", 0.0))
            x_max = float(grid.get("x_max", 1.0))
            self.zone_map.append((y_min, y_max, x_min, x_max, zone_id))
            # Stash rich metadata so emit.py can populate zone_name/zone_type
            self.zone_meta[zone_id] = {
                "zone_name":       zone_info.get("zone_name"),
                "zone_type":       zone_info.get("zone_type"),
                "is_revenue_zone": zone_info.get("is_revenue_zone", False),
                "is_staff_zone":   zone_info.get("is_staff_zone",   False),
            }

    def _build_from_fallback(self) -> None:
        """Build zone_map from the Store 1 hardcoded FALLBACK_ZONE_MAPS."""
        raw = self.FALLBACK_ZONE_MAPS.get(self.camera_id, [])
        for entry in raw:
            if len(entry) == 3:
                # Legacy tuple format: ((y_min, y_max), (x_min, x_max), zone_id)
                (y_min, y_max), (x_min, x_max), zone_id = entry
            else:
                # Flat tuple format: (y_min, y_max, x_min, x_max, zone_id)
                y_min, y_max, x_min, x_max, zone_id = entry
            self.zone_map.append((y_min, y_max, x_min, x_max, zone_id))
            # Minimal meta for Store 1 (no rich zone data available from layout)
            self.zone_meta[zone_id] = {
                "zone_name":       zone_id.replace("_", " ").title(),
                "zone_type":       "BILLING" if "BILLING" in zone_id else
                                   "ENTRY"   if "ENTRY"   in zone_id else "SHELF",
                "is_revenue_zone": "QUEUE" not in zone_id and "REST" not in zone_id,
                "is_staff_zone":   "REST" in zone_id,
            }

    # ── Public API ────────────────────────────────────────────────────────────

    def get_zone(self, cx: float, cy: float) -> Optional[str]:
        """Return zone_id for a normalised centroid (cx, cy) in [0.0, 1.0].

        Iterates zones in declaration order and returns the first match.
        Returns None if no zone covers the centroid (e.g. in an overlap gap).
        """
        for y_min, y_max, x_min, x_max, zone_id in self.zone_map:
            if y_min <= cy < y_max and x_min <= cx < x_max:
                return zone_id
        return None

    def get_zone_meta(self, zone_id: str) -> dict:
        """Return the metadata dict for a zone_id.

        Returns an empty dict if the zone is unknown (safe for callers
        using .get() on the result).
        """
        return self.zone_meta.get(zone_id, {})

    def is_entry_camera(self) -> bool:
        """True if this camera covers the store entry threshold.

        Reads is_entry_camera from the layout entry; falls back to
        checking if any zone in the map is ENTRY_THRESHOLD (Store 1).
        """
        return any(z == "ENTRY_THRESHOLD" or "ENTRY" in z
                   for *_, z in self.zone_map)

    def is_staff_zone(self, zone_id: str) -> bool:
        """True if the given zone is marked as staff-only in the layout."""
        return bool(self.zone_meta.get(zone_id, {}).get("is_staff_zone", False))

    def all_zone_ids(self) -> list[str]:
        """Return all zone IDs covered by this camera, in declaration order."""
        return [z for *_, z in self.zone_map]