"""Config construction, diffing and local policy checks.

Mirrors the simulator's contract so we can avoid predictable 422/403 responses
and compute honest proposal metadata before calling governance.
"""
from __future__ import annotations

import copy
import hashlib
import json

AGENT_DIMS = ("welcome_message", "voice", "caller_id")
CHANGE_DIMS = ("cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule")


def dist(config: dict, cohort: str, dim: str) -> dict:
    return (config.get("allocations", {}).get(cohort) or {}).get(dim) or \
        config.get("allocations", {}).get("default", {}).get(dim, {})


def normalise(weights: dict) -> dict:
    clean = {str(k): float(v) for k, v in (weights or {}).items() if float(v) > 0}
    total = sum(clean.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in clean.items()}


def compose(base: dict, cohorts: list[dict] | None = None, allocations: dict | None = None,
            prompt_version: str | None = None, schedule: dict | None = None) -> dict:
    cfg = copy.deepcopy(base)
    if cohorts is not None:
        cfg["cohorts"] = [{"id": c["id"], "where": copy.deepcopy(c["where"])} for c in cohorts
                          if c.get("id") != "default"]
    if allocations is not None:
        default = (base.get("allocations", {}).get("default") or {})
        merged: dict[str, dict] = {"default": {dim: dict(default.get(dim, {})) for dim in AGENT_DIMS}}
        for cohort, dims in allocations.items():
            target = merged["default"] if cohort == "default" else merged.setdefault(cohort, {})
            for dim, weights in dims.items():
                clean = normalise(weights)
                if clean:
                    target[dim] = clean
        for cohort in list(merged):
            if cohort != "default" and not merged[cohort]:
                del merged[cohort]
        cfg["allocations"] = merged
    if prompt_version is not None:
        cfg["prompt_version"] = prompt_version
    if schedule is not None:
        cfg["schedule"] = copy.deepcopy(schedule)
    return cfg


def config_hash(cfg: dict) -> str:
    blob = json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha1(blob).hexdigest()[:16]


def changed_dims(old: dict, new: dict) -> list[str]:
    changed = []
    if (old.get("cohorts") or []) != (new.get("cohorts") or []):
        changed.append("cohorts")
    cohorts = set(old.get("allocations", {})) | set(new.get("allocations", {}))
    for dim in AGENT_DIMS:
        if any(dist(old, c, dim) != dist(new, c, dim) for c in cohorts):
            changed.append(dim)
    if old.get("prompt_version") != new.get("prompt_version"):
        changed.append("prompt_version")
    if (old.get("schedule") or {}) != (new.get("schedule") or {}):
        changed.append("schedule")
    return [d for d in CHANGE_DIMS if d in changed]


def max_shift(old: dict, new: dict) -> float:
    cohorts = {"default"} | {c["id"] for c in old.get("cohorts") or []} | \
        {c["id"] for c in new.get("cohorts") or []}
    worst = 0.0
    for cohort in cohorts:
        for dim in AGENT_DIMS:
            a, b = dist(old, cohort, dim), dist(new, cohort, dim)
            tv = 0.5 * sum(abs(a.get(o, 0) - b.get(o, 0)) for o in set(a) | set(b))
            worst = max(worst, tv)
    return worst


def diff(old: dict, new: dict) -> dict:
    dims = changed_dims(old, new)
    if not dims:
        return {"changed_dims": [], "note": "no change"}
    out = {"changed_dims": dims}
    if "cohorts" in dims:
        out["cohorts"] = {"from": old.get("cohorts") or [], "to": new.get("cohorts") or []}
    for dim in AGENT_DIMS:
        if dim in dims:
            cohorts = {"default"} | {c["id"] for c in old.get("cohorts") or []} | \
                {c["id"] for c in new.get("cohorts") or []}
            changes = {}
            for cohort in sorted(cohorts):
                a, b = dist(old, cohort, dim), dist(new, cohort, dim)
                if a != b:
                    changes[cohort] = {"from": a, "to": b}
            out[dim] = changes
    if "prompt_version" in dims:
        out["prompt_version"] = {"from": old.get("prompt_version"), "to": new.get("prompt_version")}
    if "schedule" in dims:
        out["schedule"] = {"from": (old.get("schedule") or {}).get("hour_weights", {}),
                           "to": (new.get("schedule") or {}).get("hour_weights", {})}
    return out


def validate(wf: dict, cfg: dict) -> list[str]:
    problems = []
    options = wf["options"]
    if not isinstance(cfg.get("cohorts"), list):
        problems.append("cohorts must be a list")
    if len(cfg.get("cohorts") or []) > 20:
        problems.append("at most 20 cohorts")
    ids = [c.get("id") for c in cfg.get("cohorts") or []]
    if len(ids) != len(set(ids)) or "default" in ids:
        problems.append("cohort ids must be unique and not 'default'")
    allocs = cfg.get("allocations") or {}
    if "default" not in allocs:
        problems.append("allocations.default required")
    for cohort, dims in allocs.items():
        if cohort != "default" and cohort not in ids:
            problems.append(f"allocation for undefined cohort {cohort}")
        for dim, weights in (dims or {}).items():
            if dim not in AGENT_DIMS:
                problems.append(f"unknown dimension {dim}")
            elif not isinstance(weights, dict) or not weights:
                problems.append(f"{cohort}.{dim} must be a non-empty map")
            elif any(o not in options[dim] for o in weights):
                problems.append(f"{cohort}.{dim} has options not allowed by workflow")
            elif sum(float(v) for v in weights.values()) <= 0:
                problems.append(f"{cohort}.{dim} sums to zero")
    if "default" in allocs:
        missing = [d for d in AGENT_DIMS if d not in allocs["default"]]
        if missing:
            problems.append(f"default allocation missing {missing}")
    if cfg.get("prompt_version") not in options["prompt_version"]:
        problems.append("prompt_version not allowed")
    lo, hi = wf["dialling_window"]
    for key, table in ((cfg.get("schedule") or {}).get("hour_weights") or {}).items():
        if key != "default" and key not in ids:
            problems.append(f"hour_weights for undefined cohort {key}")
        for hour, weight in (table or {}).items():
            try:
                h = int(hour)
            except (TypeError, ValueError):
                problems.append(f"bad hour {hour}")
                continue
            if not lo <= h < hi:
                problems.append(f"hour {h} outside window")
            if float(weight) < 0:
                problems.append(f"negative weight at hour {h}")
    return problems


def impact_score(relative_primary_lift: float, population_share: float) -> float:
    raw = max(0.0, relative_primary_lift) * max(0.0, min(1.0, population_share)) / 0.10
    return round(max(0.0, min(1.0, raw)), 4)
