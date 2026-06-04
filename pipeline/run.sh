#!/bin/bash
# pipeline/run.sh
# ---------------
# Processes CCTV clips for any store (ST1008 or ST1076/Store 2).
#
# Usage:
#   Store 1 (batch):   bash pipeline/run.sh --store 1 --clips data/clips/ST1008
#   Store 2 (batch):   bash pipeline/run.sh --store 2 --clips "data/clips/Store 2"
#   API ingest mode:   bash pipeline/run.sh --store 1 --clips data/clips/ST1008 --api-mode
#   Single camera:     bash pipeline/run.sh --store 1 --clips data/clips/ST1008 --camera CAM_3
#
# Environment variables:
#   API_URL        (default: http://localhost:8000)
#   OUTPUT_DIR     (default: output)

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
STORE_NUM=""
CLIPS_DIR=""
API_MODE=""
SINGLE_CAM=""
API_URL="${API_URL:-http://localhost:8000}"
OUTPUT_DIR="${OUTPUT_DIR:-output}"

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --store)    STORE_NUM="$2";   shift 2 ;;
        --clips)    CLIPS_DIR="$2";   shift 2 ;;
        --api-mode) API_MODE="true";  shift   ;;
        --camera)   SINGLE_CAM="$2";  shift 2 ;;
        --api-url)  API_URL="$2";     shift 2 ;;
        --output)   OUTPUT_DIR="$2";  shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

# ── Validate required args ────────────────────────────────────────────────────
if [[ -z "$STORE_NUM" ]]; then
    echo "ERROR: --store is required (1 or 2)"
    echo "Usage: bash pipeline/run.sh --store 1 --clips data/clips/ST1008"
    exit 1
fi
if [[ -z "$CLIPS_DIR" ]]; then
    echo "ERROR: --clips is required"
    echo "Usage: bash pipeline/run.sh --store 1 --clips data/clips/ST1008"
    exit 1
fi
if [[ ! -d "$CLIPS_DIR" ]]; then
    echo "ERROR: Clips directory not found: $CLIPS_DIR"
    exit 1
fi

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# ── Store-specific config ─────────────────────────────────────────────────────
if [[ "$STORE_NUM" == "1" ]]; then
    STORE_ID="ST1008"
    LAYOUT="data/store1_layout.json"
    RAW_POS_CSV="data/Brigade_Bangalore_10_April_26 (1)bc6219c.csv"
    PROCESSED_POS_CSV="data/pos_transactions_ST1008.csv"

    # Store 1: one entry camera, two product cameras, one billing camera
    # Map: "filename fragment" → "camera_id"
    declare -A CAM_MAP=(
        ["CAM 1"]="CAM_1"   ["cam1"]="CAM_1"
        ["CAM 2"]="CAM_2"   ["cam2"]="CAM_2"
        ["CAM 3"]="CAM_3"   ["cam3"]="CAM_3"
        ["CAM 4"]="CAM_4"   ["cam4"]="CAM_4"
        ["CAM 5"]="CAM_5"   ["cam5"]="CAM_5"
    )

elif [[ "$STORE_NUM" == "2" ]]; then
    STORE_ID="ST1076"
    LAYOUT="data/store2_layout.json"
    RAW_POS_CSV=""
    PROCESSED_POS_CSV=""

    # Store 2: TWO entry cameras, one zone camera, one billing camera
    # Filenames: "entry 1.mp4", "entry 2.mp4", "zone.mp4", "billing_area.mp4"
    declare -A CAM_MAP=(
        ["entry 1"]="CAM_ENTRY_1"
        ["entry1"]="CAM_ENTRY_1"
        ["entry 2"]="CAM_ENTRY_2"
        ["entry2"]="CAM_ENTRY_2"
        ["zone"]="CAM_ZONE"
        ["billing_area"]="CAM_BILLING"
        ["billing"]="CAM_BILLING"
    )

else
    echo "ERROR: --store must be 1 or 2 (got: $STORE_NUM)"
    exit 1
fi

log "Store: $STORE_ID | Layout: $LAYOUT | Clips: $CLIPS_DIR"
mkdir -p "$OUTPUT_DIR"

# ── Step 0: Validate layout file exists ──────────────────────────────────────
if [[ ! -f "$LAYOUT" ]]; then
    echo "ERROR: Layout file not found: $LAYOUT"
    echo "Expected at: $LAYOUT"
    exit 1
fi

# ── Step 1: Preprocess POS CSV ────────────────────────────────────────────────
log "Preprocessing POS transactions for $STORE_ID..."
if [[ -f "$RAW_POS_CSV" ]]; then
    python pipeline/preprocess_pos.py \
        --input  "$RAW_POS_CSV" \
        --output "$PROCESSED_POS_CSV" \
        --store-id "$STORE_ID"
    log "POS preprocessing done → $PROCESSED_POS_CSV"
else
    log "WARNING: Raw POS file not found at $RAW_POS_CSV — skipping POS preprocessing"
fi

# ── Step 2: Process each clip ─────────────────────────────────────────────────
processed=0
failed=0

