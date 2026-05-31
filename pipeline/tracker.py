"""
pipeline/tracker.py
-------------------
ByteTrack track_id → stable visitor_id mapping with Re-ID.

Key responsibilities:
  1. Map ByteTrack int track_id → stable VIS_xxxxxx visitor_id
  2. Detect ENTRY / EXIT by Y-threshold crossing on CAM_3
  3. Detect REENTRY — same person returning after EXIT (Re-ID gallery)
  4. Prevent double-counting: one ENTRY per crossing, one EXIT per disappearance
  5. Handle group entry (3 people → 3 ENTRY events)
  6. Emit ZONE_ENTER / ZONE_EXIT / ZONE_DWELL events
  7. Emit BILLING_QUEUE_JOIN when visitor enters billing zone
  8. Handle empty-store periods without crashing
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np


# ── Re-ID: use torchreid if available, else color histogram ──────────────────
try:
    import torchreid
    REID_AVAILABLE = True
except ImportError:
    REID_AVAILABLE = False


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class TrackState:
    track_id:        int
    visitor_id:      str
    store_id:        str
    is_staff:        bool
    last_zone:       Optional[str]
    last_pos:        tuple[float, float]
    first_seen:      datetime
    last_seen:       datetime
    session_seq:     int            = 0
    entered:         bool           = False
    exited:          bool           = False
    dwell_start:     Optional[datetime] = None
    last_dwell_emit: Optional[datetime] = None
    embeddings:      list           = field(default_factory=list)
    billing_emitted: bool           = False    # prevent duplicate BILLING_QUEUE_JOIN


@dataclass
class GalleryEntry:
    visitor_id: str
    store_id:   str
    embeddings: list
    exit_time:  Optional[datetime]
    last_zone:  Optional[str]


# ── Tracker ───────────────────────────────────────────────────────────────────

class VisitorTracker:
    REID_THRESHOLD      = 0.30   # cosine distance — below = same person
    GALLERY_TTL_MINUTES = 30     # forget visitors after 30 min
    EXIT_GRACE_SECONDS  = 2.0    # missing this long = emit EXIT
    DWELL_EMIT_SECONDS  = 30     # emit ZONE_DWELL every 30s of continuous dwell

    # Entry/exit threshold on CAM_3 (normalised Y, 0=top, 1=bottom)
    # Person entering store: moves from above threshold downward (top-mounted cam)
    ENTRY_THRESHOLD_Y   = 0.45

    def __init__(self, store_id: str, layout: dict):
        self.store_id       = store_id
        self.layout         = layout
        self.active_tracks: dict[int, TrackState] = {}
        self.gallery:       list[GalleryEntry]    = []
        self._counter       = 0

        if REID_AVAILABLE:
            self._init_reid()

    def _init_reid(self):
        self._reid = torchreid.models.build_model(
            name="osnet_x0_25", num_classes=1000, pretrained=True
        )
        self._reid.eval()

    def _new_visitor_id(self) -> str:
        self._counter += 1
        h = hashlib.md5(
            f"{self.store_id}{time.time_ns()}{self._counter}".encode()
        ).hexdigest()[:6]
        return f"VIS_{h}"

    def _extract_embedding(self, frame, box) -> Optional[np.ndarray]:
        """OSNet embedding, or HSV histogram fallback."""
        if frame is None:
            return None
        try:
            import cv2
            x1, y1, x2, y2 = [int(v) for v in box]
            crop = frame[max(0, y1):y2, max(0, x1):x2]
            if crop.size == 0:
                return None
            if REID_AVAILABLE:
                import torch
                import torchvision.transforms as T
                t = T.Compose([T.ToPILImage(), T.Resize((256, 128)), T.ToTensor()])
                tensor = t(crop).unsqueeze(0)
                with torch.no_grad():
                    feat = self._reid(tensor)
                return feat.squeeze().numpy()
            else:
                hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
                hist = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
                cv2.normalize(hist, hist)
                return hist.flatten()
        except Exception:
            return None

    def _cosine_dist(self, a, b) -> float:
        if a is None or b is None:
            return 1.0
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 1.0
        return float(1.0 - np.dot(a, b) / (na * nb))

    def _gallery_match(self, emb) -> Optional[GalleryEntry]:
        if emb is None or not self.gallery:
            return None
        best_dist, best = self.REID_THRESHOLD, None
        for entry in self.gallery:
            for stored in entry.embeddings[-5:]:
                d = self._cosine_dist(emb, stored)
                if d < best_dist:
                    best_dist, best = d, entry
        return best

    def _is_entry_camera(self, camera_id: str) -> bool:
        c = camera_id.upper()
        return "CAM_3" in c or "CAM3" in c

    def _check_threshold(self, track: TrackState, cy: float,
                         camera_id: str) -> Optional[str]:
        """Return 'ENTRY', 'EXIT', or None based on Y-threshold crossing."""
        if not self._is_entry_camera(camera_id):
            return None
        prev_cy = track.last_pos[1]
        thr = self.ENTRY_THRESHOLD_Y
        if prev_cy < thr and cy >= thr and not track.entered:
            return "ENTRY"
        if prev_cy >= thr and cy < thr and track.entered and not track.exited:
            return "EXIT"
        return None

    def update(self, track_id: int, cx: float, cy: float,
               box: list, conf: float, is_staff: bool,
               zone_id: Optional[str], frame_time: datetime,
               emitter, frame=None, camera_id: str = "") -> None:
        from pipeline.emit import build_event

        # ── New track: check gallery for re-entry ─────────────────────────
        if track_id not in self.active_tracks:
            emb   = self._extract_embedding(frame, box)
            match = self._gallery_match(emb)

            if match:
                visitor_id = match.visitor_id
                is_reentry = True
            else:
                visitor_id = self._new_visitor_id()
                is_reentry = False

            self.active_tracks[track_id] = TrackState(
                track_id  = track_id,
                visitor_id= visitor_id,
                store_id  = self.store_id,
                is_staff  = is_staff,
                last_zone = None,
                last_pos  = (cx, cy),
                first_seen= frame_time,
                last_seen = frame_time,
            )

            if is_reentry:
                ev = build_event(
                    store_id=self.store_id, camera_id=camera_id,
                    visitor_id=visitor_id, event_type="REENTRY",
                    timestamp=frame_time, zone_id=None, dwell_ms=0,
                    is_staff=is_staff, confidence=conf,
                    session_seq=self.active_tracks[track_id].session_seq,
                )
                emitter.emit(ev)
                self.active_tracks[track_id].session_seq += 1
                self.active_tracks[track_id].entered = True

        track = self.active_tracks[track_id]
        track.last_seen = frame_time
        track.is_staff  = is_staff

        # ── Threshold crossing ────────────────────────────────────────────
        crossing = self._check_threshold(track, cy, camera_id)
        if crossing == "ENTRY":
            emitter.emit(build_event(
                store_id=self.store_id, camera_id=camera_id,
                visitor_id=track.visitor_id, event_type="ENTRY",
                timestamp=frame_time, zone_id=None, dwell_ms=0,
                is_staff=is_staff, confidence=conf,
                session_seq=track.session_seq,
            ))
            track.session_seq += 1
            track.entered = True

        elif crossing == "EXIT":
            emitter.emit(build_event(
                store_id=self.store_id, camera_id=camera_id,
                visitor_id=track.visitor_id, event_type="EXIT",
                timestamp=frame_time, zone_id=None, dwell_ms=0,
                is_staff=is_staff, confidence=conf,
                session_seq=track.session_seq,
            ))
            track.session_seq += 1
            track.exited = True
            self._archive(track)

        # ── Zone transitions ──────────────────────────────────────────────
        if zone_id and zone_id != track.last_zone:
            if track.last_zone and track.last_zone != "ENTRY_THRESHOLD":
                dwell_ms = int(
                    (frame_time - (track.dwell_start or frame_time)).total_seconds() * 1000
                )
                emitter.emit(build_event(
                    store_id=self.store_id, camera_id=camera_id,
                    visitor_id=track.visitor_id, event_type="ZONE_EXIT",
                    timestamp=frame_time, zone_id=track.last_zone, dwell_ms=dwell_ms,
                    is_staff=is_staff, confidence=conf,
                    session_seq=track.session_seq,
                ))
                track.session_seq += 1

            if zone_id != "ENTRY_THRESHOLD":
                emitter.emit(build_event(
                    store_id=self.store_id, camera_id=camera_id,
                    visitor_id=track.visitor_id, event_type="ZONE_ENTER",
                    timestamp=frame_time, zone_id=zone_id, dwell_ms=0,
                    is_staff=is_staff, confidence=conf,
                    session_seq=track.session_seq,
                ))
                track.session_seq += 1
            track.dwell_start    = frame_time
            track.last_dwell_emit= None
            track.billing_emitted= False

        # ── Billing queue join ────────────────────────────────────────────
        if (zone_id and "BILLING" in zone_id.upper()
                and not is_staff and not track.billing_emitted):
            depth = self._billing_queue_depth()
            emitter.emit(build_event(
                store_id=self.store_id, camera_id=camera_id,
                visitor_id=track.visitor_id, event_type="BILLING_QUEUE_JOIN",
                timestamp=frame_time, zone_id=zone_id, dwell_ms=0,
                is_staff=False, confidence=conf,
                session_seq=track.session_seq,
                queue_depth=depth,
            ))
            track.session_seq  += 1
            track.billing_emitted = True

        track.last_pos  = (cx, cy)
        track.last_zone = zone_id

    def check_dwell_events(self, frame_time: datetime, emitter) -> None:
        from pipeline.emit import build_event
        for track in list(self.active_tracks.values()):
            if not track.last_zone or not track.dwell_start:
                continue
            if track.last_zone == "ENTRY_THRESHOLD":
                continue
            dwell_s = (frame_time - track.dwell_start).total_seconds()
            last_s  = (
                (frame_time - track.last_dwell_emit).total_seconds()
                if track.last_dwell_emit else dwell_s
            )
            if dwell_s >= self.DWELL_EMIT_SECONDS and last_s >= self.DWELL_EMIT_SECONDS:
                emitter.emit(build_event(
                    store_id=track.store_id, camera_id="",
                    visitor_id=track.visitor_id, event_type="ZONE_DWELL",
                    timestamp=frame_time, zone_id=track.last_zone,
                    dwell_ms=int(dwell_s * 1000),
                    is_staff=track.is_staff, confidence=0.9,
                    session_seq=track.session_seq,
                ))
                track.session_seq   += 1
                track.last_dwell_emit = frame_time

    def check_exits(self, frame_time: datetime, emitter,
                    current_track_ids: set = None) -> None:
        """Emit EXIT for tracks that disappeared for > EXIT_GRACE_SECONDS."""
        from pipeline.emit import build_event
        if current_track_ids is None:
            return
        gone = set(self.active_tracks.keys()) - current_track_ids
        to_delete = []
        for tid in gone:
            track = self.active_tracks[tid]
            gap = (frame_time - track.last_seen).total_seconds()
            if gap > self.EXIT_GRACE_SECONDS and track.entered and not track.exited:
                emitter.emit(build_event(
                    store_id=track.store_id, camera_id="",
                    visitor_id=track.visitor_id, event_type="EXIT",
                    timestamp=frame_time, zone_id=None, dwell_ms=0,
                    is_staff=track.is_staff,
                    confidence=0.70,  # lower confidence on inferred exit
                    session_seq=track.session_seq,
                ))
                track.session_seq += 1
                track.exited = True
                self._archive(track)
                to_delete.append(tid)
        for tid in to_delete:
            del self.active_tracks[tid]

    def _archive(self, track: TrackState) -> None:
        self.gallery.append(GalleryEntry(
            visitor_id = track.visitor_id,
            store_id   = track.store_id,
            embeddings = track.embeddings[-10:],
            exit_time  = track.last_seen,
            last_zone  = track.last_zone,
        ))
        cutoff = track.last_seen - timedelta(minutes=self.GALLERY_TTL_MINUTES)
        self.gallery = [g for g in self.gallery if g.exit_time and g.exit_time > cutoff]

    def _billing_queue_depth(self) -> int:
        return sum(
            1 for t in self.active_tracks.values()
            if t.last_zone and "BILLING" in (t.last_zone or "").upper()
            and not t.is_staff
        )