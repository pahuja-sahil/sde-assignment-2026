"""Read-only queries over ingested calls used by detection and experiments."""
from __future__ import annotations

from . import cohorts

DIMS = {"welcome_message", "voice", "caller_id", "prompt_version"}
FEATURES = set(cohorts.FEATURES)


def _col(name: str, allowed: set[str]) -> str:
    if name not in allowed:
        raise ValueError(f"bad column {name}")
    return name


def variant_stats(conn, workflow_id: str, dim: str, since: int, until: int | None = None) -> dict:
    col = _col(dim, DIMS)
    sql = (f"SELECT {col} arm, COUNT(*) dials, SUM(connected) connected, SUM(engaged) engaged, "
           f"SUM(halluc) halluc FROM calls WHERE workflow_id=? AND minute>=?")
    args: list = [workflow_id, since]
    if until is not None:
        sql += " AND minute<?"
        args.append(until)
    sql += f" GROUP BY {col}"
    out = {}
    for r in conn.execute(sql, args):
        out[r["arm"]] = {"dials": int(r["dials"] or 0), "connected": int(r["connected"] or 0),
                         "engaged": int(r["engaged"] or 0), "halluc": int(r["halluc"] or 0)}
    return out


def variant_feature_stats(conn, workflow_id: str, dim: str, feature: str, since: int,
                           until: int | None = None) -> dict:
    col = _col(dim, DIMS)
    feat = _col(feature, FEATURES)
    sql = (f"SELECT l.{feat} val, c.{col} arm, COUNT(*) dials, SUM(c.connected) connected, "
           f"SUM(c.engaged) engaged FROM calls c JOIN leads l ON c.lead_id=l.lead_id "
           f"WHERE c.workflow_id=? AND c.minute>=?")
    args: list = [workflow_id, since]
    if until is not None:
        sql += " AND c.minute<?"
        args.append(until)
    sql += f" GROUP BY l.{feat}, c.{col}"
    out: dict = {}
    for r in conn.execute(sql, args):
        out.setdefault(str(r["val"]), {})[r["arm"]] = {
            "dials": int(r["dials"] or 0), "connected": int(r["connected"] or 0),
            "engaged": int(r["engaged"] or 0)}
    return out


def hour_stats(conn, workflow_id: str, since: int, until: int | None = None,
               age: str | None = None) -> dict[int, dict]:
    sql = ("SELECT CAST((((c.minute % 1440) + 1440) % 1440) / 60 AS INTEGER) hour, "
           "COUNT(*) dials, SUM(c.connected) connected, SUM(c.engaged) engaged "
           "FROM calls c")
    args: list = []
    if age:
        sql += " JOIN leads l ON c.lead_id=l.lead_id"
    sql += " WHERE c.workflow_id=? AND c.minute>=?"
    args.extend([workflow_id, since])
    if until is not None:
        sql += " AND c.minute<?"
        args.append(until)
    if age:
        sql += " AND l.age_band=?"
        args.append(age)
    sql += " GROUP BY hour ORDER BY hour"
    out = {}
    for r in conn.execute(sql, args):
        out[int(r["hour"])] = {"dials": int(r["dials"] or 0),
                               "connected": int(r["connected"] or 0),
                               "engaged": int(r["engaged"] or 0)}
    return out


def feature_lead_counts(conn, workflow_id: str) -> dict:
    return cohorts.feature_counts(conn, workflow_id)


def segment_share(conn, workflow_id: str, where: dict) -> float:
    counts = cohorts.feature_counts(conn, workflow_id)
    total = sum(counts.values())
    if not total:
        return 0.0
    return cohorts._match_count(counts, where) / total


def affected_leads(conn, workflow_id: str, where_list: list[dict] | None, next_batch_size: int) -> int:
    since = conn.execute("SELECT MAX(minute) FROM calls WHERE workflow_id=?", (workflow_id,)).fetchone()[0]
    since = int(since or 0) - 3 * 1440
    rows = conn.execute(
        "SELECT l.lead_id, l.lang, l.city_tier, l.age_band, l.value_band "
        "FROM leads l JOIN calls c ON c.lead_id=l.lead_id AND c.workflow_id=l.workflow_id "
        "WHERE l.workflow_id=? AND c.minute>=? AND c.engaged=0 GROUP BY l.lead_id",
        (workflow_id, since)).fetchall()
    active = []
    for r in rows:
        lead = {"lang": r["lang"], "city_tier": r["city_tier"],
                "age_band": r["age_band"], "value_band": r["value_band"]}
        if where_list is None or any(cohorts.matches(w, lead) for w in where_list):
            active.append(r["lead_id"])
    total = max(1, len(rows))
    share = 1.0 if where_list is None else len(active) / total
    return int(round(len(active) + share * max(0, next_batch_size)))


def latest_minute(conn, workflow_id: str) -> int:
    row = conn.execute("SELECT MAX(minute) FROM calls WHERE workflow_id=?", (workflow_id,)).fetchone()
    return int(row[0] or 0)


def calls_since(conn, workflow_id: str, since: int) -> int:
    row = conn.execute("SELECT COUNT(*) FROM calls WHERE workflow_id=? AND minute>=?",
                       (workflow_id, since)).fetchone()
    return int(row[0] or 0)
