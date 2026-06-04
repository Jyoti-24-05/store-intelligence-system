import json, sys

path = '/app/output/ST1008_CAM_3_events.jsonl'
try:
    with open(path) as f:
        lines = f.readlines()
    print(f'Total lines: {len(lines)}')
    for line in lines[:5]:
        print(json.loads(line))
except FileNotFoundError:
    print('File empty or missing - CAM_3 produced no events at all')
    print('This means either:')
    print('  1. No persons detected in clip')
    print('  2. Persons detected but threshold never crossed')