for clip in "$CLIPS_DIR"/*.mp4 "$CLIPS_DIR"/*.avi "$CLIPS_DIR"/*.MOV "$CLIPS_DIR"/*.mov; do
    [ -f "$clip" ] || continue

    filename=$(basename "$clip" | sed 's/\.[^.]*$//')
    camera_id="CAM_UNKNOWN"

    # Match filename fragment → camera_id (longest match wins)
    best_len=0
    for key in "${!CAM_MAP[@]}"; do
        # Case-insensitive substring match
        filename_lower="${filename,,}"
        key_lower="${key,,}"
        if [[ "$filename_lower" == *"$key_lower"* ]]; then
            key_len=${#key}
            if (( key_len > best_len )); then
                best_len=$key_len
                camera_id="${CAM_MAP[$key]}"
            fi
        fi
    done

    if [[ "$camera_id" == "CAM_UNKNOWN" ]]; then
        log "WARNING: Could not map '$filename' to any camera — skipping"
        continue
    fi

    # Skip if single-camera mode and this isn't the target
    if [[ -n "$SINGLE_CAM" && "$camera_id" != "$SINGLE_CAM" ]]; then
        continue
    fi

    log "Processing: '$filename' → $camera_id (store=$STORE_ID)"

    if python pipeline/detect.py \
        --clip       "$clip" \
        --camera-id  "$camera_id" \
        --store-id   "$STORE_ID" \
        --layout     "$LAYOUT" \
        --output-dir "$OUTPUT_DIR" \
        ${API_MODE:+--api-url "$API_URL"}; then
        log "Done: $camera_id"
        (( processed++ )) || true
    else
        log "ERROR: detect.py failed for $camera_id (clip: $clip)"
        (( failed++ )) || true
    fi
done

# ── Step 3: Guard: at least one clip processed ────────────────────────────────
if [[ "$processed" -eq 0 ]]; then
    log "ERROR: No clips were processed from $CLIPS_DIR"
    log "Files found:"
    ls -1 "$CLIPS_DIR" 2>/dev/null || echo "  (empty directory)"
    exit 1
fi

# ── Step 4: Merge all JSONL output files for this store ──────────────────────
MERGED_FILE="$OUTPUT_DIR/${STORE_ID}_all_events.jsonl"
log "Merging event streams → $MERGED_FILE"

# Collect all JSONL files for this store, sort by camera for deterministic order
shopt -s nullglob
jsonl_files=("$OUTPUT_DIR/${STORE_ID}_CAM_"*.jsonl \
             "$OUTPUT_DIR/${STORE_ID}_CAM_ENTRY_"*.jsonl \
             "$OUTPUT_DIR/${STORE_ID}_CAM_ZONE"*.jsonl \
             "$OUTPUT_DIR/${STORE_ID}_CAM_BILLING"*.jsonl)
shopt -u nullglob

if [[ ${#jsonl_files[@]} -eq 0 ]]; then
    # Fallback: pick up any JSONL for this store
    mapfile -t jsonl_files < <(find "$OUTPUT_DIR" -name "${STORE_ID}_*.jsonl" | sort)
fi

if [[ ${#jsonl_files[@]} -gt 0 ]]; then
    cat "${jsonl_files[@]}" > "$MERGED_FILE"
    EVENT_COUNT=$(wc -l < "$MERGED_FILE")
    log "Pipeline complete."
    log "  Clips processed : $processed"
    log "  Clips failed    : $failed"
    log "  Total events    : $EVENT_COUNT"
    log "  Output file     : $MERGED_FILE"
else
    log "WARNING: No JSONL files found in $OUTPUT_DIR for $STORE_ID"
    EVENT_COUNT=0
fi

# ── Step 5: Ingest into API (if --api-mode) ───────────────────────────────────
if [[ -n "$API_MODE" && -f "$MERGED_FILE" && "$EVENT_COUNT" -gt 0 ]]; then
    log "Ingesting $EVENT_COUNT events into API at $API_URL ..."
    python3 - << PYEOF
import json, httpx, sys

merged_file = "$MERGED_FILE"
api_url     = "$API_URL"
batch_size  = 500

events = []
with open(merged_file) as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  WARN: Skipping malformed line: {e}")

if not events:
    print("No valid events to ingest.")
    sys.exit(0)

total_ingested = 0
total_dupes    = 0
total_failed   = 0

for i in range(0, len(events), batch_size):
    batch = events[i:i+batch_size]
    batch_num = i // batch_size + 1
    try:
        resp = httpx.post(
            f"{api_url}/events/ingest",
            json={"events": batch},
            timeout=60.0,
        )
        resp.raise_for_status()
        d = resp.json()
        total_ingested += d.get("ingested", 0)
        total_dupes    += d.get("duplicate_skipped", 0)
        total_failed   += d.get("failed", 0)
        print(f"  Batch {batch_num}: ingested={d['ingested']} "
              f"dupes={d['duplicate_skipped']} failed={d['failed']}")
        if d.get("errors"):
            for err in d["errors"][:3]:
                print(f"    ERROR: {err}")
    except httpx.HTTPStatusError as e:
        print(f"  Batch {batch_num} HTTP error: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        print(f"  Batch {batch_num} failed: {e}")

print(f"Ingest complete: ingested={total_ingested} dupes={total_dupes} failed={total_failed}")
PYEOF
    log "Ingest done."
fi

log "All done for $STORE_ID."