"""The outcome-first decision loop.

One engine tick ingests nothing itself; it reads SQLite plus the simulator's
public API and decides whether to bootstrap, promote, roll back, respond to an
incident, or do nothing. Every decision is written to the audit store.
"""
from __future__ import annotations

import json

import httpx

from . import analysis, cohorts, config, db, stats
from .governance import Governance, LIVE_STATUSES, ROUTE_APPROVAL, ROUTE_AUTO

MIN_CONNECTED_GLOBAL = 250
MIN_CONNECTED_SEGMENT = 220
MIN_REL_LIFT = 0.03
MIN_REL_LIFT_SEGMENT = 0.04
GUARDRAIL_DROP = 0.015
EXPLORE_MINUTES = 36 * 60
EXPLORE_MAX_MINUTES = 72 * 60
EXTEND_MINUTES = 12 * 60
REQUEST_FLOOR_HOUR = 11
MAX_REQUESTS_PER_DAY = 2
CALLER_LOOKBACK_MINUTES = 6 * 60
HALLUC_LOOKBACK_MINUTES = 6 * 60
HALLUC_WARMUP_MINUTES = 6 * 60
HALLUC_MIN_CONNECTED = 300
HALLUC_MIN_BASE_CONNECTED = 1000
HALLUC_MIN_DIFF = 0.03
HALLUC_PVALUE = 0.001
HALLUC_COOLDOWN_MINUTES = 3 * 60
HALLUC_MAX_ATTEMPTS_PER_DAY = 3
MIN_PROMPT_PASS = 0.985
CREDIT_RESERVE_FRAC = 0.20


def _rate(s: dict | None, metric: str) -> float:
    if not s:
        return 0.0
    n = s["connected"] if metric == "engagement" else s["dials"]
    k = s["engaged"] if metric == "engagement" else s["connected"]
    return k / n if n else 0.0


def _prompt_candidate_ok(cur: str | None, cand: str | None, summary: str,
                         options: list[str]) -> bool:
    if not cand or cand == cur or cand not in options:
        return False
    low = (summary or "").lower()
    return not (low.startswith("strict") or "verbatim" in low)


