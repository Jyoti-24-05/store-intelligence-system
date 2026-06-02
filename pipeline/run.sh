#!/bin/bash
# pipeline/run.sh
# ---------------
# Processes all CCTV clips for ST1008 Brigade Road.
#
# Usage:
#   Batch mode (output only):  bash pipeline/run.sh data/clips data/store_layout.json
#   API ingest mode:           bash pipeline/run.sh data/clips data/store_layout.json --api-mode
#   Single camera:             bash pipeline/run.sh data/clips data/store_layout.json "" CAM_3
#
# Environment variables:
#   API_URL        (default: http://localhost:8000)
#   OUTPUT_DIR     (default: output)

set -euo pipefail

CLIPS_DIR=${1:?"Usage: run.sh <clips_dir> <layout_json>"}
LAYOUT=${2:?"Usage: run.sh <clips_dir> <layout_json>"}
API_MODE=${3:-""}
SINGLE_CAM=${4:-""}
API_URL=${API_URL:-"http://localhost:8000"}
OUTPUT_DIR=${OUTPUT_DIR:-"output"}

mkdir -p "$OUTPUT_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# ── Step 0: Preprocess POS CSV ────────────────────────────────────────────────
log "Preprocessing POS transactions..."
python pipeline/preprocess_pos.py \
    --input  "data/Brigade_Bangalore_10_April_26 (1)bc6219c.csv" \
    --output data/pos_transactions.csv
log "POS preprocessing done."

# ── Camera filename → ID map ──────────────────────────────────────────────────
declare -A CAM_MAP=(
    ["CAM 1"]="CAM_1" ["cam1"]="CAM_1"
    ["CAM 2"]="CAM_2" ["cam2"]="CAM_2"
    ["CAM 3"]="CAM_3" ["cam3"]="CAM_3"
    ["CAM 4"]="CAM_4" ["cam4"]="CAM_4"
    ["CAM 5"]="CAM_5" ["cam5"]="CAM_5"
)

# ── Process each clip ─────────────────────────────────────────────────────────
processed=0
for clip in "$CLIPS_DIR"/*.mp4 "$CLIPS_DIR"/*.avi "$CLIPS_DIR"/*.MOV "$CLIPS_DIR"/*.mov; do
    [ -f "$clip" ] || continue

    filename=$(basename "$clip" | sed 's/\.[^.]*$//')
    camera_id="CAM_UNKNOWN"
    for key in "${!CAM_MAP[@]}"; do
        if [[ "$filename" == *"$key"* ]]; then
            camera_id="${CAM_MAP[$key]}"
            break
        fi
    done

    # Skip if single-camera mode and this isn't the target
    [ -n "$SINGLE_CAM" ] && [ "$camera_id" != "$SINGLE_CAM" ] && continue

    log "Processing $filename → $camera_id"

    python pipeline/detect.py \
        --clip       "$clip" \
        --camera-id  "$camera_id" \
        --store-id   "ST1008" \
        --layout     "$LAYOUT" \
        --output-dir "$OUTPUT_DIR" \
        ${API_MODE:+--api-url "$API_URL"}

    log "Done: $camera_id"
    (( processed++ )) || true
done

if [ "$processed" -eq 0 ]; then
    log "WARNING: No clips found in $CLIPS_DIR (looked for *.mp4 *.avi *.MOV *.mov)"
    exit 1
fi

# ── Merge all JSONL files ─────────────────────────────────────────────────────
log "Merging event streams..."
cat "$OUTPUT_DIR"/ST1008_CAM_*.jsonl > "$OUTPUT_DIR/all_events.jsonl" 2>/dev/null || true
EVENT_COUNT=$(wc -l < "$OUTPUT_DIR/all_events.jsonl" 2>/dev/null || echo 0)
log "Pipeline complete. Total events: $EVENT_COUNT"
log "Events at: $OUTPUT_DIR/all_events.jsonl"

# ── Ingest merged events into API if in API mode ──────────────────────────────
if [ -n "$API_MODE" ]; then
    log "Ingesting merged events into API at $API_URL..."
    python3 -c "
import json, httpx, sys
events = [json.loads(l) for l in open('$OUTPUT_DIR/all_events.jsonl')]
batch_size = 500
total_ingested = 0
for i in range(0, len(events), batch_size):
    batch = events[i:i+batch_size]
    resp = httpx.post('$API_URL/events/ingest', json={'events': batch}, timeout=30)
    resp.raise_for_status()
    d = resp.json()
    total_ingested += d.get('ingested', 0)
    print(f'Batch {i//batch_size+1}: ingested={d[\"ingested\"]} dupes={d[\"duplicate_skipped\"]}')
print(f'Total ingested: {total_ingested}')
"
    log "Ingest complete."
fi