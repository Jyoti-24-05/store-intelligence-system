"""
pipeline/convert_to_submission_format.py
----------------------------------------
Converts internal pipeline JSONL output → Purplle submission schema.

Usage:
    python pipeline/convert_to_submission_format.py \
        --input  output/ST1008_all_events.jsonl \
        --output output/ST1008_submission_events.jsonl

    python pipeline/convert_to_submission_format.py \
        --input  output/ST1076_all_events.jsonl \
        --output output/ST1076_submission_events.jsonl
"""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path


def convert_event(e: dict) -> dict | list[dict]:
    """
    Map one internal event → one or more Purplle-schema events.

    Internal event_type  →  Purplle event_type
    ENTRY                →  entry
    EXIT                 →  exit
    ZONE_ENTER           →  zone_entered
    ZONE_EXIT            →  zone_exited
    ZONE_DWELL           →  zone_entered  (dwell treated as enriched zone event)
    BILLING_QUEUE_JOIN   →  queue_completed (partial — no exit ts yet)
    BILLING_QUEUE_ABANDON→  queue_abandoned
    REENTRY              →  entry  (with reentry flag in metadata)
    """
    etype      = e.get("event_type", "")
    visitor_id = e.get("visitor_id", "")
    store_id   = e.get("store_id", "")
    camera_id  = e.get("camera_id", "")
    timestamp  = e.get("timestamp", "")
    is_staff   = e.get("is_staff", False)
    meta       = e.get("metadata", {})

    # ── Shared demographic fields (ST1076 only, null for ST1008) ─────────────
    gender = meta.get("gender_pred")
    age    = meta.get("age_pred")
    bucket = meta.get("age_bucket")

    # ── ENTRY / REENTRY → entry ───────────────────────────────────────────────
    if etype in ("ENTRY", "REENTRY"):
        return {
            "event_type":       "entry",
            "id_token":         visitor_id,
            "store_code":       store_id,
            "camera_id":        camera_id,
            "event_timestamp":  timestamp,
            "is_staff":         is_staff,
            "gender_pred":      gender,
            "age_pred":         age,
            "age_bucket":       bucket,
            "is_face_hidden":   meta.get("is_face_hidden"),
            "group_id":         meta.get("group_id"),
            "group_size":       meta.get("group_size"),
            "reentry":          etype == "REENTRY",
        }

    # ── EXIT → exit ───────────────────────────────────────────────────────────
    if etype == "EXIT":
        return {
            "event_type":       "exit",
            "id_token":         visitor_id,
            "store_code":       store_id,
            "camera_id":        camera_id,
            "event_timestamp":  timestamp,
            "is_staff":         is_staff,
            "gender_pred":      gender,
            "age_pred":         age,
            "age_bucket":       bucket,
        }

    # ── ZONE_ENTER / ZONE_EXIT / ZONE_DWELL → zone_entered / zone_exited ─────
    if etype in ("ZONE_ENTER", "ZONE_DWELL"):
        return {
            "event_type":       "zone_entered",
            "track_id":         visitor_id,
            "store_id":         store_id,
            "camera_id":        camera_id,
            "zone_id":          e.get("zone_id"),
            "zone_name":        meta.get("zone_name") or e.get("zone_id"),
            "zone_type":        meta.get("zone_type", "SHELF"),
            "is_revenue_zone":  "Yes" if meta.get("is_revenue_zone") else "No",
            "event_time":       timestamp,
            "zone_hotspot_x":   meta.get("zone_hotspot_x"),
            "zone_hotspot_y":   meta.get("zone_hotspot_y"),
            "gender":           gender,
            "age":              age,
            "age_bucket":       bucket,
            "dwell_ms":         e.get("dwell_ms", 0),
        }

    if etype == "ZONE_EXIT":
        return {
            "event_type":       "zone_exited",
            "track_id":         visitor_id,
            "store_id":         store_id,
            "camera_id":        camera_id,
            "zone_id":          e.get("zone_id"),
            "zone_name":        meta.get("zone_name") or e.get("zone_id"),
            "zone_type":        meta.get("zone_type", "SHELF"),
            "is_revenue_zone":  "Yes" if meta.get("is_revenue_zone") else "No",
            "event_time":       timestamp,
            "zone_hotspot_x":   meta.get("zone_hotspot_x"),
            "zone_hotspot_y":   meta.get("zone_hotspot_y"),
            "gender":           gender,
            "age":              age,
            "age_bucket":       bucket,
            "dwell_ms":         e.get("dwell_ms", 0),
        }

    # ── BILLING_QUEUE_JOIN → queue_completed ──────────────────────────────────
    if etype == "BILLING_QUEUE_JOIN":
        qt = meta.get("queue_timing") or {}
        return {
            "event_type":               "queue_completed",
            "queue_event_id":           str(uuid.uuid4()),
            "track_id":                 visitor_id,
            "store_id":                 store_id,
            "camera_id":                camera_id,
            "queue_join_ts":            qt.get("queue_join_ts") or timestamp,
            "queue_served_ts":          qt.get("queue_served_ts"),
            "queue_exit_ts":            qt.get("queue_exit_ts"),
            "queue_position_at_join":   meta.get("queue_depth"),
            "wait_seconds":             qt.get("wait_seconds"),
            "abandoned":                False,
            "gender":                   gender,
            "age":                      age,
            "age_bucket":               bucket,
        }

    # ── BILLING_QUEUE_ABANDON → queue_abandoned ───────────────────────────────
    if etype == "BILLING_QUEUE_ABANDON":
        qt = meta.get("queue_timing") or {}
        return {
            "event_type":               "queue_abandoned",
            "queue_event_id":           str(uuid.uuid4()),
            "track_id":                 visitor_id,
            "store_id":                 store_id,
            "camera_id":                camera_id,
            "queue_join_ts":            qt.get("queue_join_ts") or timestamp,
            "queue_served_ts":          None,
            "queue_exit_ts":            timestamp,
            "queue_position_at_join":   meta.get("queue_depth"),
            "wait_seconds":             qt.get("wait_seconds"),
            "abandoned":                True,
            "gender":                   gender,
            "age":                      age,
            "age_bucket":               bucket,
        }

    # ── Unknown type — pass through as-is with a note ────────────────────────
    return {**e, "_conversion_note": f"unmapped_type_{etype}"}


