"""Governance client: proposals, approvals, auto-apply and crash reconciliation.

The simulator is the authority for approval state. SQLite records intent before
each network call, so a kill between request and response is recoverable without
double-applying or losing a pending approval.
"""
from __future__ import annotations

import json
import uuid

import httpx

from . import config, db

LIVE_STATUSES = ("prepared", "submitted", "pending", "approved", "applying")
ROUTE_APPROVAL = "approval"
ROUTE_AUTO = "auto"


class Governance:
    def __init__(self, client: httpx.Client, conn):
        self.client = client
        self.conn = conn

    def _wf_current(self, workflow_id: str) -> dict:
        r = self.client.get(f"/workflows/{workflow_id}/config")
        r.raise_for_status()
        return r.json()

    def requests_last_24h(self, workflow_id: str, now: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE workflow_id=? AND created_minute > ? "
            "AND status NOT IN ('prepared','failed','duplicate')", (workflow_id, now - 1440)).fetchone()
        return int(row[0] or 0)

    def can_auto(self, wf: dict, old: dict, new: dict) -> tuple[bool, list[str]]:
        gov = wf["governance"]
        pol = gov.get("auto_apply") or {}
        dims = config.changed_dims(old, new)
        reasons = []
        if not pol.get("enabled"):
            reasons.append("auto-apply disabled")
        bad = [d for d in dims if d not in pol.get("allowed_dims", [])]
        if bad:
            reasons.append(f"dims not allowed: {bad}")
        shift = config.max_shift(old, new)
        if shift > float(pol.get("max_shift", 0)) + 1e-9:
            reasons.append(f"shift {shift:.3f} > {pol.get('max_shift')}")
        if not dims:
            reasons.append("no change")
        return not reasons, reasons

    def create(self, wf: dict, proposed: dict, minute: int, ts: str, kind: str,
               route: str, summary: str, diff: dict, affected_leads: int, dry_run: dict,
               impact_score: float, rationale: str, evidence: dict | None = None,
               expires_in_hours: float | None = None) -> dict | None:
        workflow_id = wf["workflow_id"]
        base = self._wf_current(workflow_id)
        if base["version"] != (wf.get("current_version") or base["version"]):
            wf["current_version"] = base["version"]
        chash = config.config_hash(proposed)
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM proposals WHERE workflow_id=? AND config_hash=? AND status IN "
                "('prepared','submitted','pending','approved','applying') ORDER BY rowid DESC LIMIT 1",
                (workflow_id, chash)).fetchone()
        if row:
            return dict(row)
        dims = config.changed_dims(base["config"], proposed)
        if not dims:
            return None
        pid = f"{workflow_id}:{kind}:{base['version']}:{chash}:{uuid.uuid4().hex[:8]}"
        payload = {"change_summary": summary, "diff": diff, "affected_leads": int(affected_leads),
                   "dry_run": dry_run, "impact_score": float(impact_score), "rationale": rationale,
                   "evidence": evidence or {}, "kind": kind, "route": route,
                   "expires_in_hours": expires_in_hours, "dims": dims}
        with self.conn:
            self.conn.execute(
                "INSERT INTO proposals(proposal_id, workflow_id, base_version, proposed_config, config_hash, "
                "kind, status, created_minute, created_ts, updated_minute, payload) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (pid, workflow_id, base["version"], json.dumps(proposed, sort_keys=True), chash, kind,
                 "prepared", minute, ts, minute, json.dumps(payload, sort_keys=True)))
            db.record_decision(self.conn, minute, ts, workflow_id, "proposal_created",
                               f"{kind}: {summary}",
                               {"proposal_id": pid, "dims": dims, "route": route,
                                "impact_score": impact_score, "affected_leads": affected_leads})
        return dict(self.conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (pid,)).fetchone())

    def _payload(self, row: dict) -> dict:
        return json.loads(row["payload"] or "{}")

    def _set_status(self, proposal_id: str, status: str, minute: int, note: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE proposals SET status=?, updated_minute=?, note=COALESCE(?, note) "
                              "WHERE proposal_id=?", (status, minute, note, proposal_id))

    def _upsert_approval(self, approval: dict, minute: int, proposed_config: str | None = None) -> None:
        reasons = json.dumps(approval.get("reasons") or [])
        with self.conn:
            self.conn.execute(
                "INSERT INTO approvals(approval_id, proposal_id, workflow_id, status, base_version, "
                "changed_dims, created_at, expires_at, apply_by, decided_at, reasons, proposed_config, "
                "last_synced_minute) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(approval_id) DO UPDATE SET status=excluded.status, base_version=excluded.base_version, "
                "changed_dims=excluded.changed_dims, expires_at=excluded.expires_at, apply_by=excluded.apply_by, "
                "decided_at=excluded.decided_at, reasons=excluded.reasons, last_synced_minute=excluded.last_synced_minute",
                (approval.get("approval_id"), approval.get("proposal_id"), approval.get("workflow_id"),
                 approval.get("status"), approval.get("base_version"),
                 json.dumps(approval.get("changed_dims") or []), approval.get("created_at"),
                 approval.get("expires_at"), approval.get("apply_by"), approval.get("decided_at"), reasons,
                 proposed_config, minute))

    def _error(self, response: httpx.Response, minute: int, ts: str, workflow_id: str, kind: str) -> str:
        try:
            body = response.json()
        except Exception:
            body = {"detail": response.text[:500]}
        code = str(body.get("error") or body.get("detail") or response.status_code)
        db.record_decision(self.conn, minute, ts, workflow_id, kind, f"{response.status_code} {code}",
                           {"status": response.status_code, "body": body})
        return code

    def submit_approval(self, proposal_id: str, minute: int, ts: str) -> bool:
        row = self.conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row or row["status"] not in ("prepared", "submitted"):
            return False
        payload = self._payload(dict(row))
        body = {"proposal_id": proposal_id, "workflow_id": row["workflow_id"],
                "base_version": row["base_version"], "proposed_config": json.loads(row["proposed_config"]),
                "change_summary": payload.get("change_summary", ""), "diff": payload.get("diff") or {"x": 1},
                "affected_leads": payload.get("affected_leads", 0), "dry_run": payload.get("dry_run") or {"x": 1},
                "impact_score": payload.get("impact_score", 0.1), "rationale": payload.get("rationale", ""),
                "evidence": payload.get("evidence") or {}}
        if payload.get("expires_in_hours"):
            body["expires_in_hours"] = payload["expires_in_hours"]
        try:
            r = self.client.post("/approvals", json=body)
        except httpx.HTTPError as exc:
            db.record_alert(self.conn, minute, ts, row["workflow_id"], "warning", "approval_network",
                            f"approval POST failed: {exc}", {"proposal_id": proposal_id})
            return False
        if r.status_code == 200:
            approval = r.json()
            self._upsert_approval(approval, minute, row["proposed_config"])
            with self.conn:
                self.conn.execute("UPDATE proposals SET status='pending', approval_id=?, updated_minute=? "
                                  "WHERE proposal_id=?", (approval["approval_id"], minute, proposal_id))
                db.record_decision(self.conn, minute, ts, row["workflow_id"], "approval_requested",
                                   f"{approval['approval_id']} {payload.get('change_summary','')}",
                                   {"proposal_id": proposal_id, "approval_id": approval["approval_id"],
                                    "dims": payload.get("dims")})
            return True
        code = self._error(r, minute, ts, row["workflow_id"], "approval_request_failed")
        if r.status_code == 409 and code == "duplicate_proposal_id":
            found = self._find_by_proposal(proposal_id, minute)
            if found:
                return True
            self._set_status(proposal_id, "duplicate", minute, "duplicate id without matching approval")
        elif r.status_code in (409,) and code == "stale_base_version":
            self._set_status(proposal_id, "stale", minute, code)
        else:
            self._set_status(proposal_id, "failed", minute, code)
        return False

    def _find_by_proposal(self, proposal_id: str, minute: int) -> dict | None:
        try:
            approvals = self.client.get("/approvals").json()
        except httpx.HTTPError:
            return None
        for a in approvals:
            if a.get("proposal_id") == proposal_id:
                row = self.conn.execute("SELECT proposed_config FROM proposals WHERE proposal_id=?",
                                        (proposal_id,)).fetchone()
                self._upsert_approval(a, minute, row["proposed_config"] if row else None)
                with self.conn:
                    self.conn.execute("UPDATE proposals SET status=?, approval_id=?, updated_minute=? "
                                      "WHERE proposal_id=?", (a["status"], a["approval_id"], minute, proposal_id))
                return a
        return None

    def submit_auto(self, proposal_id: str, minute: int, ts: str) -> bool:
        row = self.conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row or row["status"] not in ("prepared", "submitted"):
            return False
        payload = self._payload(dict(row))
        body = {"proposal_id": proposal_id, "base_version": row["base_version"],
                "proposed_config": json.loads(row["proposed_config"]),
                "rationale": payload.get("rationale", ""), "evidence": payload.get("evidence") or {}}
        try:
            r = self.client.put(f"/workflows/{row['workflow_id']}/config", json=body)
        except httpx.HTTPError as exc:
            db.record_alert(self.conn, minute, ts, row["workflow_id"], "warning", "auto_network",
                            f"auto-apply failed: {exc}", {"proposal_id": proposal_id})
            return False
        if r.status_code == 200:
            result = r.json()
            with self.conn:
                self.conn.execute("UPDATE proposals SET status='applied', updated_minute=? WHERE proposal_id=?",
                                  (minute, proposal_id))
                db.record_decision(self.conn, minute, ts, row["workflow_id"], "auto_applied",
                                   f"v{result.get('version')} {','.join(result.get('changed_dims') or [])}",
                                   {"proposal_id": proposal_id, "version": result.get("version"),
                                    "dims": result.get("changed_dims"), "impact_score": payload.get("impact_score")})
            return True
        code = self._error(r, minute, ts, row["workflow_id"], "auto_apply_rejected")
        if r.status_code == 409 and code == "duplicate_proposal_id":
            current = self._wf_current(row["workflow_id"])
            if config.config_hash(current["config"]) == row["config_hash"]:
                self._set_status(proposal_id, "applied", minute, "reconciled duplicate")
                return True
            self._set_status(proposal_id, "duplicate", minute, code)
        elif r.status_code == 409:
            self._set_status(proposal_id, "stale", minute, code)
        else:
            self._set_status(proposal_id, "auto_rejected", minute, code)
        return False

    def _apply(self, proposal: dict, approval: dict, minute: int, ts: str) -> None:
        workflow_id = proposal["workflow_id"]
        current = self._wf_current(workflow_id)
        if current["version"] != approval.get("base_version", proposal["base_version"]):
            self._set_status(proposal["proposal_id"], "stale", minute, "stale_base_version")
            with self.conn:
                db.record_decision(self.conn, minute, ts, workflow_id, "apply_conflict",
                                   f"v{current['version']} != base {approval.get('base_version')}",
                                   {"proposal_id": proposal["proposal_id"],
                                    "approval_id": approval.get("approval_id")})
            return
        try:
            r = self.client.post(f"/workflows/{workflow_id}/apply",
                                 json={"approval_id": approval["approval_id"]})
        except httpx.HTTPError as exc:
            db.record_alert(self.conn, minute, ts, workflow_id, "warning", "apply_network",
                            f"apply failed: {exc}", {"approval_id": approval.get("approval_id")})
            return
        if r.status_code == 200:
            result = r.json()
            with self.conn:
                self.conn.execute("UPDATE proposals SET status='applied', updated_minute=? WHERE proposal_id=?",
                                  (minute, proposal["proposal_id"]))
                self.conn.execute("UPDATE approvals SET status='applied', applied_version=?, "
                                  "last_synced_minute=? WHERE approval_id=?",
                                  (result.get("version"), minute, approval["approval_id"]))
                db.record_decision(self.conn, minute, ts, workflow_id, "applied",
                                   f"v{result.get('version')} {','.join(result.get('changed_dims') or [])}",
                                   {"proposal_id": proposal["proposal_id"],
                                    "approval_id": approval["approval_id"],
                                    "version": result.get("version"), "dims": result.get("changed_dims")})
            return
        code = self._error(r, minute, ts, workflow_id, "apply_failed")
        if r.status_code == 410:
            self._set_status(proposal["proposal_id"], "lapsed", minute, code)
            with self.conn:
                self.conn.execute("UPDATE approvals SET status='lapsed' WHERE approval_id=?",
                                  (approval["approval_id"],))
        elif r.status_code == 409:
            self._set_status(proposal["proposal_id"], "stale", minute, code)
        else:
            self._set_status(proposal["proposal_id"], "apply_failed", minute, code)

    def poll(self, minute: int, ts: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT * FROM proposals WHERE status IN ('prepared','submitted','pending','approved','applying') "
            "ORDER BY rowid").fetchall()
        changed = []
        for raw in rows:
            row = dict(raw)
            payload = self._payload(row)
            route = payload.get("route", ROUTE_APPROVAL)
            if row["status"] in ("prepared", "submitted"):
                ok = self.submit_auto(row["proposal_id"], minute, ts) if route == ROUTE_AUTO else \
                    self.submit_approval(row["proposal_id"], minute, ts)
                if ok:
                    changed.append(row["proposal_id"])
                continue
            aid = row["approval_id"]
            if not aid:
                self._set_status(row["proposal_id"], "failed", minute, "missing approval id")
                continue
            try:
                a = self.client.get(f"/approvals/{aid}").json()
            except httpx.HTTPError as exc:
                db.record_alert(self.conn, minute, ts, row["workflow_id"], "warning", "approval_poll",
                                f"approval poll failed: {exc}", {"approval_id": aid})
                continue
            status = a.get("status")
            self._upsert_approval(a, minute, row["proposed_config"])
            if status in ("expired", "rejected", "lapsed"):
                if row["status"] != status:
                    self._set_status(row["proposal_id"], status, minute, ",".join(a.get("reasons") or []))
                    with self.conn:
                        db.record_decision(self.conn, minute, ts, row["workflow_id"], f"approval_{status}",
                                           f"{aid}", {"proposal_id": row["proposal_id"], "approval_id": aid,
                                                      "reasons": a.get("reasons") or []})
                        if status == "lapsed":
                            db.record_alert(self.conn, minute, ts, row["workflow_id"], "warning",
                                            "approval_lapsed", f"{aid} lapsed", {"approval_id": aid})
                changed.append(row["proposal_id"])
            elif status == "approved":
                if row["status"] != "applying":
                    self._set_status(row["proposal_id"], "applying", minute)
                    with self.conn:
                        self.conn.execute("UPDATE approvals SET status='approved' WHERE approval_id=?", (aid,))
                self._apply(row, a, minute, ts)
                changed.append(row["proposal_id"])
            elif status == "applied":
                if row["status"] != "applied":
                    self._set_status(row["proposal_id"], "applied", minute, "reconciled")
                changed.append(row["proposal_id"])
        return changed

    def reconcile(self, minute: int, ts: str) -> None:
        try:
            approvals = self.client.get("/approvals").json()
        except httpx.HTTPError:
            approvals = []
        by_proposal = {a.get("proposal_id"): a for a in approvals if a.get("proposal_id")}
        rows = self.conn.execute("SELECT * FROM proposals WHERE status IN "
                                 "('prepared','submitted','pending','approved','applying')").fetchall()
        for raw in rows:
            row = dict(raw)
            if row["proposal_id"] in by_proposal:
                a = by_proposal[row["proposal_id"]]
                self._upsert_approval(a, minute, row["proposed_config"])
                if a.get("status") in ("approved", "applied") or row["status"] == "applying":
                    with self.conn:
                        self.conn.execute("UPDATE proposals SET status=?, approval_id=?, updated_minute=? "
                                          "WHERE proposal_id=?",
                                          ("approved" if a.get("status") == "approved" else a.get("status"),
                                           a["approval_id"], minute, row["proposal_id"]))
        self.poll(minute, ts)

    def find_live(self, workflow_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE workflow_id=? AND status IN "
            "('prepared','submitted','pending','approved','applying') ORDER BY rowid DESC LIMIT 1",
            (workflow_id,)).fetchone()
        return dict(row) if row else None
