import json
import httpx

c = httpx.Client(base_url="http://127.0.0.1:8700", timeout=60)

print("STATE:", json.dumps(c.get("/sim/state").json(), indent=2))

for w in c.get("/workflows").json():
    print("\n=== WORKFLOW:", w["workflow_id"], "===")
    print(json.dumps(w, indent=2))