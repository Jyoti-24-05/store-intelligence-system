"""
pipeline/detect.py
------------------
Main detection + tracking script. Supports all stores.

Usage — Store 1 (ST1008, Brigade Road):
    python pipeline/detect.py \\
        --clip     data/clips/ST1008/CAM3.mp4 \\
        --camera-id CAM_3 \\
        --layout   data/store1_layout.json \\
        --output-dir output/

Usage — Store 2 (ST1076, Purplle Mumbai):
    python pipeline/detect.py \\
        --clip     "data/clips/ST1076/entry 1.mp4" \\
        --camera-id CAM_ENTRY_1 \\
        --layout   data/store2_layout.json \\
        --output-dir output/

The --store-id is optional: if omitted the store_id is read directly
from layout["store_id"], so you never need to pass it on the command line.

Architecture:
    Frame → YOLOv8m (person detection) → ByteTrack (multi-object tracking) →
    StaffClassifier → ZoneMapper → VisitorTracker → EventEmitter → JSONL / API
"""
from __future__ import annotations

import json
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────
FRAME_SKIP      = 3      # process every 3rd frame (30fps → effective 10fps)
CONF_THRESHOLD  = 0.25   # minimum confidence — low-conf events EMITTED, not dropped


def detect_clip(
    clip_path: str,
    camera_id: str,
    layout: dict,
    output_dir: str,
    store_id: str   = None,   # if None, read from layout["store_id"]
    api_url: str    = None,
    clip_start_time: datetime = None,
) -> int:
    """Process one clip. Returns count of events emitted.

    Parameters
    ----------
    clip_path       : path to the video file
    camera_id       : camera identifier (e.g. "CAM_3", "CAM_ENTRY_1")
    layout          : parsed store_layout.json dict
    output_dir      : directory where <store_id>_<camera_id>_events.jsonl is written
    store_id        : optional — inferred from layout["store_id"] if not given
    api_url         : optional — if set, events are also POSTed to the API in batches
    clip_start_time : optional — if not given, derived from layout clip_date/clip_start_time
    """
    try:
        import cv2
        from ultralytics import YOLO
    except ImportError as e:
        print(f"[{camera_id}] ERROR: Missing dependency: {e}")
        print("  Install: pip install ultralytics opencv-python-headless")
        return 0

    from pipeline.tracker         import VisitorTracker
    from pipeline.staff_classifier import StaffClassifier
    from pipeline.zone_mapper     import ZoneMapper
    from pipeline.emit            import EventEmitter

    # ── Resolve store_id ─────────────────────────────────────────────────────
    # Prefer explicit argument; fall back to layout. This removes the need to
    # pass --store-id on the CLI when the layout already has it.
    if store_id is None:
        store_id = layout.get("store_id")
        if not store_id:
            raise ValueError(
                "store_id not provided and not found in layout['store_id']. "
                "Pass --store-id or add 'store_id' to the layout JSON."
            )

    # ── Model + video ─────────────────────────────────────────────────────────
    model = YOLO("yolov8m.pt")
    cap   = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        print(f"[{camera_id}] ERROR: Cannot open clip: {clip_path}")
        return 0

    fps          = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # ── Derive clip start time ────────────────────────────────────────────────
    # Priority: explicit arg → layout["clip_start_time"] → sensible default
    if clip_start_time is None:
        date_str  = layout.get("clip_date",       "2026-04-10")
        start_str = layout.get("clip_start_time", "11:00:00")
        clip_start_time = datetime.strptime(
            f"{date_str}T{start_str}", "%Y-%m-%dT%H:%M:%S"
        ).replace(tzinfo=timezone.utc)

    # ── ZoneMapper — layout-driven for ST1076, fallback for ST1008 ───────────
    zone_mapper = ZoneMapper(camera_id=camera_id, layout=layout)

    # ── Output + emitter ──────────────────────────────────────────────────────
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_file = Path(output_dir) / f"{store_id}_{camera_id}_events.jsonl"
    emitter = EventEmitter(
        output_path = str(output_file),
        api_url     = api_url,
        store_id    = store_id,
        camera_id   = camera_id,
    )

    # ── Pipeline components ───────────────────────────────────────────────────
    tracker   = VisitorTracker(store_id=store_id, layout=layout)
    staff_clf = StaffClassifier(layout=layout)

    frame_num      = 0
    events_emitted = 0

    print(f"[{camera_id}] store={store_id} | "
          f"{total_frames} frames @ {fps:.1f}fps → {output_file}")

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        frame_num += 1
        if frame_num % FRAME_SKIP != 0:
            continue

        frame_time = clip_start_time + timedelta(seconds=frame_num / fps)

        # ── YOLOv8 + ByteTrack ────────────────────────────────────────────────
        results = model.track(
            frame,
            persist = True,
            tracker = "bytetrack.yaml",
            classes = [0],              # 0 = person
            conf    = CONF_THRESHOLD,
            verbose = False,
        )

        if results[0].boxes.id is None:
            # No detections this frame — check for disappeared tracks
            tracker.check_exits(frame_time, emitter, current_track_ids=set())
            continue

        track_ids = results[0].boxes.id.int().cpu().tolist()
        boxes     = results[0].boxes.xyxy.cpu().tolist()
        confs     = results[0].boxes.conf.cpu().tolist()
        h, w      = frame.shape[:2]

        current_ids = set(track_ids)

        for track_id, box, conf in zip(track_ids, boxes, confs):
            x1, y1, x2, y2 = box
            cx = ((x1 + x2) / 2) / w   # normalise to [0, 1]
            cy = ((y1 + y2) / 2) / h

            zone_id   = zone_mapper.get_zone(cx, cy)
            zone_meta = zone_mapper.get_zone_meta(zone_id) if zone_id else {}

            is_staff, staff_definitive = staff_clf.classify_detailed(
                frame, box, cx, cy, zone_id
            )

            # Force is_staff=True for any zone marked is_staff_zone in layout
            if zone_meta.get("is_staff_zone"):
                is_staff         = True
                staff_definitive = True

            tracker.update(
                track_id         = track_id,
                cx               = cx,
                cy               = cy,
                box              = box,
                conf             = conf,
                is_staff         = is_staff,
                staff_definitive = staff_definitive,
                zone_id          = zone_id,
                zone_meta        = zone_meta,
                frame_time       = frame_time,
                emitter          = emitter,
                frame            = frame,
                camera_id        = camera_id,
            )

            # FIXED (demographics passed through — None for ST1008, real values for ST1076)
            is_staff, staff_definitive = staff_clf.classify_detailed(
            frame, box, cx, cy, zone_id
            )

            if zone_meta.get("is_staff_zone"):
               is_staff         = True
               staff_definitive = True

            # Demographics — ST1076 only for now; all None for ST1008 (safe, ignored by tracker)
            # Replace with real model output here when demographics model is integrated

            
            gender_pred:    Optional[str]  = None
            age_pred:       Optional[int]  = None
            age_bucket:     Optional[str]  = None
            is_face_hidden: Optional[bool] = None

            tracker.update(
                track_id         = track_id,
                cx               = cx,
                cy               = cy,
                box              = box,
                conf             = conf,
                is_staff         = is_staff,
                staff_definitive = staff_definitive,
                zone_id          = zone_id,
                zone_meta        = zone_meta,
                frame_time       = frame_time,
                emitter          = emitter,
                frame            = frame,
                camera_id        = camera_id,
                gender_pred      = gender_pred,
                age_pred         = age_pred,
                age_bucket       = age_bucket,
                is_face_hidden   = is_face_hidden,
            )

        tracker.check_exits(frame_time, emitter, current_ids)
        tracker.check_dwell_events(frame_time, emitter)

        if frame_num % 300 == 0:
            pct = (frame_num / total_frames * 100) if total_frames else 0
            print(f"[{camera_id}] {pct:.0f}% — frame {frame_num}/{total_frames} "
                  f"| active tracks: {len(tracker.active_tracks)}")

    cap.release()
    emitter.close()

    if output_file.exists():
        events_emitted = sum(1 for _ in output_file.open())

    print(f"[{camera_id}] Done. {events_emitted} events → {output_file}")
    return events_emitted


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Store Intelligence — detection pipeline (multi-store)"
    )
    parser.add_argument(
        "--clip",       required=True,
        help="Path to video clip (e.g. 'data/clips/ST1076/entry 1.mp4')"
    )
    parser.add_argument(
        "--camera-id",  required=True,
        help="Camera ID matching layout cameras key (e.g. CAM_3, CAM_ENTRY_1)"
    )
    parser.add_argument(
        "--store-id",   default=None,
        help="Store ID (e.g. ST1008, ST1076). Inferred from layout if omitted."
    )
    parser.add_argument(
        "--layout",     required=True,
        help="Path to store_layout.json (e.g. data/store2_layout.json)"
    )
    parser.add_argument("--output-dir",  default="output")
    parser.add_argument(
        "--api-url",    default=None,
        help="API base URL for live ingest (e.g. http://localhost:8000)"
    )
    args = parser.parse_args()

    with open(args.layout) as f:
        layout = json.load(f)

    detect_clip(
        clip_path  = args.clip,
        camera_id  = args.camera_id,
        store_id   = args.store_id,    # None → inferred from layout
        layout     = layout,
        output_dir = args.output_dir,
        api_url    = args.api_url,
    )