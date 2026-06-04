import json

with open('/app/data/store1_layout.json') as f:
    l1 = json.load(f)
print('=== STORE 1 ===')
print('store_id:', l1.get('store_id'))
print('clip_date:', l1.get('clip_date'))
print('clip_start_time:', l1.get('clip_start_time'))
print('cameras:', list(l1.get('cameras', {}).keys()))
for cid, cam in l1.get('cameras', {}).items():
    print(f'  {cid}: is_entry_camera={cam.get("is_entry_camera")}')

with open('/app/data/store2_layout.json') as f:
    l2 = json.load(f)
print()
print('=== STORE 2 ===')
print('store_id:', l2.get('store_id'))
print('cameras:', list(l2.get('cameras', {}).keys()))
for cid, cam in l2.get('cameras', {}).items():
    print(f'  {cid}: is_entry_camera={cam.get("is_entry_camera")}')
    for zid, z in cam.get('zones', {}).items():
        print(f'    {zid}: {z.get("zone_name")}')