class Engine:
    def __init__(self, client: httpx.Client, conn, workflows: list[dict], state: dict):
        self.client = client
        self.conn = conn
        self.gov = Governance(client, conn)
        self.wfs = {w["workflow_id"]: w for w in workflows}
        self.minute = int(state["sim_minute"])
        self.ts = state["ts"]
        self.initial_credits = float(state.get("credits_remaining", 0.0) or 0.0)
        self.current: dict[str, dict] = {}
        self._refresh_all()
        self._bootstrap_seen_versions()

    # ------------------------------------------------------------------ state
    def _refresh_all(self) -> None:
        for wid in self.wfs:
            self._refresh(wid)

    def _refresh(self, workflow_id: str) -> dict:
        r = self.client.get(f"/workflows/{workflow_id}/config")
        r.raise_for_status()
        js = r.json()
        self.current[workflow_id] = {"version": js["version"], "config": js["config"]}
        return self.current[workflow_id]

    def _exp_row(self, workflow_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM experiments WHERE workflow_id=? AND dimension='plan'", (workflow_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"] or "{}")
        return {"phase": row["phase"], "status": row["status"],
                "started_minute": row["started_minute"], "checkpoint_minute": row["checkpoint_minute"],
                "payload": payload}

    def _save_exp(self, workflow_id: str, phase: str, status: str, started: int | None,
                  checkpoint: int | None, payload: dict, minute: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO experiments(workflow_id, dimension, phase, status, started_minute, "
                "checkpoint_minute, payload) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(workflow_id, dimension) DO UPDATE SET phase=excluded.phase, "
                "status=excluded.status, started_minute=excluded.started_minute, "
                "checkpoint_minute=excluded.checkpoint_minute, payload=excluded.payload",
                (workflow_id, "plan", phase, status, started, checkpoint, json.dumps(payload, sort_keys=True)))

    def _meta_int(self, key: str, default: int = 0) -> int:
        value = db.get_meta(self.conn, key)
        return int(value) if value is not None else default

    def _bootstrap_seen_versions(self) -> None:
        for wid in self.wfs:
            row = self.conn.execute("SELECT MAX(version) FROM config_changes WHERE workflow_id=?",
                                    (wid,)).fetchone()
            seen = int(row[0] or 0)
            seen = max(seen, int(self.current[wid]["version"]))
            if db.get_meta(self.conn, f"seen_version_{wid}") is None:
                with self.conn:
                    db.set_meta(self.conn, f"seen_version_{wid}", seen)

    # ------------------------------------------------------------------ tick
    def on_tick(self, state: dict) -> None:
        self.minute = int(state["sim_minute"])
        self.ts = state["ts"]
        changed = self.gov.poll(self.minute, self.ts)
        if changed:
            self._refresh_all()
        self._sync_proposal_lifecycles()
        self._handle_config_events()
        self._incident_caller_id()
        self._incident_hallucination()
        for wid, wf in self.wfs.items():
            self._maybe_bootstrap(wid, wf)
            self._maybe_checkpoint(wid, wf)
            self._maybe_decay(wid, wf)
        self._record_metrics()

    # ------------------------------------------------------------- governance
    def _can_request(self, wf: dict) -> bool:
        gov = wf.get("governance") or {}
        lo, hi = gov.get("approver_hours", [10, 19])
        hour = (self.minute % 1440) // 60
        if not (max(REQUEST_FLOOR_HOUR, int(lo)) <= hour < max(REQUEST_FLOOR_HOUR + 1, int(hi) - 1)):
            return False
        cap = min(MAX_REQUESTS_PER_DAY, int(gov.get("max_requests_per_24h", 4)))
        if self.gov.requests_last_24h(wf["workflow_id"], self.minute) >= cap:
            return False
        return self.gov.find_live(wf["workflow_id"]) is None

    def _proposal_status(self, proposal_id: str | None) -> str | None:
        if not proposal_id:
            return None
        row = self.conn.execute("SELECT status FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        return row[0] if row else None

    def _proposal_minute(self, proposal_id: str | None) -> int | None:
        if not proposal_id:
            return None
        row = self.conn.execute("SELECT minute FROM config_changes WHERE proposal_id=? LIMIT 1",
                                (proposal_id,)).fetchone()
        return int(row[0]) if row else None

    def _sync_proposal_lifecycles(self) -> None:
        for wid in self.wfs:
            exp = self._exp_row(wid)
            if not exp:
                continue
            phase = exp["phase"]
            pid = exp["payload"].get("proposal_id")
            status = self._proposal_status(pid)
            if phase == "bootstrapping":
                applied = self._proposal_minute(pid)
                if applied is None and status == "applied":
                    applied = self.minute
                if applied is not None:
                    payload = exp["payload"]
                    payload["applied_minute"] = applied
                    self._save_exp(wid, "exploring", "active", applied,
                                   applied + EXPLORE_MINUTES, payload, self.minute)
                    self._record(wid, "experiment_started", "initial exploration is live",
                                 {"proposal_id": pid, "started_minute": applied,
                                  "checkpoint_minute": applied + EXPLORE_MINUTES})
                elif status in ("rejected", "expired", "stale", "lapsed", "failed", "auto_rejected", "duplicate"):
                    retry = self.minute + 12 * 60
                    self._save_exp(wid, "bootstrap_retry", status, None, retry, exp["payload"], self.minute)
                    self._record(wid, "bootstrap_interrupted", f"{status}; retrying later",
                                 {"proposal_id": pid, "retry_minute": retry})
            elif phase == "promoting":
                applied = self._proposal_minute(pid)
                if applied is None and status == "applied":
                    applied = self.minute
                if applied is not None:
                    payload = exp["payload"]
                    strat = payload.get("proposed_strategy") or {}
                    payload["strategy"] = strat
                    payload["promoted_minute"] = applied
                    payload["proposal_id"] = None
                    self._save_exp(wid, "exploiting", "active", exp["started_minute"],
                                   applied + 6 * 60, payload, self.minute)
                    self._record(wid, "promotion_applied", "promoted measured winners",
                                 {"proposal_id": pid, "applied_minute": applied,
                                  "strategy": strat})
                elif status in ("rejected", "expired", "stale", "lapsed", "failed", "duplicate"):
                    payload = exp["payload"]
                    payload["proposal_id"] = None
                    payload["last_rejection"] = status
                    self._save_exp(wid, "exploring", "rejected", exp["started_minute"],
                                   max(exp["checkpoint_minute"] or 0, self.minute + EXTEND_MINUTES),
                                   payload, self.minute)
                    self._record(wid, "promotion_rejected", f"{status}; continue measuring",
                                 {"proposal_id": pid, "status": status})
                    db.record_alert(self.conn, self.minute, self.ts, wid, "info", "promotion_rejected",
                                    f"{wid} promotion {status}", {"proposal_id": pid})

    def _record(self, workflow_id: str, kind: str, summary: str, detail: dict) -> None:
        with self.conn:
            db.record_decision(self.conn, self.minute, self.ts, workflow_id, kind, summary, detail)

    def _handle_config_events(self) -> None:
        for wid in self.wfs:
            seen = self._meta_int(f"seen_version_{wid}", 0)
            rows = self.conn.execute(
                "SELECT version, actor, changed_dims, minute, ts, proposal_id FROM config_changes "
                "WHERE workflow_id=? AND version>? ORDER BY version", (wid, seen)).fetchall()
            if not rows:
                continue
            for r in rows:
                seen = max(seen, int(r["version"]))
                if r["actor"] == "supervisor":
                    dims = json.loads(r["changed_dims"] or "[]")
                    with self.conn:
                        db.record_alert(self.conn, self.minute, self.ts, wid, "info", "supervisor_edit",
                                        f"supervisor changed {', '.join(dims)}",
                                        {"version": r["version"], "dims": dims, "minute": r["minute"]})
                    self._on_supervisor_edit(wid, dims, r["version"], int(r["minute"]))
            with self.conn:
                db.set_meta(self.conn, f"seen_version_{wid}", seen)
            self._refresh(wid)

    def _on_supervisor_edit(self, wid: str, dims: list[str], version: int, minute: int) -> None:
        exp = self._exp_row(wid)
        if exp and exp["phase"] in ("exploring", "promoting", "bootstrapping"):
            payload = exp["payload"]
            payload["supervisor_edit"] = {"dims": dims, "version": version, "minute": minute}
            if exp["phase"] == "bootstrapping":
                self._save_exp(wid, "bootstrap_retry", "supervisor", None,
                               self.minute + 6 * 60, payload, self.minute)
                self._record(wid, "experiment_interrupted", "supervisor edited config mid-bootstrap",
                             {"dims": dims, "version": version})
                return
            if any(d in dims for d in ("welcome_message", "voice", "cohorts", "prompt_version")):
                self._save_exp(wid, "exploring", "supervisor_reset", self.minute,
                               self.minute + EXPLORE_MINUTES, payload, self.minute)
                self._record(wid, "experiment_restarted",
                             "supervisor edited config; measuring again from the new baseline",
                             {"dims": dims, "version": version})
                return
        if "prompt_version" in dims:
            with self.conn:
                db.set_meta(self.conn, f"prompt_edit_{wid}", version)

    # --------------------------------------------------------------- bootstrap
    def _request_window_open(self, wf: dict) -> bool:
        gov = wf.get("governance") or {}
        lo, hi = gov.get("approver_hours", [10, 19])
        hour = (self.minute % 1440) // 60
        return max(REQUEST_FLOOR_HOUR, int(lo)) <= hour < max(REQUEST_FLOOR_HOUR + 1, int(hi) - 1)

    def _hour_weights(self, wf: dict) -> dict:
        wid = wf["workflow_id"]
        lo, hi = wf["dialling_window"]
        ages = cohorts.age_cohorts(self.conn, wid)
        global_profile = analysis.hour_stats(self.conn, wid, -10**9, 0)
        default = self._profile_weights(global_profile, lo, hi)
        tables = {"default": default}
        for c in ages:
            age = c["where"]["age_band"][0]
            prof = analysis.hour_stats(self.conn, wid, -10**9, 0, age=age)
            if sum(v["dials"] for v in prof.values()) >= 200:
                tables[c["id"]] = self._profile_weights(prof, lo, hi)
        return {"hour_weights": tables, "lead_preferred_hour": {}}

    @staticmethod
    def _profile_weights(profile: dict[int, dict], lo: int, hi: int) -> dict[str, float]:
        rates = {}
        overall_conn = sum(v["connected"] for v in profile.values())
        overall_dials = sum(v["dials"] for v in profile.values())
        base = overall_conn / overall_dials if overall_dials else 0.3
        for hour in range(lo, hi):
            s = profile.get(hour)
            if s and s["dials"] >= 30:
                r = (s["connected"] + 2.0) / (s["dials"] + 8.0)
            else:
                r = base
            weight = r / base if base > 0 else 1.0
            rates[str(hour)] = round(max(0.4, min(1.6, weight)), 4)
        mean = sum(rates.values()) / max(1, len(rates))
        if mean > 0:
            rates = {h: round(max(0.3, w / mean), 4) for h, w in rates.items()}
        return rates

    def _initial_allocations(self, wf: dict, base_cfg: dict) -> tuple[dict, dict]:
        incumbent = {}
        alloc = {}
        for dim in ("welcome_message", "voice"):
            opts = list(wf["options"][dim])
            current = config.dist(base_cfg, "default", dim)
            inc = max(opts, key=lambda o: current.get(o, 0)) if current else opts[0]
            incumbent[dim] = inc
            others = [o for o in opts if o != inc]
            if len(opts) == 2:
                weights = {inc: 0.5, others[0]: 0.5}
            elif len(opts) == 3:
                weights = {inc: 0.5, others[0]: 0.25, others[1]: 0.25}
            else:
                share = round(0.6 / max(1, len(others)), 4)
                weights = {inc: 0.4, **{o: share for o in others}}
            alloc[dim] = weights
        alloc["caller_id"] = dict(config.dist(base_cfg, "default", "caller_id"))
        return alloc, incumbent

    @staticmethod
    def _current_allocations(cfg: dict) -> dict:
        out = {}
        for cohort, dims in (cfg.get("allocations") or {}).items():
            out[cohort] = {dim: dict(weights) for dim, weights in (dims or {}).items()}
        return out

    def _maybe_bootstrap(self, wid: str, wf: dict) -> None:
        exp = self._exp_row(wid)
        if exp and exp["phase"] not in ("bootstrap_retry",):
            return
        if exp and exp["phase"] == "bootstrap_retry":
            if self.minute < (exp["checkpoint_minute"] or 0):
                return
        if self._proposal_status((exp or {}).get("payload", {}).get("proposal_id")) in LIVE_STATUSES:
            return
        if not self._request_window_open(wf) or not self._can_request(wf):
            return
        base = self._refresh(wid)
        age = cohorts.age_cohorts(self.conn, wid)
        if not age:
            return
        cohorts.save_cohorts(self.conn, wid, age, self.minute, "age_base")
        allocations, incumbent = self._initial_allocations(wf, base["config"])
        new_cfg = config.compose(base["config"], cohorts=age, allocations={"default": allocations})
        problems = config.validate(wf, new_cfg)
        if problems:
            self._record(wid, "bootstrap_invalid", "config rejected locally", {"problems": problems})
            new_cfg = config.compose(base["config"], cohorts=age)
            problems = config.validate(wf, new_cfg)
            if problems:
                return
        if not config.changed_dims(base["config"], new_cfg):
            self._save_exp(wid, "exploring", "no_change", self.minute,
                           self.minute + EXPLORE_MINUTES,
                           {"incumbents": incumbent, "planned_arms": {}, "proposal_id": None}, self.minute)
            return
        affected = analysis.affected_leads(self.conn, wid, [c["where"] for c in age],
                                           int(wf["daily_batch"]["size"]))
        dry = self._dry_run(wid, base["config"], new_cfg, self.minute - 7 * 1440, None)
        payload = {"incumbents": incumbent,
                   "planned_arms": {"welcome_message": list(wf["options"]["welcome_message"]),
                                    "voice": list(wf["options"]["voice"])},
                   "initial_hash": config.config_hash(new_cfg)}
        summary = "Explore welcome/voice arms across age cohorts"
        rationale = (f"Randomised 2-day field experiment over welcome_message and voice on "
                     f"{len(age)} age cohorts; pickup guardrail respected by keeping the live "
                     f"schedule and caller-ID mix unchanged.")
        row = self.gov.create(wf, new_cfg, self.minute, self.ts, "experiment", ROUTE_APPROVAL,
                              summary, config.diff(base["config"], new_cfg), affected, dry,
                              config.impact_score(0.02, 1.0), rationale,
                              expires_in_hours=min(12, int(wf["governance"]["proposal_ttl_hours"])))
        if not row:
            return
        payload["proposal_id"] = row["proposal_id"]
        ok = self.gov.submit_approval(row["proposal_id"], self.minute, self.ts)
        phase = "bootstrapping" if ok else "bootstrap_retry"
        checkpoint = None if ok else self.minute + 60
        self._save_exp(wid, phase, "active" if ok else "submit_failed", None, checkpoint, payload, self.minute)
        if ok:
            self._record(wid, "bootstrap_requested", "exploration proposal submitted",
                         {"proposal_id": row["proposal_id"], "incumbents": incumbent,
                          "cohorts": [c["id"] for c in age]})

    # ---------------------------------------------------------------- analysis
    def _candidates(self, stats_map: dict, incumbent: str | None, metric: str,
                    min_n: int, context: str) -> list[dict]:
        inc = stats_map.get(incumbent)
        out = []
        if not inc:
            return out
        if metric == "engagement":
            inc_k, inc_n = inc["engaged"], inc["connected"]
            inc_gk, inc_gn = inc["connected"], inc["dials"]
        else:
            inc_k, inc_n = inc["connected"], inc["dials"]
            inc_gk, inc_gn = 0, 1
        for arm, s in stats_map.items():
            if arm == incumbent:
                continue
            if metric == "engagement":
                k, n = s["engaged"], s["connected"]
                gk, gn = s["connected"], s["dials"]
            else:
                k, n = s["connected"], s["dials"]
                gk, gn = 0, 1
            if n < min_n or inc_n < 100:
                continue
            diff, lo, hi = stats.risk_diff_ci(k, n, inc_k, inc_n)
            rel = stats.relative_lift(k, n, inc_k, inc_n)
            _, _, p = stats.two_proportion_z(k, n, inc_k, inc_n)
            guard = 0.0
            guard_lo = -1.0
            if metric == "engagement":
                guard, guard_lo, _ = stats.risk_diff_ci(gk, gn, inc_gk, inc_gn)
            out.append({"arm": arm, "context": context, "n": n, "inc_n": inc_n,
                        "rate": k / n if n else 0.0, "inc_rate": inc_k / inc_n if inc_n else 0.0,
                        "diff": diff, "lo": lo, "hi": hi, "rel": rel, "p": p,
                        "guard_diff": guard, "guard_lo": guard_lo})
        return out

    def _analyse(self, wid: str, wf: dict, exp: dict) -> dict:
        start = exp["started_minute"] or self.minute
        payload = exp["payload"]
        incumbent = payload.get("incumbents") or {}
        primary = (wf.get("objective") or {}).get("primary", "engagement_rate")
        min_rel = MIN_REL_LIFT
        candidates = []
        for dim in ("welcome_message", "voice"):
            vstats = analysis.variant_stats(self.conn, wid, dim, start)
            candidates += self._candidates(vstats, incumbent.get(dim), "engagement",
                                           MIN_CONNECTED_GLOBAL, f"global:{dim}")
        age_winners = {}
        for age in cohorts.FEATURES["age_band"]:
            for dim in ("voice",):
                fstats = analysis.variant_feature_stats(self.conn, wid, dim, "age_band", start)
                arm_stats = fstats.get(age) or {}
                cands = self._candidates(arm_stats, incumbent.get(dim), "engagement",
                                         MIN_CONNECTED_SEGMENT, f"age:{age}:{dim}")
                for c in cands:
                    candidates.append(c)
        welcome_segments = []
        for feature in ("lang", "city_tier", "value_band"):
            fstats = analysis.variant_feature_stats(self.conn, wid, "welcome_message", feature, start)
            best = None
            for value, arm_stats in fstats.items():
                cands = self._candidates(arm_stats, incumbent.get("welcome_message"), "engagement",
                                         MIN_CONNECTED_SEGMENT, f"{feature}:{value}:welcome_message")
                for c in cands:
                    candidates.append(c)
                    if c["diff"] > 0 and (best is None or c["rel"] > best["rel"]):
                        c["feature"] = feature
                        c["value"] = value
                        best = c
            if best:
                welcome_segments.append(best)
        pvals = [c["p"] for c in candidates]
        passed = stats.benjamini_hochberg(pvals, q=0.10) if pvals else []
        for c, ok in zip(candidates, passed):
            c["passed"] = bool(ok and c["diff"] > 0 and c["rel"] >= min_rel
                               and c["lo"] > 0 and c["guard_lo"] >= -GUARDRAIL_DROP)
        winners = {}
        for dim in ("welcome_message", "voice"):
            global_c = [c for c in candidates if c["context"] == f"global:{dim}" and c["passed"]]
            if global_c:
                winners[dim] = max(global_c, key=lambda c: (c["rate"], -c["p"]))["arm"]
            else:
                winners[dim] = incumbent.get(dim)
        for dim in ("voice",):
            for age in cohorts.FEATURES["age_band"]:
                seg = [c for c in candidates if c["context"] == f"age:{age}:{dim}" and c["passed"]]
                if seg:
                    best = max(seg, key=lambda c: c["rate"])
                    age_winners[age] = best["arm"]
        welcome_segments = [{"feature": s["feature"], "value": s["value"], "arm": s["arm"],
                             "rel": s["rel"], "n": s["n"],
                             "where": {s["feature"]: [s["value"]]}}
                            for s in welcome_segments if s["passed"]][:2]
        return {"winners": winners, "age_winners": age_winners,
                "welcome_segments": welcome_segments, "candidates": candidates,
                "passed": passed, "incumbent": incumbent, "primary": primary}

    def _strategy_config(self, wid: str, wf: dict, base_cfg: dict, result: dict) -> tuple[dict, dict]:
        ages = cohorts.load_cohorts(self.conn, wid)
        extras = [{"where": s["where"]} for s in result["welcome_segments"]]
        if extras:
            partition = cohorts.build_partition(self.conn, wid, ages, extras, min_share=0.02, cap=20)
        else:
            partition = ages
        winners = result["winners"]
        age_winners = result["age_winners"]
        allocations = {"default": {
            "welcome_message": {winners.get("welcome_message"): 1.0} if winners.get("welcome_message") else
            config.dist(base_cfg, "default", "welcome_message"),
            "voice": {winners.get("voice"): 1.0} if winners.get("voice") else
            config.dist(base_cfg, "default", "voice"),
            "caller_id": dict(config.dist(base_cfg, "default", "caller_id")),
        }}
        for c in partition:
            dims = {}
            for seg in result["welcome_segments"]:
                if all(set(seg["where"].get(f, [])) == set(c["where"].get(f, []))
                       for f in seg["where"]):
                    dims["welcome_message"] = {seg["arm"]: 1.0}
                    break
            age_values = c["where"].get("age_band") or []
            if age_values and age_values[0] in age_winners:
                dims["voice"] = {age_winners[age_values[0]]: 1.0}
            if dims:
                allocations[c["id"]] = dims
        new_cfg = config.compose(base_cfg, cohorts=partition, allocations=allocations,
                                 schedule=base_cfg.get("schedule"))
        return new_cfg, {"partition": partition, "allocations": allocations,
                         "winners": winners, "age_winners": age_winners,
                         "welcome_segments": result["welcome_segments"]}

    def _maybe_checkpoint(self, wid: str, wf: dict) -> None:
        exp = self._exp_row(wid)
        if not exp or exp["phase"] != "exploring":
            return
        if self.minute < (exp["checkpoint_minute"] or 0):
            return
        if self.gov.find_live(wid) is not None:
            return
        start = exp["started_minute"] or self.minute
        elapsed = self.minute - start
        result = self._analyse(wid, wf, exp)
        if not any(c["passed"] for c in result["candidates"]):
            nxt = self.minute + EXTEND_MINUTES
            payload = exp["payload"]
            payload["last_analysis"] = self._analysis_summary(result)
            phase = "exploring" if elapsed < EXPLORE_MAX_MINUTES else "exploiting"
            self._save_exp(wid, phase, "no_winners", start, nxt, payload, self.minute)
            self._record(wid, "checkpoint_hold", "no arm cleared the pre-registered threshold",
                         {"elapsed_minutes": elapsed, "summary": self._analysis_summary(result)})
            return
        base = self._refresh(wid)
        new_cfg, strategy = self._strategy_config(wid, wf, base["config"], result)
        dims = config.changed_dims(base["config"], new_cfg)
        if not dims:
            nxt = self.minute + EXTEND_MINUTES
            payload = exp["payload"]
            payload["last_analysis"] = self._analysis_summary(result)
            phase = "exploring" if elapsed < EXPLORE_MAX_MINUTES else "exploiting"
            self._save_exp(wid, phase, "no_change", start, nxt, payload, self.minute)
            self._record(wid, "checkpoint_hold", "no statistically supported change",
                         {"elapsed_minutes": elapsed, "summary": self._analysis_summary(result)})
            return
        if not self._request_window_open(wf) or not self._can_request(wf):
            self._save_exp(wid, "exploring", "waiting_window", start,
                           min(self.minute + 60, self.minute + 60), exp["payload"], self.minute)
            return
        problems = config.validate(wf, new_cfg)
        if problems:
            self._record(wid, "promotion_invalid", "config rejected locally", {"problems": problems})
            self._save_exp(wid, "exploring", "invalid", start, self.minute + EXTEND_MINUTES,
                           exp["payload"], self.minute)
            return
        where_list = [c["where"] for c in strategy["partition"]] or None
        affected = analysis.affected_leads(self.conn, wid, where_list,
                                           int(wf["daily_batch"]["size"]))
        dry = self._dry_run(wid, base["config"], new_cfg, start, strategy)
        global_lift = 0.0
        for c in result["candidates"]:
            if c["context"] in ("global:welcome_message", "global:voice") and c["passed"]:
                global_lift = max(global_lift, c["rel"])
        share = 1.0
        summary = self._promotion_summary(result, strategy)
        rationale = self._promotion_rationale(result)
        payload = dict(exp["payload"])
        payload["proposed_strategy"] = strategy
        row = self.gov.create(wf, new_cfg, self.minute, self.ts, "promotion", ROUTE_APPROVAL,
                              summary, config.diff(base["config"], new_cfg), affected, dry,
                              config.impact_score(global_lift, share), rationale,
                              expires_in_hours=min(12, int(wf["governance"]["proposal_ttl_hours"])))
        if not row:
            return
        payload["proposal_id"] = row["proposal_id"]
        ok = self.gov.submit_approval(row["proposal_id"], self.minute, self.ts)
        self._save_exp(wid, "promoting" if ok else "exploring", "active" if ok else "submit_failed",
                       start, exp["checkpoint_minute"] if not ok else None, payload, self.minute)
        if ok:
            self._record(wid, "promotion_requested", summary,
                         {"proposal_id": row["proposal_id"], "strategy": strategy,
                          "affected_leads": affected, "dry_run": dry})

    @staticmethod
    def _analysis_summary(result: dict) -> dict:
        return {"winners": result["winners"], "age_winners": result["age_winners"],
                "welcome_segments": result["welcome_segments"],
                "tested": len(result["candidates"]),
                "passed": sum(result["passed"]) if result["passed"] else 0}

    def _promotion_summary(self, result: dict, strategy: dict) -> str:
        bits = []
        for dim in ("welcome_message", "voice"):
            arm = result["winners"].get(dim)
            inc = result["incumbent"].get(dim)
            if arm and arm != inc:
                bits.append(f"{dim} {inc}->{arm}")
        if result["age_winners"]:
            bits.append(f"{len(result['age_winners'])} age-specific voice overrides")
        if result["welcome_segments"]:
            bits.append(f"{len(result['welcome_segments'])} targeted welcome cohorts")
        return "Promote winners: " + ", ".join(bits) if bits else "Promote measured winners"

    def _promotion_rationale(self, result: dict) -> str:
        parts = []
        for c in result["candidates"]:
            if c["passed"] and c["context"].startswith("global:"):
                parts.append(f"{c['context']} {c['arm']} {c['rate']:.3f} vs {c['inc_rate']:.3f} "
                             f"(rel {c['rel']*100:+.1f}%, n={c['n']}, 95% CI {c['lo']*100:+.2f}pp, "
                             f"pickup guard {c['guard_lo']*100:+.2f}pp)")
        if result["welcome_segments"]:
            for s in result["welcome_segments"]:
                parts.append(f"target {s['feature']}={s['value']} -> {s['arm']} "
                             f"(rel {s['rel']*100:+.1f}%, n={s['n']})")
        return "Benjamini-Hochberg q=0.10; " + "; ".join(parts) if parts else \
            "No arm passed the pre-registered promotion threshold; holding."

    def _dry_run(self, wid: str, old_cfg: dict, new_cfg: dict, since: int,
                 strategy: dict | None) -> dict:
        dims = config.changed_dims(old_cfg, new_cfg)
        out = {"window_minutes": max(0, self.minute - since), "method":
               "replay observed arm rates over affected cohorts"}
        for dim in ("welcome_message", "voice", "caller_id"):
            if dim not in dims:
                continue
            stats_map = analysis.variant_stats(self.conn, wid, dim, since)
            if not stats_map:
                continue
            metric = "pickup" if dim == "caller_id" else "engagement"
            rates = {}
            for arm, s in stats_map.items():
                k = s["connected"] if metric == "pickup" else s["engaged"]
                n = s["dials"] if metric == "pickup" else s["connected"]
                rates[arm] = stats.smoothed_rate(k, n, 2.0, 20.0)
            old_w = config.dist(old_cfg, "default", dim)
            new_w = config.dist(new_cfg, "default", dim)
            old_e = sum(old_w.get(a, 0) * r for a, r in rates.items())
            new_e = sum(new_w.get(a, 0) * r for a, r in rates.items())
            out[dim] = {"old": round(old_e, 4), "new": round(new_e, 4),
                        "delta": round(new_e - old_e, 4),
                        "connected": sum(s["connected"] for s in stats_map.values())}
        if not any(k in out for k in ("welcome_message", "voice", "caller_id")):
            out["note"] = "no variant-level change"
        return out

    # ------------------------------------------------------------------ decay
    def _maybe_decay(self, wid: str, wf: dict) -> None:
        exp = self._exp_row(wid)
        if not exp or exp["phase"] != "exploiting":
            return
        if self.minute < (exp["checkpoint_minute"] or 0):
            return
        if self.gov.find_live(wid) is not None:
            return
        payload = exp["payload"]
        strategy = payload.get("strategy") or {}
        winners = strategy.get("winners") or {}
        if not winners:
            self._save_exp(wid, "exploiting", "active", exp["started_minute"],
                           self.minute + 6 * 60, payload, self.minute)
            return
        promoted = payload.get("promoted_minute") or exp["started_minute"] or self.minute
        recent_since = self.minute - 2 * 1440
        prior_since = self.minute - 5 * 1440
        decayed = {}
        for dim, arm in winners.items():
            recent = analysis.variant_stats(self.conn, wid, dim, recent_since)
            prior = analysis.variant_stats(self.conn, wid, dim, prior_since, recent_since)
            r, p = recent.get(arm), prior.get(arm)
            if not r or not p:
                continue
            metric = "engagement"
            k, n = r["engaged"], r["connected"]
            pk, pn = p["engaged"], p["connected"]
            if n < 200 or pn < 200:
                continue
            diff, lo, hi = stats.risk_diff_ci(k, n, pk, pn)
            rel = stats.relative_lift(k, n, pk, pn)
            _, _, pval = stats.two_proportion_z(k, n, pk, pn)
            if rel <= -0.05 and lo < 0 and pval < 0.05:
                decayed[dim] = {"arm": arm, "recent": k / n, "prior": pk / pn,
                                "rel": rel, "n": n, "prior_n": pn, "p": pval}
        if not decayed:
            self._save_exp(wid, "exploiting", "active", exp["started_minute"],
                           self.minute + 6 * 60, payload, self.minute)
            return
        base = self._refresh(wid)
        allocations = self._current_allocations(base["config"])
        incumbent = payload.get("incumbents") or {}
        for dim in decayed:
            inc = incumbent.get(dim)
            if not inc:
                continue
            for dims in allocations.values():
                dims.pop(dim, None)
            allocations.setdefault("default", {})[dim] = {inc: 1.0}
        new_cfg = config.compose(base["config"], schedule=base["config"].get("schedule"),
                                 allocations=allocations)
        if not config.changed_dims(base["config"], new_cfg):
            self._save_exp(wid, "exploiting", "active", exp["started_minute"],
                           self.minute + 6 * 60, payload, self.minute)
            return
        if not self._request_window_open(wf) or not self._can_request(wf):
            self._save_exp(wid, "exploiting", "waiting_window", exp["started_minute"],
                           self.minute + 60, payload, self.minute)
            return
        where_list = None
        affected = analysis.affected_leads(self.conn, wid, where_list, int(wf["daily_batch"]["size"]))
        dry = self._dry_run(wid, base["config"], new_cfg, recent_since, None)
        summary = "Rollback decaying variant: " + ", ".join(f"{d}={v['arm']}" for d, v in decayed.items())
        rationale = "; ".join(f"{d} {v['arm']} fell {v['rel']*100:+.1f}% vs prior window "
                              f"(n={v['n']}, p={v['p']:.4f})" for d, v in decayed.items())
        row = self.gov.create(wf, new_cfg, self.minute, self.ts, "rollback", ROUTE_APPROVAL,
                              summary, config.diff(base["config"], new_cfg), affected, dry, 0.2,
                              rationale, expires_in_hours=min(6, int(wf["governance"]["proposal_ttl_hours"])))
        if not row:
            return
        ok = self.gov.submit_approval(row["proposal_id"], self.minute, self.ts)
        payload["decay"] = decayed
        payload["rollback_proposal_id"] = row["proposal_id"]
        self._save_exp(wid, "rollback" if ok else "exploiting", "active" if ok else "submit_failed",
                       exp["started_minute"], self.minute + 60 if not ok else None, payload, self.minute)
        if ok:
            self._record(wid, "rollback_requested", summary,
                         {"proposal_id": row["proposal_id"], "decay": decayed})
            db.record_alert(self.conn, self.minute, self.ts, wid, "warning", "variant_decay",
                            summary, decayed)

    # -------------------------------------------------------------- incidents
    def _incident_caller_id(self) -> None:
        if self.minute % 60 != 0 or self.minute < CALLER_LOOKBACK_MINUTES:
            return
        since = self.minute - CALLER_LOOKBACK_MINUTES
        for wid, wf in self.wfs.items():
            if self.gov.find_live(wid) is not None:
                continue
            current = self._refresh(wid)
            recent = analysis.variant_stats(self.conn, wid, "caller_id", since)
            usable = {d: r for d, r in recent.items() if r["dials"] >= 300}
            if len(usable) < 2:
                continue
            unhealthy = {}
            for did, r in usable.items():
                peers = [(d, s) for d, s in usable.items() if d != did]
                pk = sum(s["connected"] for _, s in peers)
                pn = sum(s["dials"] for _, s in peers)
                if pn < 600:
                    continue
                diff, _, p = stats.two_proportion_z(r["connected"], r["dials"], pk, pn)
                rel = stats.relative_lift(r["connected"], r["dials"], pk, pn)
                if rel <= -0.25 and diff <= -0.05 and p < 0.001:
                    unhealthy[did] = {"recent_pickup": r["connected"] / r["dials"],
                                      "peer_pickup": pk / pn,
                                      "rel": rel, "p": p, "dials": r["dials"]}
            if not unhealthy:
                continue
            active = {}
            for did, info in list(unhealthy.items()):
                key = f"did_alert_{wid}_{did}"
                if self.minute - self._meta_int(key, -10**9) < 1440:
                    continue
                with self.conn:
                    db.set_meta(self.conn, key, self.minute)
                active[did] = info
            if not active:
                continue
            healthy = {d: max(1e-6, stats.smoothed_rate(r["connected"], r["dials"], 1.0, 20.0))
                       for d, r in usable.items() if d not in active}
            total = sum(healthy.values())
            if total <= 0:
                continue
            target = {d: round(h / total, 4) for d, h in healthy.items()}
            allocations = self._current_allocations(current["config"])
            for cohort, dims in allocations.items():
                if cohort != "default":
                    dims.pop("caller_id", None)
            allocations.setdefault("default", {})["caller_id"] = target
            new_cfg = config.compose(current["config"], schedule=current["config"].get("schedule"),
                                     allocations=allocations)
            if not config.changed_dims(current["config"], new_cfg):
                continue
            problems = config.validate(wf, new_cfg)
            if problems:
                self._record(wid, "caller_id_invalid", "rotation rejected locally", {"problems": problems})
                continue
            ok_auto, _ = self.gov.can_auto(wf, current["config"], new_cfg)
            route = ROUTE_AUTO if ok_auto else ROUTE_APPROVAL
            if route == ROUTE_APPROVAL and (not self._request_window_open(wf) or not self._can_request(wf)):
                continue
            affected = analysis.affected_leads(self.conn, wid, None, int(wf["daily_batch"]["size"]))
            dry = self._dry_run(wid, current["config"], new_cfg, self.minute - 1440, None)
            summary = f"Caller-ID health: rotate away from {','.join(active)}"
            rationale = "; ".join(f"{d} pickup {v['recent_pickup']:.3f} vs peers {v['peer_pickup']:.3f} "
                                  f"(rel {v['rel']*100:+.1f}%, n={v['dials']}, p={v['p']:.4f})"
                                  for d, v in active.items())
            row = self.gov.create(wf, new_cfg, self.minute, self.ts, "caller_id", route,
                                  summary, config.diff(current["config"], new_cfg), affected, dry,
                                  0.3, rationale, expires_in_hours=6)
            if not row:
                continue
            ok = self.gov.submit_auto(row["proposal_id"], self.minute, self.ts) if route == ROUTE_AUTO else \
                self.gov.submit_approval(row["proposal_id"], self.minute, self.ts)
            if ok:
                self._record(wid, "caller_id_rotation", summary,
                             {"proposal_id": row["proposal_id"], "route": route,
                              "unhealthy": active, "target": target})
                db.record_alert(self.conn, self.minute, self.ts, wid, "warning", "caller_id_degraded",
                                summary, active)

    def _credits(self) -> float:
        try:
            return float(self.client.get("/sim/state").json().get("credits_remaining", 0.0))
        except (httpx.HTTPError, ValueError, TypeError):
            return 0.0

    def _incident_hallucination(self) -> None:
        if self.minute % 60 or self.minute < HALLUC_WARMUP_MINUTES:
            return
        since = self.minute - HALLUC_LOOKBACK_MINUTES
        reserve = max(10.0, self.initial_credits * CREDIT_RESERVE_FRAC)
        for wid, wf in self.wfs.items():
            if self.gov.find_live(wid) is not None:
                continue
            key = f"halluc_alert_{wid}"
            if self.minute - self._meta_int(key, -10**9) < HALLUC_COOLDOWN_MINUTES:
                continue
            current = self._refresh(wid)
            cfg = current["config"]
            cur_prompt = cfg.get("prompt_version")
            recent = analysis.variant_stats(self.conn, wid, "prompt_version", since).get(cur_prompt)
            if not recent or recent["connected"] < HALLUC_MIN_CONNECTED or recent["halluc"] < 5:
                continue
            base_all = analysis.variant_stats(self.conn, wid, "prompt_version", -10**9, since)
            base = base_all.get(cur_prompt)
            if base and base["connected"] >= HALLUC_MIN_BASE_CONNECTED:
                bk, bn = base["halluc"], base["connected"]
            else:
                bk = sum(s["halluc"] for s in base_all.values())
                bn = sum(s["connected"] for s in base_all.values())
            if bn < HALLUC_MIN_BASE_CONNECTED:
                continue
            diff, _, p = stats.two_proportion_z(recent["halluc"], recent["connected"], bk, bn)
            rrate = recent["halluc"] / recent["connected"]
            brate = bk / bn if bn else 0.0
            if diff < HALLUC_MIN_DIFF or p >= HALLUC_PVALUE:
                continue
            if not self._request_window_open(wf) or not self._can_request(wf):
                continue
            if self._credits() - reserve < 8:
                continue
            attempt_key = f"halluc_attempts_{wid}_{self.minute // 1440}"
            attempts = self._meta_int(attempt_key, 0)
            if attempts >= HALLUC_MAX_ATTEMPTS_PER_DAY:
                continue
            with self.conn:
                db.set_meta(self.conn, attempt_key, attempts + 1)
            with self.conn:
                db.record_alert(
                    self.conn, self.minute, self.ts, wid, "critical", "hallucination_spike",
                    f"{wid}: hallucination {rrate:.3f} vs {brate:.3f} baseline",
                    {"recent": recent, "baseline": {"halluc": bk, "connected": bn},
                     "diff": diff, "p_value": p, "prompt_version": cur_prompt})
            problem = (f"Hallucination rate rose to {rrate:.3f} over {recent['connected']} connected "
                       f"calls from a {brate:.3f} baseline on prompt {cur_prompt}.")
            try:
                r = self.client.post("/llm/suggest-fixes",
                                     json={"workflow_id": wid, "problem": problem})
                if r.status_code != 200:
                    self._record(wid, "llm_suggest_failed", f"HTTP {r.status_code}",
                                 {"body": r.text[:200]})
                    continue
                suggestions = r.json().get("suggestions") or []
            except httpx.HTTPError as exc:
                self._record(wid, "llm_suggest_error", str(exc), {})
                continue
            options = wf.get("options", {}).get("prompt_version") or []
            catalog = wf.get("prompt_catalog") or {}
            candidates: list[dict] = []
            for suggestion in suggestions:
                cand = suggestion.get("prompt_version")
                summary = suggestion.get("summary") or ""
                if not _prompt_candidate_ok(cur_prompt, cand, summary, options):
                    continue
                candidates.append({"prompt_version": cand, "summary": summary})
            history = analysis.variant_stats(self.conn, wid, "prompt_version", -10**9, since)
            ranked = sorted(
                (s["halluc"] / s["connected"], p) for p, s in history.items()
                if p in options and p != cur_prompt and s["connected"] >= 500)
            for _, cand in ranked[:2]:
                if any(c["prompt_version"] == cand for c in candidates):
                    continue
                summary = catalog.get(cand, "")
                if not _prompt_candidate_ok(cur_prompt, cand, summary, options):
                    continue
                candidates.append({"prompt_version": cand, "summary": summary})
            chosen = None
            tried: list[dict] = []
            for candidate in candidates:
                cand = candidate["prompt_version"]
                summary = candidate["summary"]
                if self._credits() - reserve < 4:
                    break
                try:
                    vr = self.client.post("/validation/runs",
                                          json={"workflow_id": wid, "prompt_version": cand, "n": 400})
                    if vr.status_code != 200:
                        tried.append({"prompt_version": cand, "error": vr.text[:120]})
                        continue
                    run = vr.json()
                except httpx.HTTPError as exc:
                    tried.append({"prompt_version": cand, "error": str(exc)})
                    continue
                tried.append({"prompt_version": cand, "pass_rate": run.get("pass_rate"),
                              "validation_run_id": run.get("validation_run_id"),
                              "summary": summary})
                if float(run.get("pass_rate") or 0.0) >= MIN_PROMPT_PASS:
                    chosen = (cand, summary, run)
                    break
            if not chosen:
                with self.conn:
                    db.set_meta(self.conn, key, self.minute)
                self._record(wid, "prompt_fix_unavailable",
                             "no suggested prompt passed the hallucination safety floor",
                             {"halluc_rate": rrate, "baseline": brate, "tried": tried})
                continue
            cand, summary, run = chosen
            new_cfg = config.compose(cfg, prompt_version=cand)
            problems = config.validate(wf, new_cfg)
            if problems:
                self._record(wid, "prompt_fix_invalid", "proposed config rejected locally",
                             {"problems": problems})
                continue
            evidence = {"validation_run_id": run["validation_run_id"],
                        "pass_rate": run["pass_rate"], "n": run.get("n", 400),
                        "workflow_id": wid, "prompt_version": cand}
            affected = analysis.affected_leads(
                self.conn, wid, None, int(wf.get("daily_batch", {}).get("size", 0)))
            dry = {"validation": evidence, "recent_hallucination": round(rrate, 4),
                   "baseline_hallucination": round(brate, 4)}
            text = (f"Fix hallucination spike on {wid}: {cur_prompt}->{cand} "
                    f"(validated pass {run['pass_rate']:.3f} on n={run.get('n', 400)})")
            rationale = (f"Recent hallucination {rrate:.3f} vs {brate:.3f} baseline "
                         f"(diff {diff * 100:+.2f}pp, p={p:.5f}, n={recent['connected']}). "
                         f"Candidate {cand}: {summary}")
            ttl = int((wf.get("governance") or {}).get("proposal_ttl_hours", 12))
            row = self.gov.create(wf, new_cfg, self.minute, self.ts, "prompt_fix", ROUTE_APPROVAL,
                                  text, config.diff(cfg, new_cfg), affected, dry, 0.6, rationale,
                                  evidence=evidence, expires_in_hours=min(12, ttl))
            if not row:
                continue
            ok = self.gov.submit_approval(row["proposal_id"], self.minute, self.ts)
            with self.conn:
                db.set_meta(self.conn, key, self.minute)
            if ok:
                self._record(wid, "prompt_fix_requested", text,
                             {"proposal_id": row["proposal_id"], "prompt_version": cand,
                              "validation": evidence, "halluc_rate": rrate,
                              "baseline": brate, "tried": tried})


    # --------------------------------------------------------------- telemetry
    def _record_metrics(self) -> None:
        lag = 0
        try:
            state = self.client.get("/sim/state").json()
            lag = int(state["sim_minute"]) - analysis.latest_minute(self.conn, next(iter(self.wfs)))
        except (httpx.HTTPError, StopIteration):
            lag = 0
        with self.conn:
            self.conn.execute(
                "INSERT INTO metrics(minute, wall_s, events_ingested, ingest_s, events_per_s, "
                "lag_minutes, credits_remaining, rss_mb, decisions) VALUES(?,?,?,?,?,?,?,?,?)",
                (self.minute, 0.0, 0, 0.0, 0.0, lag, 0.0, 0.0, 0))
