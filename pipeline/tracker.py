"""
pipeline/tracker.py
-------------------
ByteTrack track_id → stable visitor_id mapping with Re-ID.

Spec requirements implemented:
  1. Map ByteTrack int track_id → stable VIS_xxxxxx visitor_id
  2. ENTRY / EXIT — Y-threshold crossing on entry cameras (CAM_3 / CAM_ENTRY_1 / CAM_ENTRY_2)
  3. REENTRY — same person returning after EXIT (Re-ID cosine distance)
  4. Group entry — 3 people enter together → 3 ENTRY events (each track_id independent)
     Group detection: visitors crossing the entry threshold within GROUP_ENTRY_WINDOW_SECONDS
     of each other on the same camera share a group_id token (e.g. "G_3").
  5. Staff exclusion — majority vote over frames, not single-frame flip
  6. ZONE_ENTER / ZONE_EXIT / ZONE_DWELL (every 30s of continuous dwell)
  7. BILLING_QUEUE_JOIN — visitor enters billing zone while queue_depth > 0
  8. BILLING_QUEUE_ABANDON — visitor leaves billing zone with no POS in 5-min window
  9. Cross-camera deduplication — same visitor_id active on two cameras suppresses duplicate ENTRY
  10. Empty store periods — no crash when zero detections
  11. Confidence calibration — low-conf events emitted, never silently dropped
  12. [Step 10] Demographics — gender_pred, age_pred, age_bucket, is_face_hidden propagated
      through all events that carry them (ENTRY, ZONE_ENTER, ZONE_EXIT, ZONE_DWELL, REENTRY)
  13. [Step 10] Group entry — visitors entering within GROUP_ENTRY_WINDOW_SECONDS on the
      same camera share a stable group_id; group_size set on all members once the window closes
  14. [Step 10] Two-entry-camera deduplication — CAM_ENTRY_1 and CAM_ENTRY_2 both supported;
      cross-camera entry suppression already handled by _active_entries dict
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
    billing_emitted:  bool           = False
    billing_zone_enter_time: Optional[datetime] = None
    # Staff majority vote
    staff_votes:      int            = 0
    total_votes:      int            = 0
    staff_locked:     bool           = False
    pending_entry:    bool           = False
    pending_entry_since: Optional[datetime] = None
    entered_zone_emitted: Optional[str] = None

    # ── [Step 10] Visitor demographics ───────────────────────────────────────
    # These are populated once per track (typically from the first confident
    # detection frame) and then forwarded on every event for this visitor.
    gender_pred:      Optional[str]  = None   # "M" | "F"
    age_pred:         Optional[int]  = None   # predicted age in years
    age_bucket:       Optional[str]  = None   # e.g. "25-34"
    is_face_hidden:   Optional[bool] = None   # True when face obscured

    # ── [Step 10] Group entry ─────────────────────────────────────────────────
    # group_id is assigned by VisitorTracker once the entry-window closes.
    # group_size is backfilled on all members when the group is finalised.
    group_id:         Optional[str]  = None
    group_size:       Optional[int]  = None


@dataclass
class GalleryEntry:
    visitor_id:   str
    store_id:     str
    embeddings:   list
    exit_time:    Optional[datetime]
    last_zone:    Optional[str]
    is_staff:     bool  = False
    staff_locked: bool  = False
    # Demographics carried into Re-ID so a returning visitor keeps their profile
    gender_pred:  Optional[str] = None
    age_pred:     Optional[int] = None
    age_bucket:   Optional[str] = None
    is_face_hidden: Optional[bool] = None


# ─────────────────────────────────────────────────────────────────────────────
# Group-entry window helper
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _PendingGroup:
    """Tracks visitor_ids that crossed the entry threshold close together.

    Lives in VisitorTracker._pending_groups keyed by camera_id.
    Finalised (group_id + group_size assigned) once no new member has
    joined for GROUP_ENTRY_WINDOW_SECONDS.
    """
    camera_id:    str
    visitor_ids:  list[str]        = field(default_factory=list)
    last_join_ts: Optional[datetime] = None
    finalised:    bool             = False


# ─────────────────────────────────────────────────────────────────────────────
# Tracker
# ─────────────────────────────────────────────────────────────────────────────

class VisitorTracker:
    REID_THRESHOLD          = 0.30   # cosine distance — below = same person
    GALLERY_TTL_MINUTES     = 30     # forget visitors after 30 min
    EXIT_GRACE_SECONDS      = 2.0    # missing this long → emit EXIT
    DWELL_EMIT_SECONDS      = 30     # emit ZONE_DWELL every 30s continuous dwell
    ENTRY_THRESHOLD_Y       = 0.45   # normalised Y for threshold crossing
    STAFF_VOTE_THRESHOLD    = 0.64
    STAFF_LOCK_FRAMES       = 11
    ENTRY_STAFF_WAIT_SECONDS = 1.5   # defer ENTRY until staff vote settles
    ABANDON_WINDOW_SECONDS  = 300    # 5 minutes

    # [Step 10] Group entry: visitors crossing within this window share a group_id
    GROUP_ENTRY_WINDOW_SECONDS = 3.0
    # Minimum group size to assign a group_id (solo visitor → group_id=None)
    GROUP_MIN_SIZE = 2

    def __init__(self, store_id: str, layout: dict):
        self.store_id      = store_id
        self.layout        = layout
        self.active_tracks: dict[int, TrackState] = {}
        self.gallery:       list[GalleryEntry]    = []
        self._counter      = 0
        self._group_counter = 0
        # Cross-camera deduplication: visitor_id → camera_id of first active ENTRY
        self._active_entries: dict[str, str] = {}
        # One stable is_staff label per visitor_id
        self._visitor_staff: dict[str, bool] = {}
        # [Step 10] Per-camera pending group windows (camera_id → _PendingGroup)
        self._pending_groups: dict[str, _PendingGroup] = {}
        # [Step 10] visitor_id → TrackState reference for group backfill
        self._visitor_tracks: dict[str, TrackState] = {}

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

    def _new_group_id(self) -> str:
        self._group_counter += 1
        return f"G_{self._group_counter}"

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
        """True if this camera covers the entry threshold.

        Reads is_entry_camera from layout["cameras"][camera_id] when available.
        Both CAM_ENTRY_1 and CAM_ENTRY_2 return True for ST1076.
        Falls back to CAM_3 string check for ST1008 (backwards compat).
        """
        cam_entry = self.layout.get("cameras", {}).get(camera_id, {})
        if cam_entry:
            return bool(cam_entry.get("is_entry_camera", False))
        c = camera_id.upper()
        return "CAM_3" in c or "CAM3" in c

    def _lock_staff(self, track: TrackState, is_staff: bool) -> None:
        track.is_staff = is_staff
        track.staff_locked = True
        self._visitor_staff[track.visitor_id] = is_staff

    def _visitor_is_staff(self, track: TrackState) -> bool:
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

    # ── [Step 10] Demographics helpers ───────────────────────────────────────

    def _update_demographics(self, track: TrackState,
                             gender_pred: Optional[str],
                             age_pred: Optional[int],
                             age_bucket: Optional[str],
                             is_face_hidden: Optional[bool]) -> None:
        """Update demographic fields on a track — first non-None value wins.

        Demographics are set once from the first confident detection and
        then frozen.  This prevents jitter from frame-to-frame variation
        while still capturing the best available reading.
        is_face_hidden is always updated (it changes if the person turns).
        """
        if track.gender_pred is None and gender_pred is not None:
            track.gender_pred = gender_pred
        if track.age_pred is None and age_pred is not None:
            track.age_pred = age_pred
        if track.age_bucket is None and age_bucket is not None:
            track.age_bucket = age_bucket
        # is_face_hidden can flip frame-to-frame; keep latest value
        if is_face_hidden is not None:
            track.is_face_hidden = is_face_hidden

    def _demo_kwargs(self, track: TrackState) -> dict:
        """Return demographics dict ready to unpack into build_event()."""
        return {
            "gender_pred":    track.gender_pred,
            "age_pred":       track.age_pred,
            "age_bucket":     track.age_bucket,
            "is_face_hidden": track.is_face_hidden,
            "group_id":       track.group_id,
            "group_size":     track.group_size,
        }

    # ── [Step 10] Group-entry helpers ─────────────────────────────────────────

    def _register_entry_for_group(self, visitor_id: str,
                                   camera_id: str,
                                   frame_time: datetime) -> None:
        """Add visitor_id to the pending group window for this camera.

        Called when a threshold crossing is detected (crossing == "ENTRY"),
        before the deferred ENTRY event is emitted.  The group window is
        started on the first crossing and extended by each subsequent
        crossing within GROUP_ENTRY_WINDOW_SECONDS.
        """
        pg = self._pending_groups.get(camera_id)
        if pg is None or pg.finalised:
            pg = _PendingGroup(camera_id=camera_id)
            self._pending_groups[camera_id] = pg

        if visitor_id not in pg.visitor_ids:
            pg.visitor_ids.append(visitor_id)
        pg.last_join_ts = frame_time

    def _finalise_groups(self, frame_time: datetime) -> None:
        """Close any pending group windows that have expired.

        Called every frame.  When the window closes:
        - Groups of ≥ GROUP_MIN_SIZE → assign a shared group_id and
          backfill group_size onto all member TrackState objects.
        - Solo "groups" (size 1) → group_id stays None.
        """
        for camera_id, pg in list(self._pending_groups.items()):
            if pg.finalised or pg.last_join_ts is None:
                continue
            elapsed = (frame_time - pg.last_join_ts).total_seconds()
            if elapsed < self.GROUP_ENTRY_WINDOW_SECONDS:
                continue  # window still open

            # Window expired — finalise
            pg.finalised = True
            n = len(pg.visitor_ids)
            if n < self.GROUP_MIN_SIZE:
                # Solo entry — leave group_id as None for all members
                continue

            gid = self._new_group_id()
            for vid in pg.visitor_ids:
                # Find the active track for this visitor_id
                track = self._visitor_tracks.get(vid)
                if track is not None:
                    track.group_id   = gid
                    track.group_size = n

    # ── Zone / entry helpers ──────────────────────────────────────────────────

    def _on_staff_locked(self, track: TrackState, camera_id: str,
                         frame_time: datetime, zone_id: Optional[str],
                         emitter, conf: float) -> None:
        """Emit deferred ZONE_ENTER after label is stable."""
        from pipeline.emit import build_event
        z = zone_id or track.last_zone
        if (z and not self._is_entry_zone(z)
                and track.entered_zone_emitted != z):
            emitter.emit(build_event(
                store_id=self.store_id, camera_id=camera_id,
                visitor_id=track.visitor_id, event_type="ZONE_ENTER",
                timestamp=frame_time, zone_id=z, dwell_ms=0,
                is_staff=self._visitor_is_staff(track), confidence=conf,
                session_seq=track.session_seq,
                **self._demo_kwargs(track),
            ))
            track.session_seq += 1
            track.entered_zone_emitted = z
            track.dwell_start = frame_time
            track.last_dwell_emit = None

    def _is_entry_zone(self, zone_id: Optional[str]) -> bool:
        if not zone_id:
            return False
        return "ENTRY" in zone_id.upper()

    def _check_threshold(self, track: TrackState, cy: float,
                          camera_id: str) -> Optional[str]:
        """Detect ENTRY/EXIT by Y-threshold crossing on entry camera(s).

        Two-entry-camera note: each camera is processed independently via
        separate VisitorTracker instances (one per clip in detect.py), OR
        via the same instance when multiple cameras feed the same tracker.
        Either way, _active_entries cross-camera deduplication prevents
        double-counting — no special handling required here.
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
            # Already entered via another camera — suppress duplicate ENTRY
            track.pending_entry = False
            track.entered = True
            return

        emitter.emit(build_event(
            store_id=self.store_id, camera_id=camera_id,
            visitor_id=track.visitor_id, event_type="ENTRY",
            timestamp=frame_time, zone_id=None, dwell_ms=0,
            is_staff=self._footfall_is_staff(track), confidence=conf,
            session_seq=track.session_seq,
            **self._demo_kwargs(track),
        ))
        track.session_seq += 1
        track.entered = True
        track.pending_entry = False
        self._active_entries[track.visitor_id] = camera_id

    # ── Main update ───────────────────────────────────────────────────────────

    def update(self, track_id: int, cx: float, cy: float,
               box: list, conf: float, is_staff: bool,
               zone_id: Optional[str], frame_time: datetime,
               emitter, frame=None, camera_id: str = "",
               staff_definitive: bool = False,
               zone_meta: dict = None,
               # [Step 10] Demographics — supplied by detect.py per-detection
               gender_pred:    Optional[str]  = None,
               age_pred:       Optional[int]  = None,
               age_bucket:     Optional[str]  = None,
               is_face_hidden: Optional[bool] = None) -> None:
        """Update tracker state for one detection.

        Parameters
        ----------
        gender_pred, age_pred, age_bucket, is_face_hidden
            Per-frame demographic predictions from the demographics model
            (ST1076).  All are None for ST1008 — the tracker handles both
            gracefully; None fields simply do not appear in emitted events.
        zone_meta
            Rich zone metadata dict from ZoneMapper.get_zone_meta().
            Populated for ST1076; empty dict for ST1008.
        """
        from pipeline.emit import build_event
        if zone_meta is None:
            zone_meta = {}

        # ── Finalise any expired group windows ────────────────────────────────
        self._finalise_groups(frame_time)

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
                is_staff   = False,
                last_zone  = None,
                last_pos   = (cx, cy),
                first_seen = frame_time,
                last_seen  = frame_time,
                # Seed demographics from gallery on re-entry
                gender_pred    = match.gender_pred    if match else None,
                age_pred       = match.age_pred       if match else None,
                age_bucket     = match.age_bucket     if match else None,
                is_face_hidden = match.is_face_hidden if match else None,
            )
            track = self.active_tracks[track_id]
            # Keep visitor_id → track reference for group backfill
            self._visitor_tracks[visitor_id] = track

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
                    **self._demo_kwargs(track),
                ))
                track.session_seq += 1
                track.entered = True

        track = self.active_tracks[track_id]
        track.last_seen = frame_time

        # ── Update demographics (first-non-None-wins, except is_face_hidden) ──
        self._update_demographics(track, gender_pred, age_pred,
                                  age_bucket, is_face_hidden)

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
                track.pending_entry      = True
                track.pending_entry_since = frame_time
                # [Step 10] Register in the group-entry window for this camera
                self._register_entry_for_group(track.visitor_id, camera_id, frame_time)

        elif crossing == "EXIT":
            if track.pending_entry:
                self._try_emit_pending_entry(track, camera_id, frame_time, emitter, conf)
            emitter.emit(build_event(
                store_id=self.store_id, camera_id=camera_id,
                visitor_id=track.visitor_id, event_type="EXIT",
                timestamp=frame_time, zone_id=None, dwell_ms=0,
                is_staff=self._footfall_is_staff(track), confidence=conf,
                session_seq=track.session_seq,
                **self._demo_kwargs(track),
            ))
            track.session_seq += 1
            track.exited = True
            self._active_entries.pop(track.visitor_id, None)
            self._archive(track)

        # ── Zone transitions ──────────────────────────────────────────────────
        if zone_id and zone_id != track.last_zone:
            if track.staff_locked:
                if track.last_zone and not self._is_entry_zone(track.last_zone):
                    dwell_ms = int(
                        (frame_time - (track.dwell_start or frame_time)).total_seconds() * 1000
                    )
                    emitter.emit(build_event(
                        store_id=self.store_id, camera_id=camera_id,
                        visitor_id=track.visitor_id, event_type="ZONE_EXIT",
                        timestamp=frame_time, zone_id=track.last_zone, dwell_ms=dwell_ms,
                        is_staff=self._visitor_is_staff(track), confidence=conf,
                        session_seq=track.session_seq,
                        **self._demo_kwargs(track),
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
                                **self._demo_kwargs(track),
                            ))
                            track.session_seq += 1
                        track.billing_zone_enter_time = None
                        track.billing_emitted = False

                if zone_id and not self._is_entry_zone(zone_id):
                    emitter.emit(build_event(
                        store_id=self.store_id, camera_id=camera_id,
                        visitor_id=track.visitor_id, event_type="ZONE_ENTER",
                        timestamp=frame_time, zone_id=zone_id, dwell_ms=0,
                        is_staff=self._visitor_is_staff(track), confidence=conf,
                        session_seq=track.session_seq,
                        zone_meta=zone_meta,
                        **self._demo_kwargs(track),
                    ))
                    track.session_seq += 1
                    track.entered_zone_emitted = zone_id

                track.dwell_start = frame_time
                track.last_dwell_emit = None
                track.billing_emitted = False
            else:
                # Staff label not yet locked — track zone internally
                track.dwell_start = frame_time
                track.last_dwell_emit = None
                track.billing_emitted = False

        # ── Billing queue join ────────────────────────────────────────────────
        if (zone_id and "BILLING" in zone_id.upper()
                and track.staff_locked
                and not self._visitor_is_staff(track)
                and not track.billing_emitted):
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
                    zone_meta=zone_meta,
                    **self._demo_kwargs(track),
                ))
                track.session_seq   += 1
                track.billing_emitted = True
                track.billing_zone_enter_time = frame_time

        track.last_pos  = (cx, cy)
        track.last_zone = zone_id

    # ── Periodic checks ───────────────────────────────────────────────────────

    def check_dwell_events(self, frame_time: datetime, emitter) -> None:
        """Emit ZONE_DWELL every 30 seconds of continuous zone presence."""
        from pipeline.emit import build_event
        # Finalise group windows that may have expired between update() calls
        self._finalise_groups(frame_time)

        for track in list(self.active_tracks.values()):
            if not track.staff_locked or not track.last_zone or not track.dwell_start:
                continue
            if self._is_entry_zone(track.last_zone):
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
                    **self._demo_kwargs(track),
                ))
                track.session_seq    += 1
                track.last_dwell_emit = frame_time

    def check_exits(self, frame_time: datetime, emitter,
                    current_track_ids: set = None) -> None:
        """Emit EXIT for tracks missing > EXIT_GRACE_SECONDS.

        Lower confidence (0.70) on inferred exits.
        Empty store periods (current_track_ids=set()) must not crash.
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
                    confidence=0.70,
                    session_seq=track.session_seq,
                    **self._demo_kwargs(track),
                ))
                track.session_seq += 1
                track.exited = True
                self._active_entries.pop(track.visitor_id, None)
                self._archive(track)
                to_delete.append(tid)
        for tid in to_delete:
            del self.active_tracks[tid]

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _archive(self, track: TrackState) -> None:
        """Move a finished track to the Re-ID gallery."""
        # Clean up visitor_tracks reference
        self._visitor_tracks.pop(track.visitor_id, None)

        self.gallery.append(GalleryEntry(
            visitor_id    = track.visitor_id,
            store_id      = track.store_id,
            embeddings    = track.embeddings[-10:],
            exit_time     = track.last_seen,
            last_zone     = track.last_zone,
            is_staff      = track.is_staff,
            staff_locked  = track.staff_locked,
            # [Step 10] Carry demographics into the gallery for Re-ID reuse
            gender_pred   = track.gender_pred,
            age_pred      = track.age_pred,
            age_bucket    = track.age_bucket,
            is_face_hidden = track.is_face_hidden,
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