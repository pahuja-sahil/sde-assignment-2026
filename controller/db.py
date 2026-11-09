"""SQLite state for the controller.

All durable loop state lives here: the ingest cursor, raw events, aggregates,
governance lifecycle and the audit trail. WAL plus one transaction per chunk
makes a kill at any point recoverable.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    campaign_id TEXT,
    person_id TEXT NOT NULL,
    lang TEXT,
    city_tier TEXT,
    age_band TEXT,
    value_band TEXT,
    first_minute INTEGER,
    first_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_person ON leads(workflow_id, person_id);
CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(workflow_id, campaign_id);
CREATE INDEX IF NOT EXISTS idx_leads_wf ON leads(workflow_id);

CREATE TABLE IF NOT EXISTS calls (
    event_id INTEGER PRIMARY KEY,
    minute INTEGER NOT NULL,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    campaign_id TEXT,
    lead_id TEXT,
    person_id TEXT,
    attempt_no INTEGER,
    campaign_day_index INTEGER,
    config_version INTEGER,
    cohort_id TEXT,
    welcome_message TEXT,
    voice TEXT,
    caller_id TEXT,
    prompt_version TEXT,
    connected INTEGER NOT NULL,
    engaged INTEGER NOT NULL,
    halluc INTEGER NOT NULL,
    disposition TEXT,
    duration_s INTEGER,
    turns INTEGER,
    flow_adherence REAL
);
CREATE INDEX IF NOT EXISTS idx_calls_wf_minute ON calls(workflow_id, minute);
CREATE INDEX IF NOT EXISTS idx_calls_wf_day ON calls(workflow_id, day);
CREATE INDEX IF NOT EXISTS idx_calls_lead ON calls(workflow_id, lead_id);
CREATE INDEX IF NOT EXISTS idx_calls_person ON calls(workflow_id, person_id);

CREATE TABLE IF NOT EXISTS call_agg (
    workflow_id TEXT NOT NULL,
    day TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    cohort_id TEXT NOT NULL,
    welcome_message TEXT NOT NULL,
    voice TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    halluc INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, day, config_version, cohort_id,
                 welcome_message, voice, caller_id, prompt_version)
);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    tenant_id TEXT,
    minute INTEGER,
    day TEXT,
    leads INTEGER,
    config_version_at_creation INTEGER
);
CREATE INDEX IF NOT EXISTS idx_campaigns_wf ON campaigns(workflow_id, minute);

CREATE TABLE IF NOT EXISTS config_changes (
    event_id INTEGER PRIMARY KEY,
    minute INTEGER,
    ts TEXT,
    workflow_id TEXT NOT NULL,
    version INTEGER,
    actor TEXT,
    changed_dims TEXT,
    proposal_id TEXT,
    approval_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_wf ON config_changes(workflow_id, minute);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    proposal_id TEXT,
    workflow_id TEXT NOT NULL,
    status TEXT NOT NULL,
    base_version INTEGER,
    changed_dims TEXT,
    created_at TEXT,
    expires_at TEXT,
    apply_by TEXT,
    decided_at TEXT,
    reasons TEXT,
    proposed_config TEXT,
    applied_version INTEGER,
    last_synced_minute INTEGER
);
CREATE INDEX IF NOT EXISTS idx_approvals_wf ON approvals(workflow_id, status);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    base_version INTEGER,
    proposed_config TEXT NOT NULL,
    config_hash TEXT,
    kind TEXT,
    status TEXT NOT NULL,
    approval_id TEXT,
    created_minute INTEGER,
    created_ts TEXT,
    updated_minute INTEGER,
    payload TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_wf ON proposals(workflow_id, status);

CREATE TABLE IF NOT EXISTS cohorts (
    workflow_id TEXT NOT NULL,
    cohort_id TEXT NOT NULL,
    where_json TEXT NOT NULL,
    source TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_minute INTEGER,
    PRIMARY KEY (workflow_id, cohort_id)
);

CREATE TABLE IF NOT EXISTS experiments (
    workflow_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    phase TEXT,
    status TEXT,
    started_minute INTEGER,
    checkpoint_minute INTEGER,
    payload TEXT,
    PRIMARY KEY (workflow_id, dimension)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    minute INTEGER,
    ts TEXT,
    workflow_id TEXT,
    kind TEXT NOT NULL,
    summary TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_wf ON decisions(workflow_id, minute);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    minute INTEGER,
    ts TEXT,
    workflow_id TEXT,
    severity TEXT,
    kind TEXT NOT NULL,
    message TEXT,
    detail TEXT,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_wf ON alerts(workflow_id, resolved);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    minute INTEGER,
    wall_s REAL,
    events_ingested INTEGER,
    ingest_s REAL,
    events_per_s REAL,
    lag_minutes REAL,
    credits_remaining REAL,
    rss_mb REAL,
    decisions INTEGER
);
"""

TABLES = ("metrics", "alerts", "decisions", "experiments", "cohorts", "proposals", "approvals",
          "config_changes", "campaigns", "call_agg", "calls", "leads", "meta")


def connect(path: str | Path) -> sqlite3.Connection:
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_cursor(conn: sqlite3.Connection, kind: str) -> int:
    value = get_meta(conn, f"cursor_{kind}")
    return int(value) if value is not None else -1


def reset_run(conn: sqlite3.Connection) -> None:
    with conn:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")


def bind_run(conn: sqlite3.Connection, state: dict) -> bool:
    """True when this is a fresh (or replaced) world and state was cleared."""
    scenario = str(state.get("scenario"))
    old_scenario = get_meta(conn, "bound_scenario")
    old_last_event = int(get_meta(conn, "last_event_id", -1))
    current_last = int(state.get("last_event_id", -1))
    replace = old_scenario is not None and (
        old_scenario != scenario or (old_last_event >= 0 and current_last < old_last_event)
    )
    if replace:
        reset_run(conn)
    with conn:
        set_meta(conn, "bound_scenario", scenario)
        set_meta(conn, "bound_end_minute", state.get("end_minute", -1))
        set_meta(conn, "bound_tick_minutes", state.get("tick_minutes", 15))
        if replace:
            set_meta(conn, "last_event_id", current_last)
    return replace


def record_decision(conn: sqlite3.Connection, minute: int, ts: str, workflow_id: str | None,
                    kind: str, summary: str, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO decisions(minute, ts, workflow_id, kind, summary, detail) VALUES(?,?,?,?,?,?)",
        (minute, ts, workflow_id, kind, summary, json.dumps(detail or {}, sort_keys=True)),
    )


def record_alert(conn: sqlite3.Connection, minute: int, ts: str, workflow_id: str | None,
                 severity: str, kind: str, message: str, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO alerts(minute, ts, workflow_id, severity, kind, message, detail) "
        "VALUES(?,?,?,?,?,?,?)",
        (minute, ts, workflow_id, severity, kind, message, json.dumps(detail or {}, sort_keys=True)),
    )


def latest_minute(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(minute) FROM calls").fetchone()
    return int(row[0] or 0)
