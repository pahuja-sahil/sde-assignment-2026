"""Ingest simulator history and live events into SQLite.

Ingestion is chunk-granular and idempotent: the calls/aggregates writes, the
audit rows derived from events, and the cursor update all commit together.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict

import httpx

from . import db

START = dt.date(2027, 1, 4)
_DAY_CACHE: dict[str, int] = {}

CALL_COLS = (
    "event_id", "minute", "ts", "day", "workflow_id", "campaign_id", "lead_id", "person_id",
    "attempt_no", "campaign_day_index", "config_version", "cohort_id", "welcome_message",
    "voice", "caller_id", "prompt_version", "connected", "engaged", "halluc", "disposition",
    "duration_s", "turns", "flow_adherence",
)
CALL_SQL = f"INSERT OR IGNORE INTO calls ({','.join(CALL_COLS)}) VALUES ({','.join('?' * len(CALL_COLS))})"
LEAD_SQL = ("INSERT OR IGNORE INTO leads (lead_id, workflow_id, campaign_id, person_id, lang, "
            "city_tier, age_band, value_band, first_minute, first_ts) VALUES (?,?,?,?,?,?,?,?,?,?)")
CAMP_SQL = ("INSERT OR IGNORE INTO campaigns (campaign_id, workflow_id, tenant_id, minute, day, leads, "
            "config_version_at_creation) VALUES (?,?,?,?,?,?,?)")
CHANGE_SQL = ("INSERT OR IGNORE INTO config_changes (event_id, minute, ts, workflow_id, version, actor, "
              "changed_dims, proposal_id, approval_id) VALUES (?,?,?,?,?,?,?,?,?)")
AGG_SQL = """
INSERT INTO call_agg (workflow_id, day, config_version, cohort_id, welcome_message, voice, caller_id,
                      prompt_version, dials, connected, engaged, halluc)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(workflow_id, day, config_version, cohort_id, welcome_message, voice, caller_id, prompt_version)
DO UPDATE SET dials=dials+excluded.dials, connected=connected+excluded.connected,
              engaged=engaged+excluded.engaged, halluc=halluc+excluded.halluc