def convert_file(input_path: str, output_path: str) -> tuple[int, int]:
    """Convert input JSONL → output JSONL. Returns (converted, skipped)."""
    converted = 0
    skipped   = 0

    with open(input_path, encoding="utf-16") as fin, \
     open(output_path, "w", encoding="utf-8") as fout:

        for line_num, line in enumerate(fin, 1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                result = convert_event(event)
                # convert_event may return a single dict or a list
                if isinstance(result, list):
                    for r in result:
                        fout.write(json.dumps(r) + "\n")
                        converted += 1
                else:
                    fout.write(json.dumps(result) + "\n")
                    converted += 1
            except Exception as exc:
                print(f"  WARN line {line_num}: {exc}")
                skipped += 1

    return converted, skipped


def validate_output(output_path: str) -> tuple[int, int]:
    """Quick validation — every line must be valid JSON with event_type."""
    valid   = 0
    invalid = 0
    with open(output_path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "event_type" not in obj:
                    print(f"  INVALID line {line_num}: missing event_type")
                    invalid += 1
                else:
                    valid += 1
            except json.JSONDecodeError as e:
                print(f"  INVALID line {line_num}: {e}")
                invalid += 1
    return valid, invalid


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert internal JSONL → Purplle submission schema"
    )
    parser.add_argument("--input",  required=True, help="Internal events JSONL file")
    parser.add_argument("--output", required=True, help="Submission JSONL output path")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"ERROR: Input file not found: {args.input}")
        exit(1)

    print(f"Converting: {args.input} → {args.output}")
    converted, skipped = convert_file(args.input, args.output)
    print(f"Converted: {converted} events  |  Skipped: {skipped}")

    print(f"Validating: {args.output}")
    valid, invalid = validate_output(args.output)
    print(f"Valid: {valid}  |  Invalid: {invalid}")

    if invalid == 0:
        print("✓ Output is valid JSONL — ready for submission")
    else:
        print("✗ Fix invalid lines before submitting")