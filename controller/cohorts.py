"""The single shared cohort object.

Cohorts are stored in SQLite and referenced by detection, experiments, the
config builder and the supervisor view. The partition is data-driven: age bands
form the base, and evidence-backed language/tier/value segments are intersected
with them later. Cohort growth is capped and pruned by lead share.
"""
from __future__ import annotations

import json
import re
from itertools import product

FEATURES: dict[str, list[str]] = {
    "lang": ["hi", "en", "ta", "te", "kn", "mr", "bn"],
    "city_tier": ["1", "2", "3"],
    "age_band": ["18-30", "31-45", "46-60", "60+"],
    "value_band": ["low", "mid", "high"],
}
FEATURE_VALUES = {f: set(v) for f, v in FEATURES.items()}


def feature_counts(conn, workflow_id: str) -> dict[tuple, int]:
    rows = conn.execute(
        "SELECT lang, city_tier, age_band, value_band, COUNT(*) n FROM leads "
        "WHERE workflow_id=? GROUP BY lang, city_tier, age_band, value_band",
        (workflow_id,)).fetchall()
    return {(r["lang"], r["city_tier"], r["age_band"], r["value_band"]): int(r["n"]) for r in rows}


def _match_count(counts: dict[tuple, int], where: dict) -> int:
    total = 0
    for key, n in counts.items():
        lead = dict(zip(("lang", "city_tier", "age_band", "value_band"), key))
        if matches(where, lead):
            total += n
    return total


def matches(where: dict, lead: dict) -> bool:
    for feature, values in (where or {}).items():
        if str(lead.get(feature)) not in {str(v) for v in values}:
            return False
    return True


def cohort_id_for(where: dict) -> str:
    parts = []
    short = {"age_band": "age", "city_tier": "tier", "value_band": "value", "lang": "lang"}
    for feature in ("age_band", "lang", "city_tier", "value_band"):
        if feature not in where:
            continue
        label = "_".join(str(v) for v in where[feature])
        label = label.replace("+", "p").replace("-", "_")
        label = re.sub(r"[^a-zA-Z0-9_]+", "_", label)
        parts.append(f"{short[feature]}_{label}")
    return "__".join(parts) or "default"


def age_cohorts(conn, workflow_id: str, min_share: float = 0.03) -> list[dict]:
    counts = feature_counts(conn, workflow_id)
    total = sum(counts.values())
    if not total:
        return []
    out = []
    for age in FEATURES["age_band"]:
        share = sum(n for key, n in counts.items() if key[2] == age) / total
        if share >= min_share:
            where = {"age_band": [age]}
            out.append({"id": cohort_id_for(where), "where": where, "share": round(share, 4),
                        "source": "age_base"})
    return out


def _feature_options(segments: list[dict], feature: str) -> list[dict]:
    selected: list[str] = []
    for seg in segments:
        if feature in seg["where"]:
            selected.extend(str(v) for v in seg["where"][feature])
    selected = [v for v in FEATURES[feature] if v in set(selected)]
    if not selected:
        return []
    options = [{feature: list(selected)}]
    rest = [v for v in FEATURES[feature] if v not in set(selected)]
    if rest:
        options.append({feature: rest})
    return options


def build_partition(conn, workflow_id: str, age_bands: list[dict], extra_segments: list[dict],
                    min_share: float = 0.02, cap: int = 20) -> list[dict]:
    """Build a disjoint partition from age bands plus up to two extra features."""
    counts = feature_counts(conn, workflow_id)
    total = sum(counts.values())
    if not total:
        return []
    extra_features: list[str] = []
    for seg in extra_segments:
        for feature in seg["where"]:
            if feature != "age_band" and feature not in extra_features:
                extra_features.append(feature)
    extra_features = extra_features[:2]
    feature_options = [_feature_options(extra_segments, f) for f in extra_features]
    age_options = [c["where"] for c in age_bands] if age_bands else [None]
    if any(not opts for opts in feature_options):
        feature_options = []
    combos = product(age_options, *feature_options) if feature_options else \
        ([a] for a in age_options)
    out = []
    seen = set()
    for combo in combos:
        wheres = [w for w in combo if w]
        if not wheres:
            continue
        where: dict = {}
        for w in wheres:
            for feature, values in w.items():
                where[feature] = list(values)
        share = _match_count(counts, where) / total
        if share < min_share:
            continue
        cid = cohort_id_for(where)
        if cid in seen:
            continue
        seen.add(cid)
        out.append({"id": cid, "where": where, "share": round(share, 4),
                    "source": "intersection", "specificity": len(where)})
    out.sort(key=lambda c: (-c["specificity"], -c["share"], c["id"]))
    out = out[:cap]
    for c in out:
        c.pop("specificity", None)
    return out


def save_cohorts(conn, workflow_id: str, cohorts: list[dict], minute: int, source: str) -> None:
    with conn:
        conn.execute("UPDATE cohorts SET active=0 WHERE workflow_id=?", (workflow_id,))
        for c in cohorts:
            conn.execute(
                "INSERT INTO cohorts(workflow_id, cohort_id, where_json, source, active, created_minute) "
                "VALUES(?,?,?,?,1,?) ON CONFLICT(workflow_id, cohort_id) DO UPDATE SET "
                "where_json=excluded.where_json, source=excluded.source, active=1, "
                "created_minute=excluded.created_minute",
                (workflow_id, c["id"], json.dumps(c["where"], sort_keys=True), source, minute))


def load_cohorts(conn, workflow_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT cohort_id, where_json, source, created_minute FROM cohorts "
        "WHERE workflow_id=? AND active=1 ORDER BY rowid", (workflow_id,)).fetchall()
    return [{"id": r["cohort_id"], "where": json.loads(r["where_json"]), "source": r["source"],
             "created_minute": r["created_minute"]} for r in rows]


def assign(cohorts: list[dict], lead: dict) -> dict:
    for c in cohorts:
        if matches(c["where"], lead):
            return c
    return {"id": "default", "where": {}, "source": "default"}


def validate_where(where: dict) -> list[str]:
    problems = []
    for feature, values in (where or {}).items():
        if feature not in FEATURES:
            problems.append(f"unknown feature {feature}")
        elif not isinstance(values, list) or not values:
            problems.append(f"{feature} must be a non-empty list")
        elif any(str(v) not in FEATURE_VALUES[feature] for v in values):
            problems.append(f"bad values for {feature}: {values}")
    return problems
