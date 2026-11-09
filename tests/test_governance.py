import copy
import json

import httpx
import pytest

from controller import config, db
from controller.governance import Governance


def base_config():
    return {"cohorts": [], "allocations": {"default": {
        "welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0},
        "caller_id": {"D1": 0.5, "D2": 0.5}}}, "prompt_version": "P1",
        "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}


class FakeSim:
    def __init__(self, approve=True):
        self.version = 1
        self.config = base_config()
        self.approvals = {}
        self.proposals = set()
        self.approve = approve
        self.applied_versions = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/config") and request.method == "GET":
            return httpx.Response(200, json={"workflow_id": "wf", "version": self.version,
                                             "config": copy.deepcopy(self.config)})
        if path == "/approvals" and request.method == "POST":
            body = json.loads(request.content)
            if body["proposal_id"] in self.proposals:
                return httpx.Response(409, json={"error": "duplicate_proposal_id"})
            self.proposals.add(body["proposal_id"])
            aid = f"apr_{len(self.approvals) + 1:05d}"
            self.approvals[aid] = {"approval_id": aid, "proposal_id": body["proposal_id"],
                                   "workflow_id": body["workflow_id"], "status": "pending",
                                   "base_version": body["base_version"], "changed_dims": ["voice"],
                                   "proposed_config": body["proposed_config"], "reasons": []}
            a = self.approvals[aid]
            return httpx.Response(200, json={k: a[k] for k in ("approval_id", "proposal_id", "workflow_id",
                                                              "status", "base_version", "changed_dims")} |
                                           {"created_at": "x", "expires_at": "y"})
        if path.startswith("/approvals/") and request.method == "GET":
            aid = path.split("/")[-1]
            a = self.approvals[aid]
            if a["status"] == "pending" and self.approve:
                a["status"] = "approved"
            return httpx.Response(200, json={k: a[k] for k in ("approval_id", "proposal_id", "workflow_id",
                                                              "status", "base_version", "changed_dims")} |
                                           {"reasons": a.get("reasons", [])})
        if path == "/approvals" and request.method == "GET":
            return httpx.Response(200, json=[{k: a[k] for k in ("approval_id", "proposal_id", "workflow_id",
                                                               "status", "base_version", "changed_dims")}
                                             for a in self.approvals.values()])
        if path.endswith("/apply") and request.method == "POST":
            body = json.loads(request.content)
            a = self.approvals[body["approval_id"]]
            if a["status"] != "approved":
                return httpx.Response(409, json={"error": "not_approved"})
            self.config = a["proposed_config"]
            self.version += 1
            a["status"] = "applied"
            self.applied_versions.append(self.version)
            return httpx.Response(200, json={"workflow_id": "wf", "version": self.version,
                                             "changed_dims": ["voice"]})
        if path.endswith("/config") and request.method == "PUT":
            body = json.loads(request.content)
            self.config = body["proposed_config"]
            self.version += 1
            self.applied_versions.append(self.version)
            return httpx.Response(200, json={"workflow_id": "wf", "version": self.version,
                                             "changed_dims": ["caller_id"]})
        return httpx.Response(404, json={"error": "not_found"})


def wf(governance=None):
    return {"workflow_id": "wf", "current_version": 1,
            "governance": governance or {"auto_apply": {"enabled": True,
                                                        "allowed_dims": ["schedule", "caller_id"],
                                                        "max_shift": 0.5},
                                        "approver_hours": [10, 19], "max_requests_per_24h": 3,
                                        "proposal_ttl_hours": 24}}


def proposal_body():
    new = config.compose(base_config(), allocations={"default": {"voice": {"V1": 0.8, "V2": 0.2}}})
    return {"proposed": new, "minute": 600, "ts": "2027-01-04T10:00:00+05:30", "kind": "experiment",
            "route": "approval", "summary": "test", "diff": {"voice": {"default": {"from": {"V1": 1},
                                                                                 "to": {"V1": 0.8, "V2": 0.2}}}},
            "affected_leads": 100, "dry_run": {"n": 10}, "impact_score": 0.2, "rationale": "test"}


def test_approval_lifecycle_and_no_double_apply():
    sim = FakeSim()
    client = httpx.Client(base_url="http://sim", transport=httpx.MockTransport(sim.handler))
    conn = db.connect(":memory:")
    gov = Governance(client, conn)
    body = proposal_body()
    row = gov.create(wf(), body["proposed"], body["minute"], body["ts"], body["kind"], body["route"],
                     body["summary"], body["diff"], body["affected_leads"], body["dry_run"],
                     body["impact_score"], body["rationale"])
    assert gov.requests_last_24h("wf", 600) == 0
    assert gov.submit_approval(row["proposal_id"], body["minute"], body["ts"])
    assert sim.approvals["apr_00001"]["status"] == "pending"
    assert gov.requests_last_24h("wf", 601) == 1
    gov.poll(body["minute"], body["ts"])
    assert sim.version == 2 and sim.applied_versions == [2]
    gov.poll(body["minute"], body["ts"])
    assert sim.applied_versions == [2]
    assert conn.execute("SELECT status FROM proposals").fetchone()[0] == "applied"


def test_auto_apply_and_policy():
    sim = FakeSim()
    client = httpx.Client(base_url="http://sim", transport=httpx.MockTransport(sim.handler))
    conn = db.connect(":memory:")
    gov = Governance(client, conn)
    old = base_config()
    new = config.compose(old, allocations={"default": {"caller_id": {"D1": 1.0}}})
    ok, reasons = gov.can_auto(wf(), old, new)
    assert ok and not reasons
    body = proposal_body()
    row = gov.create(wf(), new, 600, "x", "caller_id", "auto", "spread", {"caller_id": {}}, 10,
                     {"n": 1}, 0.1, "healthy")
    assert gov.submit_auto(row["proposal_id"], 600, "x")
    assert sim.version == 2
    voice = config.compose(old, allocations={"default": {"voice": {"V1": 0.8, "V2": 0.2}}})
    ok, reasons = gov.can_auto(wf(), old, voice)
    assert not ok and any("dims not allowed" in r for r in reasons)


def test_crash_restart_reconciles_without_double_apply(tmp_path):
    path = tmp_path / "state.db"
    sim = FakeSim()
    client = httpx.Client(base_url="http://sim", transport=httpx.MockTransport(sim.handler))
    conn = db.connect(path)
    gov = Governance(client, conn)
    body = proposal_body()
    row = gov.create(wf(), body["proposed"], 600, "x", "experiment", "approval", "test", {"d": 1},
                     100, {"n": 1}, 0.2, "test")
    assert gov.submit_approval(row["proposal_id"], 600, "x")
    conn.close()
    conn = db.connect(path)
    gov = Governance(client, conn)
    gov.reconcile(601, "x")
    assert sim.version == 2 and sim.applied_versions == [2]
    gov.reconcile(602, "x")
    assert sim.applied_versions == [2]
    assert conn.execute("SELECT status FROM proposals").fetchone()[0] == "applied"
