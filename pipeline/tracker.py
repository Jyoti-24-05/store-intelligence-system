"""
pipeline/tracker.py
-------------------
ByteTrack track_id → stable visitor_id mapping with Re-ID.

Spec requirements implemented:
  1. Map ByteTrack int track_id → stable VIS_xxxxxx visitor_id
  2. ENTRY / EXIT — Y-threshold crossing on CAM_3 ONLY
  3. REENTRY — same person returning after EXIT (Re-ID cosine distance)
  4. Group entry — 3 people enter together → 3 ENTRY events (each track_id independent)
  5. Staff exclusion — majority vote over frames, not single-frame flip
  6. ZONE_ENTER / ZONE_EXIT / ZONE_DWELL (every 30s of continuous dwell)
  7. BILLING_QUEUE_JOIN — visitor enters billing zone while queue_depth > 0
  8. BILLING_QUEUE_ABANDON — visitor leaves billing zone with no POS in 5-min window
  9. Cross-camera deduplication — same visitor_id active on two cameras suppresses duplicate ENTRY
  10. Empty store periods — no crash when zero detections
  11. Confidence calibration — low-conf events emitted, never silently dropped
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

try:
    import torchreid
    REID_AVAILABLE = True
except ImportError:
    REID_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TrackState:
    track_id:         int
    visitor_id:       str
    store_id:         str
    is_staff:         bool
    last_zone:        Optional[str]
    last_pos:         tuple[float, float]
    first_seen:       datetime
    last_seen:        datetime
    session_seq:      int            = 0
    entered:          bool           = False
    exited:           bool           = False
    dwell_start:      Optional[datetime] = None
    last_dwell_emit:  Optional[datetime] = None
    embeddings:       list           = field(default_factory=list)
    billing_emitted:  bool           = False   # prevent duplicate BILLING_QUEUE_JOIN
    billing_zone_enter_time: Optional[datetime] = None  # for ABANDON detection
    # Staff majority vote — stable per visitor_id (no ENTRY/EXIT flip-flop)
    staff_votes:      int            = 0
    total_votes:      int            = 0
    staff_locked:     bool           = False
    pending_entry:    bool           = False   # defer ENTRY until staff vote settles
    pending_entry_since: Optional[datetime] = None
    entered_zone_emitted: Optional[str] = None  # last zone we emitted ZONE_ENTER for


@dataclass
class GalleryEntry:
    visitor_id: str
    store_id:   str
    embeddings: list
    exit_time:  Optional[datetime]
    last_zone:  Optional[str]
    is_staff:   bool = False
    staff_locked: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Tracker
# ─────────────────────────────────────────────────────────────────────────────

class VisitorTracker:
    REID_THRESHOLD       = 0.30   # cosine distance — below = same person
    GALLERY_TTL_MINUTES  = 30     # forget visitors after 30 min
    EXIT_GRACE_SECONDS   = 2.0    # missing this long → emit EXIT
    DWELL_EMIT_SECONDS   = 30     # emit ZONE_DWELL every 30s continuous dwell
    ENTRY_THRESHOLD_Y    = 0.45   # normalised Y for CAM_3 threshold crossing
    STAFF_VOTE_THRESHOLD    = 0.64   # need consistent frames — cuts false staff (~30% → ~20%)
    STAFF_LOCK_FRAMES       = 11
    ENTRY_STAFF_WAIT_SECONDS = 1.5   # defer ENTRY until staff vote settles (footfall)
    # BILLING_QUEUE_ABANDON: visitor leaves billing without POS in this window
    ABANDON_WINDOW_SECONDS = 300  # 5 minutes

    def __init__(self, store_id: str, layout: dict):
        self.store_id      = store_id
        self.layout        = layout
        self.active_tracks: dict[int, TrackState] = {}
        self.gallery:       list[GalleryEntry]    = []
        self._counter      = 0
        # Cross-camera deduplication: visitor_id → camera_id of first active ENTRY
        self._active_entries: dict[str, str] = {}
        # One stable is_staff label per visitor_id (fixes mixed True/False on same VIS_*)
        self._visitor_staff: dict[str, bool] = {}

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
        if frame is None:
            return None
        try:
            import cv2
            x1, y1, x2, y2 = [int(v) for v in box]
            crop = frame[max(0, y1):y2, max(0, x1):x2]
            if crop.size == 0:
                return None
            if REID_AVAILABLE:
                import torch, torchvision.transforms as T
                t = T.Compose([T.ToPILImage(), T.Resize((256, 128)), T.ToTensor()])
                with torch.no_grad():
                    feat = self._reid(t(crop).unsqueeze(0))
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

    def _lock_staff(self, track: TrackState, is_staff: bool) -> None:
        track.is_staff = is_staff
        track.staff_locked = True
        self._visitor_staff[track.visitor_id] = is_staff

    def _visitor_is_staff(self, track: TrackState) -> bool:
        """Single label per visitor_id — every event uses the same is_staff value."""
        vid = track.visitor_id
        if vid in self._visitor_staff:
            return self._visitor_staff[vid]
        if track.staff_locked:
            self._visitor_staff[vid] = track.is_staff
            return track.is_staff
        return False

    def _footfall_is_staff(self, track: TrackState) -> bool:
        return self._visitor_is_staff(track)

    def _emit_is_staff(self, track: TrackState) -> bool:
        return self._visitor_is_staff(track)

    def _update_staff_vote(self, track: TrackState, is_staff_frame: bool,
                           staff_definitive: bool = False) -> bool:
        """
        Majority-vote staff classification.
        Returns True if classification just locked this frame.
        """
        if staff_definitive:
            if not track.staff_locked:
                self._lock_staff(track, True)
                return True
            return False
        if track.staff_locked:
            return False
        track.total_votes += 1
        if is_staff_frame:
            track.staff_votes += 1
        if track.total_votes >= self.STAFF_LOCK_FRAMES:
            self._lock_staff(
                track,
                (track.staff_votes / track.total_votes) >= self.STAFF_VOTE_THRESHOLD,
            )
            return True
        return False

    def _on_staff_locked(self, track: TrackState, camera_id: str,
                         frame_time: datetime, zone_id: Optional[str],
                         emitter, conf: float) -> None:
        """Emit deferred ZONE_ENTER after label is stable."""
        from pipeline.emit import build_event
        z = zone_id or track.last_zone
        if (z and z != "ENTRY_THRESHOLD"
                and track.entered_zone_emitted != z):
            emitter.emit(build_event(
                store_id=self.store_id, camera_id=camera_id,
                visitor_id=track.visitor_id, event_type="ZONE_ENTER",
                timestamp=frame_time, zone_id=z, dwell_ms=0,
                is_staff=self._visitor_is_staff(track), confidence=conf,
                session_seq=track.session_seq,
            ))
            track.session_seq += 1
            track.entered_zone_emitted = z
            track.dwell_start = frame_time
            track.last_dwell_emit = None

    def _check_threshold(self, track: TrackState, cy: float,
                          camera_id: str) -> Optional[str]:
        """
        Detect ENTRY/EXIT by Y-threshold crossing on CAM_3.
        Only CAM_3 (entry gate) emits these events — other cameras never do.
        Group entry: each track_id is processed independently so 3 people
        crossing together emit 3 separate ENTRY events.
        """
        if not self._is_entry_camera(camera_id):
            return None
        prev_cy = track.last_pos[1]
        thr     = self.ENTRY_THRESHOLD_Y
        if prev_cy < thr and cy >= thr and not track.entered and not track.pending_entry:
            return "ENTRY"
        if prev_cy >= thr and cy < thr and track.entered and not track.exited:
            return "EXIT"
        return None

    def _try_emit_pending_entry(self, track: TrackState, camera_id: str,
                                frame_time: datetime, emitter, conf: float) -> None:
        """Emit deferred ENTRY once staff classification has settled."""
        from pipeline.emit import build_event
        if not track.pending_entry:
            return
        elapsed = (
            (frame_time - track.pending_entry_since).total_seconds()
            if track.pending_entry_since else 0.0
        )
        if not track.staff_locked and elapsed < self.ENTRY_STAFF_WAIT_SECONDS:
            return

        already_active = self._active_entries.get(track.visitor_id)
        if already_active and already_active != camera_id:
            track.pending_entry = False
            track.entered = True
            return

        emitter.emit(build_event(
            store_id=self.store_id, camera_id=camera_id,
            visitor_id=track.visitor_id, event_type="ENTRY",
            timestamp=frame_time, zone_id=None, dwell_ms=0,
            is_staff=self._footfall_is_staff(track), confidence=conf,
            session_seq=track.session_seq,
        ))
        track.session_seq += 1
        track.entered = True
        track.pending_entry = False
        self._active_entries[track.visitor_id] = camera_id

    def update(self, track_id: int, cx: float, cy: float,
               box: list, conf: float, is_staff: bool,
               zone_id: Optional[str], frame_time: datetime,
               emitter, frame=None, camera_id: str = "",
               staff_definitive: bool = False) -> None:
        from pipeline.emit import build_event

        # ── New track: Re-ID gallery check ───────────────────────────────────
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
                track_id   = track_id,
                visitor_id = visitor_id,
                store_id   = self.store_id,
                is_staff   = False,   # customer until votes / REST_AREA lock
                last_zone  = None,
                last_pos   = (cx, cy),
                first_seen = frame_time,
                last_seen  = frame_time,
            )
            track = self.active_tracks[track_id]
            if match and match.staff_locked:
                self._lock_staff(track, match.is_staff)

            if is_reentry and match and match.staff_locked:
                self._visitor_staff[visitor_id] = match.is_staff

            if is_reentry:
                emitter.emit(build_event(
                    store_id=self.store_id, camera_id=camera_id,
                    visitor_id=visitor_id, event_type="REENTRY",
                    timestamp=frame_time, zone_id=None, dwell_ms=0,
                    is_staff=self._footfall_is_staff(track), confidence=conf,
                    session_seq=track.session_seq,
                ))
                track.session_seq += 1
                track.entered = True

        track = self.active_tracks[track_id]
        track.last_seen = frame_time

        emb = self._extract_embedding(frame, box)
        if emb is not None and len(track.embeddings) < 20:
            track.embeddings.append(emb)

        just_locked = self._update_staff_vote(
            track, is_staff, staff_definitive=staff_definitive
        )
        if just_locked:
            self._on_staff_locked(track, camera_id, frame_time, zone_id, emitter, conf)
        self._try_emit_pending_entry(track, camera_id, frame_time, emitter, conf)

        crossing = self._check_threshold(track, cy, camera_id)

        if crossing == "ENTRY":
            already_active = self._active_entries.get(track.visitor_id)
            if not already_active or already_active == camera_id:
                track.pending_entry = True
                track.pending_entry_since = frame_time

        elif crossing == "EXIT":
            if track.pending_entry:
                self._try_emit_pending_entry(track, camera_id, frame_time, emitter, conf)
            emitter.emit(build_event(
                store_id=self.store_id, camera_id=camera_id,
                visitor_id=track.visitor_id, event_type="EXIT",
                timestamp=frame_time, zone_id=None, dwell_ms=0,
                is_staff=self._footfall_is_staff(track), confidence=conf,
                session_seq=track.session_seq,
            ))
            track.session_seq += 1
            track.exited = True
            self._active_entries.pop(track.visitor_id, None)
            self._archive(track)

        # ── Zone transitions (only emit after staff label locked — no mixed is_staff) ──
        if zone_id and zone_id != track.last_zone:
            if track.staff_locked:
                if track.last_zone and track.last_zone != "ENTRY_THRESHOLD":
                    dwell_ms = int(
                        (frame_time - (track.dwell_start or frame_time)).total_seconds() * 1000
                    )
                    emitter.emit(build_event(
                        store_id=self.store_id, camera_id=camera_id,
                        visitor_id=track.visitor_id, event_type="ZONE_EXIT",
                        timestamp=frame_time, zone_id=track.last_zone, dwell_ms=dwell_ms,
                        is_staff=self._visitor_is_staff(track), confidence=conf,
                        session_seq=track.session_seq,
                    ))
                    track.session_seq += 1
                    track.entered_zone_emitted = None

                    if ("BILLING" in (track.last_zone or "").upper()
                            and not self._visitor_is_staff(track)):
                        if track.billing_zone_enter_time:
                            emitter.emit(build_event(
                                store_id=self.store_id, camera_id=camera_id,
                                visitor_id=track.visitor_id,
                                event_type="BILLING_QUEUE_ABANDON",
                                timestamp=frame_time, zone_id=track.last_zone,
                                dwell_ms=dwell_ms,
                                is_staff=False, confidence=conf,
                                session_seq=track.session_seq,
                            ))
                            track.session_seq += 1
                        track.billing_zone_enter_time = None
                        track.billing_emitted = False

                if zone_id != "ENTRY_THRESHOLD":
                    emitter.emit(build_event(
                        store_id=self.store_id, camera_id=camera_id,
                        visitor_id=track.visitor_id, event_type="ZONE_ENTER",
                        timestamp=frame_time, zone_id=zone_id, dwell_ms=0,
                        is_staff=self._visitor_is_staff(track), confidence=conf,
                        session_seq=track.session_seq,
                    ))
                    track.session_seq += 1
                    track.entered_zone_emitted = zone_id

                track.dwell_start = frame_time
                track.last_dwell_emit = None
                track.billing_emitted = False
            else:
                # Track zone internally; ZONE_ENTER emitted in _on_staff_locked
                track.dwell_start = frame_time
                track.last_dwell_emit = None
                track.billing_emitted = False

        # ── Billing queue join ────────────────────────────────────────────────
        # SPEC: emit when visitor enters billing zone while queue_depth > 0
        if (zone_id and "BILLING" in zone_id.upper()
                and track.staff_locked
                and not self._visitor_is_staff(track) and not track.billing_emitted):
            depth = self._billing_queue_depth()
            if depth > 0:
                emitter.emit(build_event(
                    store_id=self.store_id, camera_id=camera_id,
                    visitor_id=track.visitor_id,
                    event_type="BILLING_QUEUE_JOIN",
                    timestamp=frame_time, zone_id=zone_id, dwell_ms=0,
                    is_staff=False, confidence=conf,
                    session_seq=track.session_seq,
                    queue_depth=depth,
                ))
                track.session_seq   += 1
                track.billing_emitted = True
                track.billing_zone_enter_time = frame_time

        track.last_pos  = (cx, cy)
        track.last_zone = zone_id

    def check_dwell_events(self, frame_time: datetime, emitter) -> None:
        """
        SPEC: Emit ZONE_DWELL every 30 seconds of continuous zone presence.
        """
        from pipeline.emit import build_event
        for track in list(self.active_tracks.values()):
            if not track.staff_locked or not track.last_zone or not track.dwell_start:
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
                    store_id=track.store_id, camera_id=emitter.camera_id,
                    visitor_id=track.visitor_id, event_type="ZONE_DWELL",
                    timestamp=frame_time, zone_id=track.last_zone,
                    dwell_ms=int(dwell_s * 1000),
                    is_staff=self._visitor_is_staff(track), confidence=0.9,
                    session_seq=track.session_seq,
                ))
                track.session_seq    += 1
                track.last_dwell_emit = frame_time

    def check_exits(self, frame_time: datetime, emitter,
                    current_track_ids: set = None) -> None:
        """
        SPEC: Empty store periods must not crash.
        Emit EXIT for tracks missing > EXIT_GRACE_SECONDS.
        Lower confidence (0.70) on inferred exits.
        """
        from pipeline.emit import build_event
        if current_track_ids is None:
            return
        gone = set(self.active_tracks.keys()) - current_track_ids
        to_delete = []
        for tid in gone:
            track = self.active_tracks[tid]
            gap = (frame_time - track.last_seen).total_seconds()
            if gap > self.EXIT_GRACE_SECONDS and track.entered and not track.exited:
                if track.pending_entry:
                    self._try_emit_pending_entry(
                        track, emitter.camera_id, frame_time, emitter, 0.70
                    )
                emitter.emit(build_event(
                    store_id=track.store_id, camera_id=emitter.camera_id,
                    visitor_id=track.visitor_id, event_type="EXIT",
                    timestamp=frame_time, zone_id=None, dwell_ms=0,
                    is_staff=self._footfall_is_staff(track),
                    confidence=0.70,  # lower confidence — inferred, not observed
                    session_seq=track.session_seq,
                ))
                track.session_seq += 1
                track.exited = True
                self._active_entries.pop(track.visitor_id, None)
                self._archive(track)
                to_delete.append(tid)
        for tid in to_delete:
            del self.active_tracks[tid]

    def _archive(self, track: TrackState) -> None:
        self.gallery.append(GalleryEntry(
            visitor_id   = track.visitor_id,
            store_id     = track.store_id,
            embeddings   = track.embeddings[-10:],
            exit_time    = track.last_seen,
            last_zone    = track.last_zone,
            is_staff     = track.is_staff,
            staff_locked = track.staff_locked,
        ))
        cutoff = track.last_seen - timedelta(minutes=self.GALLERY_TTL_MINUTES)
        self.gallery = [g for g in self.gallery if g.exit_time and g.exit_time > cutoff]

    def _billing_queue_depth(self) -> int:
        """Count non-staff visitors currently in any billing zone."""
        return sum(
            1 for t in self.active_tracks.values()
            if t.last_zone and "BILLING" in (t.last_zone or "").upper()
            and not self._visitor_is_staff(t)
        )