"""


def event_minute(ts: str) -> int:
    key = ts[:10]
    day_index = _DAY_CACHE.get(key)
    if day_index is None:
        y, m, d = int(key[0:4]), int(key[5:7]), int(key[8:10])
        day_index = (dt.date(y, m, d) - START).days
        _DAY_CACHE[key] = day_index
    return day_index * 1440 + int(ts[11:13]) * 60 + int(ts[14:16])


def _int(value, default=0) -> int:
    if value is None:
        return default
    return int(value)


def _float(value, default=0.0) -> float:
    if value is None:
        return default
    return float(value)


def _sync_approval_event(conn, ev: dict) -> None:
    aid = ev.get("approval_id")
    if not aid:
        return
    ts = ev.get("ts", "")
    status = ev.get("status")
    reasons = json.dumps(ev.get("reasons") or [])
    row = conn.execute("SELECT approval_id FROM approvals WHERE approval_id=?", (aid,)).fetchone()
    if row:
        conn.execute("UPDATE approvals SET status=?, decided_at=?, reasons=?, last_synced_minute=? "
                     "WHERE approval_id=?", (status, ts, reasons, event_minute(ts), aid))
    else:
        conn.execute("INSERT INTO approvals(approval_id, workflow_id, status, decided_at, reasons, "
                     "last_synced_minute) VALUES(?,?,?,?,?,?)",
                     (aid, ev.get("workflow_id"), status, ts, reasons, event_minute(ts)))
    conn.execute("UPDATE proposals SET status=?, updated_minute=? WHERE approval_id=? AND status IN "
                 "('prepared','submitted','pending','approved')",
                 (status, event_minute(ts), aid))


def ingest_chunk(conn, kind: str, chunk_no: int, text: str) -> tuple[int, int, int]:
    leads: list[tuple] = []
    calls: list[tuple] = []
    camps: list[tuple] = []
    changes: list[tuple] = []
    agg: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    max_event_id = -1
    n_events = 0
    for line in text.splitlines():
        if not line:
            continue
        ev = json.loads(line)
        n_events += 1
        eid = _int(ev.get("event_id"), -1)
        if eid > max_event_id:
            max_event_id = eid
        typ = ev.get("type")
        ts = ev.get("ts", "")
        if typ == "lead":
            f = ev.get("features") or {}
            leads.append((ev.get("lead_id"), ev.get("workflow_id"), ev.get("campaign_id"),
                          ev.get("person_id"), f.get("lang"), f.get("city_tier"),
                          f.get("age_band"), f.get("value_band"), event_minute(ts) if ts else None, ts))
        elif typ == "call":
            v = ev.get("variant") or {}
            qa = ev.get("qa") or {}
            minute = event_minute(ts)
            day = ts[:10]
            connected = bool(ev.get("connected"))
            engaged = bool(ev.get("engaged"))
            halluc = bool(qa.get("hallucination_flag")) if connected else False
            calls.append((
                eid, minute, ts, day, ev.get("workflow_id"), ev.get("campaign_id"), ev.get("lead_id"),
                ev.get("person_id"), ev.get("attempt_no"), ev.get("campaign_day_index"),
                ev.get("config_version"), ev.get("cohort_id"), v.get("welcome_message"), v.get("voice"),
                v.get("caller_id"), v.get("prompt_version"), int(connected), int(engaged), int(halluc),
                ev.get("disposition"), ev.get("duration_s"), ev.get("turns"),
                _float(qa.get("flow_adherence"), 0.0),
            ))
            key = (ev.get("workflow_id"), day, ev.get("config_version"), ev.get("cohort_id"),
                   v.get("welcome_message"), v.get("voice"), v.get("caller_id"), v.get("prompt_version"))
            a = agg[key]
            a[0] += 1
            a[1] += int(connected)
            a[2] += int(engaged)
            a[3] += int(halluc)
        elif typ == "campaign_created":
            camps.append((ev.get("campaign_id"), ev.get("workflow_id"), ev.get("tenant_id"),
                          event_minute(ts) if ts else None, ts[:10] if ts else None,
                          ev.get("leads"), ev.get("config_version_at_creation")))
        elif typ == "config_changed":
            minute = event_minute(ts) if ts else None
            changes.append((eid, minute, ts, ev.get("workflow_id"), ev.get("version"), ev.get("actor"),
                            json.dumps(ev.get("changed_dims") or []), ev.get("proposal_id"),
                            ev.get("approval_id")))
            db.record_decision(conn, minute or 0, ts, ev.get("workflow_id"), "config_changed",
                               f"{ev.get('actor')} applied v{ev.get('version')}",
                               {"dims": ev.get("changed_dims"), "version": ev.get("version"),
                                "actor": ev.get("actor"), "proposal_id": ev.get("proposal_id"),
                                "approval_id": ev.get("approval_id")})
        elif typ == "approval_decided":
            _sync_approval_event(conn, ev)
            db.record_decision(conn, event_minute(ts), ts, ev.get("workflow_id"), "approval_decided",
                               f"{ev.get('status')} {ev.get('approval_id')}",
                               {"approval_id": ev.get("approval_id"), "status": ev.get("status"),
                                "reasons": ev.get("reasons") or []})

    with conn:
        if leads:
            conn.executemany(LEAD_SQL, leads)
        for start in range(0, len(calls), 20000):
            conn.executemany(CALL_SQL, calls[start:start + 20000])
        if camps:
            conn.executemany(CAMP_SQL, camps)
        if changes:
            conn.executemany(CHANGE_SQL, changes)
        conn.executemany(AGG_SQL, [(*key, *vals) for key, vals in agg.items()])
        db.set_meta(conn, f"cursor_{kind}", chunk_no)
        if max_event_id >= 0:
            previous = int(db.get_meta(conn, "last_event_id", -1))
            if max_event_id > previous:
                db.set_meta(conn, "last_event_id", max_event_id)
        db.set_meta(conn, "last_ingested_minute", db.latest_minute(conn))
    return len(leads), len(calls), n_events


def sync(client: httpx.Client, conn, kind: str, verbose: bool = False) -> dict:
    cursor = db.get_cursor(conn, kind)
    chunks = client.get("/events/chunks", params={"kind": kind, "after": cursor}).json()
    chunks.sort(key=lambda c: c["chunk"])
    totals = {"chunks": 0, "leads": 0, "calls": 0, "events": 0}
    for ch in chunks:
        if ch["chunk"] <= cursor:
            continue
        text = client.get(f"/events/chunks/{ch['chunk']}").text
        n_leads, n_calls, n_events = ingest_chunk(conn, kind, ch["chunk"], text)
        totals["chunks"] += 1
        totals["leads"] += n_leads
        totals["calls"] += n_calls
        totals["events"] += n_events
        cursor = ch["chunk"]
        if verbose:
            print(f"{kind}: chunk {ch['chunk']} leads={n_leads} calls={n_calls} events={n_events}",
                  flush=True)
    return totals


def summary(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT workflow_id, SUM(dials) dials, SUM(connected) connected, SUM(engaged) engaged, "
        "SUM(halluc) halluc FROM call_agg GROUP BY workflow_id ORDER BY workflow_id").fetchall()
    out = []
    for r in rows:
        dials = r["dials"] or 0
        connected = r["connected"] or 0
        out.append({"workflow_id": r["workflow_id"], "dials": dials, "connected": connected,
                    "engaged": r["engaged"] or 0,
                    "pickup_rate": connected / dials if dials else 0.0,
                    "engagement_rate": (r["engaged"] or 0) / connected if connected else 0.0,
                    "hallucinated_calls": r["halluc"] or 0})
    return out


def print_summary(conn) -> None:
    print("\nWorkflow summary (all ingested data):", flush=True)
    for s in summary(conn):
        print(f"  {s['workflow_id']:20s} dials={s['dials']:8d} pickup={s['pickup_rate']:.3f} "
              f"engagement={s['engagement_rate']:.3f} halluc={s['hallucinated_calls']}", flush=True)
    n_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    n_calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    print(f"  leads stored: {n_leads}, calls stored: {n_calls}", flush=True)
