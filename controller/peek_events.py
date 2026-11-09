import json
import httpx

c = httpx.Client(base_url="http://127.0.0.1:8700", timeout=60)

chunks = c.get("/events/chunks", params={"kind": "history"}).json()
print("history chunks:", len(chunks))
print("first chunk info:", chunks[0])

first = chunks[0]["chunk"]
lines = c.get(f"/events/chunks/{first}").text.splitlines()
print("events in first chunk:", len(lines))

seen = {}
for line in lines:
    ev = json.loads(line)
    if ev["type"] not in seen:
        seen[ev["type"]] = ev

for t, ev in seen.items():
    print("\n---", t, "---")
    print(json.dumps(ev, indent=2))