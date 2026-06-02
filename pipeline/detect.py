"""
pipeline/detect.py
------------------
Main detection + tracking script for Brigade Road Purplle store (ST1008).

Usage:
    python pipeline/detect.py \\
        --clip data/clips/CAM3.mp4 \\
        --camera-id CAM_3 \\
        --store-id ST1008 \\
        --layout data/store_layout.json \\
        --output-dir output/ \\
        [--api-url http://localhost:8000]

Architecture:
    Frame → YOLOv8m (person detection) → ByteTrack (tracking) →
    StaffClassifier → ZoneMapper → VisitorTracker → EventEmitter → JSONL / API
"""
from __future__ import annotations

import json
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────
FRAME_SKIP      = 3      # process every 3rd frame (30fps → effective 10fps)
CONF_THRESHOLD  = 0.25   # minimum confidence — low-conf events EMITTED, not dropped


def detect_clip(
    clip_path: str,
    camera_id: str,
    store_id: str,
    layout: dict,
    output_dir: str,
    api_url: str = None,
    clip_start_time: datetime = None,
) -> int:
    """Process one clip. Returns count of events emitted."""
    try:
        import cv2
        from ultralytics import YOLO
    except ImportError as e:
        print(f"[{camera_id}] ERROR: Missing dependency: {e}")
        print("  Install: pip install ultralytics opencv-python-headless")
        return 0

    from pipeline.tracker        import VisitorTracker
    from pipeline.staff_classifier import StaffClassifier
    from pipeline.zone_mapper    import ZoneMapper
    from pipeline.emit           import EventEmitter

    # ── Model + video ─────────────────────────────────────────────────────────
    model = YOLO("yolov8m.pt")
    cap   = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        print(f"[{camera_id}] ERROR: Cannot open clip: {clip_path}")
        return 0

    fps          = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # ── Derive clip start time ────────────────────────────────────────────────
    if clip_start_time is None:
        date_str  = layout.get("clip_date",       "2026-04-10")
        start_str = layout.get("clip_start_time", "11:00:00")
        clip_start_time = datetime.strptime(
            f"{date_str}T{start_str}", "%Y-%m-%dT%H:%M:%S"
        ).replace(tzinfo=timezone.utc)

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
    tracker    = VisitorTracker(store_id=store_id, layout=layout)
    staff_clf  = StaffClassifier(layout=layout)
    zone_mapper= ZoneMapper(camera_id=camera_id, layout=layout)

    frame_num    = 0
    events_emitted = 0

    print(f"[{camera_id}] Processing {total_frames} frames @ {fps:.1f}fps → {output_file}")

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
            persist    = True,
            tracker    = "bytetrack.yaml",
            classes    = [0],           # 0 = person
            conf       = CONF_THRESHOLD,
            verbose    = False,
        )

        if results[0].boxes.id is None:
            # No detections — check for disappeared tracks
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

            zone_id  = zone_mapper.get_zone(cx, cy)
            is_staff, staff_definitive = staff_clf.classify_detailed(
                frame, box, cx, cy, zone_id
            )

            tracker.update(
                track_id  = track_id,
                cx        = cx,
                cy        = cy,
                box       = box,
                conf      = conf,
                is_staff  = is_staff,
                staff_definitive=staff_definitive,
                zone_id   = zone_id,
                frame_time= frame_time,
                emitter   = emitter,
                frame     = frame,
                camera_id = camera_id,
            )

        tracker.check_exits(frame_time, emitter, current_ids)
        tracker.check_dwell_events(frame_time, emitter)

        if frame_num % 300 == 0:
            pct = (frame_num / total_frames * 100) if total_frames else 0
            print(f"[{camera_id}] {pct:.0f}% — frame {frame_num}/{total_frames} "
                  f"| active tracks: {len(tracker.active_tracks)}")

    cap.release()
    emitter.close()

    # Count emitted events
    if output_file.exists():
        events_emitted = sum(1 for _ in output_file.open())

    print(f"[{camera_id}] Done. {events_emitted} events → {output_file}")
    return events_emitted


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Store Intelligence — detection pipeline"
    )
    parser.add_argument("--clip",        required=True,  help="Path to video clip")
    parser.add_argument("--camera-id",   required=True,  help="Camera ID (CAM_1..CAM_5)")
    parser.add_argument("--store-id",    default="ST1008")
    parser.add_argument("--layout",      required=True,  help="Path to store_layout.json")
    parser.add_argument("--output-dir",  default="output")
    parser.add_argument("--api-url",     default=None,   help="API base URL for live ingest")
    args = parser.parse_args()

    with open(args.layout) as f:
        layout = json.load(f)

    detect_clip(
        clip_path  = args.clip,
        camera_id  = args.camera_id,
        store_id   = args.store_id,
        layout     = layout,
        output_dir = args.output_dir,
        api_url    = args.api_url,
    )