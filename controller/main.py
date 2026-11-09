"""The always-on controller loop.

It binds to whatever run the simulator is serving, ingests history once, then
repeats: advance the world, ingest the new events, let the engine decide. All
durable state lives in SQLite, so a kill at any point resumes cleanly.
"""
from __future__ import annotations

import argparse
import os

import httpx

from . import db, ingest
from .engine import Engine


def _state(client: httpx.Client) -> dict:
    r = client.get("/sim/state")
    r.raise_for_status()
    return r.json()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8700")
    ap.add_argument("--db", default="controller/state.db")
    ap.add_argument("--step", type=int, default=60, help="simulated minutes per tick (multiple of 15)")
    ap.add_argument("--crash-at-minute", type=int, default=0,
                    help="test only: hard-kill after advancing to this sim minute")
    ap.add_argument("--max-ticks", type=int, default=0, help="test only: stop after N ticks")
    a = ap.parse_args()
    if a.step <= 0 or a.step % 15:
        ap.error("--step must be a positive multiple of 15")

    client = httpx.Client(base_url=a.url, timeout=300)
    conn = db.connect(a.db)

    state = _state(client)
    if db.bind_run(conn, state):
        print(f"new run detected: cleared controller state for {state.get('scenario')}", flush=True)
    workflows = client.get("/workflows").json()

    hist = ingest.sync(client, conn, "history", verbose=True)
    live = ingest.sync(client, conn, "events")
    print(f"ingested history: {hist['events']} events, {hist['calls']} calls; "
          f"live backlog: {live['events']} events", flush=True)

    engine = Engine(client, conn, workflows, state)
    engine.gov.reconcile(engine.minute, engine.ts)
    engine.on_tick(state)

    ticks = 0
    last_day = None
    while not state["finished"]:
        r = client.post("/sim/advance", json={"minutes": a.step})
        if r.status_code == 409:
            break
        r.raise_for_status()
        state = r.json()
        ticks += 1
        if a.crash_at_minute and state["sim_minute"] >= a.crash_at_minute:
            print(f"SIMULATED CRASH at minute {state['sim_minute']} before ingest", flush=True)
            os._exit(1)
        ingest.sync(client, conn, "events")
        engine.on_tick(state)
        day = state["ts"][:10]
        if day != last_day:
            last_day = day
            versions = {wid: c["version"] for wid, c in engine.current.items()}
            print(f"day {day} minute {state['sim_minute']} versions={versions}", flush=True)
        if a.max_ticks and ticks >= a.max_ticks:
            print("stopped at --max-ticks", flush=True)
            break

    if state["finished"]:
        print(f"finished at {state['ts']} (minute {state['sim_minute']})", flush=True)
    ingest.print_summary(conn)


if __name__ == "__main__":
    main()
