"""
Quick QA on merged pipeline output (all_events.jsonl).

Usage:
    python pipeline/validate_events.py output/all_events.jsonl
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def validate(path: str) -> int:
    p = Path(path)
    if not p.exists():
        print(f"File not found: {path}")
        return 1

    total = staff_events = 0
    by_visitor_staff: dict[str, set[bool]] = defaultdict(set)
    entry_exit: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    event_types: Counter = Counter()

    with p.open() as f:
        for line in f:
            e = json.loads(line)
            total += 1
            event_types[e["event_type"]] += 1
            if e.get("is_staff"):
                staff_events += 1
            vid = e["visitor_id"]
            by_visitor_staff[vid].add(bool(e.get("is_staff")))
            if e["event_type"] in ("ENTRY", "EXIT"):
                entry_exit[vid].append((e["event_type"], bool(e.get("is_staff"))))

    mixed = [v for v, flags in by_visitor_staff.items() if len(flags) > 1]
    footfall_mismatch = [
        v for v, pairs in entry_exit.items()
        if len(pairs) >= 2 and len({b for _, b in pairs}) > 1
    ]

    print(f"Total events       = {total}")
    pct = staff_events / total * 100 if total else 0
    print(f"Staff events       = {staff_events} ({pct:.2f}%)")
    print(f"Customer events    = {total - staff_events}")
    staff_visitors = sum(1 for flags in by_visitor_staff.values() if True in flags)
    print(f"Visitors w/ staff tag = {staff_visitors} / {len(by_visitor_staff)}")
    if pct > 26:
        print("  WARN: staff % high for ~5 staff — try tighter thresholds or staff gallery")
    elif pct < 8:
        print("  WARN: staff % low — check CAM_4 / uniform lighting")
    else:
        print("  OK: staff % in expected band (~10–25% for 5 staff on floor)")
    print(f"Unique visitors    = {len(by_visitor_staff)}")
    print(f"Event types        = {dict(event_types)}")
    print(f"Mixed is_staff     = {len(mixed)} visitors")
    if mixed[:10]:
        print(f"  examples: {mixed[:10]}")
    print(f"ENTRY/EXIT mismatch= {len(footfall_mismatch)} visitors")
    if footfall_mismatch[:10]:
        print(f"  examples: {footfall_mismatch[:10]}")

    rc = 0
    if footfall_mismatch:
        rc = 1
    if mixed:
        print("  FAIL: mixed is_staff must be 0 after visitor-level labels")
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(validate(sys.argv[1] if len(sys.argv) > 1 else "output/all_events.jsonl"))
