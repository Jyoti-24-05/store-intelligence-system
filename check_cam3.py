import json
with open('data/store1_layout.json') as f:
    layout = json.load(f)
print(json.dumps(layout['cameras']['CAM_3'], indent=2))
