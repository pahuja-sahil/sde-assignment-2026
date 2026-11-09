# Outcome-First Agent Loop — Submission

**Name:** Sahil Pahuja · **Date:** 2026-11-09 · **Hours actually spent:** Not tracked · **Language / storage:** Python 3.12 / SQLite (WAL)

## 0. TL;DR
The controller ingests simulator history/events into SQLite, tests welcome-message and voice variants, routes proposals through governance, and responds to caller-ID and prompt incidents.
On the checked public runs, `public_01` improved total conclusion rate by 1.8% relative to the no-op baseline; `public_02` improved it by 6.5%, but applied one harmful KYC change.
It preserves decision and approval records and has a kill/restart test log.
It does not meet every constraint yet: the analysis currently assumes engagement is primary, and the public_03 scale envelope has not been measured.
Per-person call-time optimization, a supervisor UI, and fair multi-tenant credit scheduling were cut.

## 1. Assumptions
1. The simulator HTTP API is the controller's only source of workflow, lead, event, approval, validation, and run-state information. The controller does not use scenario truth to make decisions.
2. The public scenarios and their API schemas are representative of hidden scenarios, but their outcome rates are not assumed to generalize.
3. Historical event chunks are immutable enough to resume by chunk cursor; event IDs are stable and suitable for deduplication.
4. SQLite on a persistent local volume is acceptable for this single-process assignment deployment. A multi-instance production deployment would need a different concurrency and storage design.
5. The configured dialling window, cadence, and capacity are business-owned. The controller does not change cadence or capacity; its historical hour weights only reweight scheduling within the allowed window.
6. Statistical thresholds are initial operating choices, not calibrated business guarantees. The 10% Benjamini-Hochberg level is a target false-discovery control under its assumptions, not a measured empirical false-winner rate.

## 2. Scope decisions
| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion at load-scenario scale | Partial | Chunk cursors, event-ID deduplication, and transactional writes support replay and restart. The required public_03 throughput/lag benchmark is not recorded. | Run public_03 on the target 4-vCPU/8-GB Linux host; measure peak RSS, ingestion rate, decision lag, and simulated-day wall time; optimize from evidence. |
| C2 | Lead/feature joins and one shared cohort object | Partial | Leads and features are joined for analysis; persisted cohort IDs are used in configuration and analysis. There is no supervisor view consuming them. | Expose the same persisted cohort model through a consolidated, authorized supervisor surface and test membership consistency end-to-end. |
| C3 | Bootstrapping from history before D0 | Built | History is ingested before the live loop and used to form age cohorts and historical hour weights. | Verify on load and hidden scenarios; add bounds and freshness monitoring. |
| C4 | Call-time optimization | Partial | Historical hour weights are derived globally and for supported age cohorts. Per-person timing is not implemented. | Evaluate the scheduling contract and fairness constraints, then add an online-safe per-cohort/person policy and measure it separately. |
| C5 | Caller-ID health | Built for the exercised incident path | Population-level degradation detection and a policy-checked rotation proposal are implemented; the public_02 Kestrel flagged-ID share fell to zero after day 2. | Add broader scenarios, calibrated thresholds, and explicit exploration safeguards. |
| C6 | Welcome/voice experimentation | Partial | Randomized allocations, sample floors, confidence intervals, and Benjamini-Hochberg correction are used. Interactions between welcome and voice are not modeled, and the wrong KPI is currently used for workflows whose primary KPI is pickup. | Fix objective/guardrail selection first; then test interaction-aware designs and calibrate sample/window choices. |
| C7 | Winner promotion, decay detection and rollback | Partial | Promotion and a 2-day-vs-prior-window decay check can propose rollback. The public reports show variant-share decay but do not establish a successful, independently verified rollback. | Add a focused decay/rollback test with an observed post-rollback outcome and demonstrate the rollback path in a report. |
| C8 | Hallucination incident response | Partial | The controller detects a statistically significant spike, requests candidate prompts, validates them, and attaches validation evidence before approval. Public_02 used 11 credits, two validations and one LLM fix call; the report does not provide a complete per-incident fix/verification timeline. | Add auditable stage timestamps, verification-window criteria, and a test proving the applied prompt reduced hallucination without breaching engagement. |
| C9 | Governance client | Partial | Approval/auto-apply policy checks, lifecycle reconciliation, approver-hour timing, request caps, and stale/lapsed handling are implemented. Independent changes are not generally batched into one request; expiry/reproposal behavior needs more scenario coverage. | Add lifecycle fault-injection tests and safe batching that preserves per-change evidence and policy checks. |
| C10 | Impact, affected leads and dry runs | Partial | Proposals include these fields, but some impact scores are fixed heuristics and the dry run is a smoothed historical-arm replay, not a full simulator replay. | Calibrate scores against realized outcomes, quantify uncertainty, and replay the exact proposed policy over representative historical cohorts. |
| C11 | Consolidated supervisor view | Cut | A UI/API was beyond the selected implementation scope. | Build a role-authorized view over proposal, cohort, alert, experiment, and change history; usability-test it with supervisors. |
| C12 | Queryable decision/audit store | Built | SQLite records decisions, proposals, approvals, config-change events, and experiment state. | Add retention, export, and access-control policies for production. |
| C13 | Crash recovery, demonstrated | Built for the recorded test | `reports/crash_test_01.log` records a forced stop at simulated minute 5040, restart from minute 5040, and completion. | Repeat with crashes at more network/database boundaries and assert the resulting audit/config state. |
| C14 | Loop observability | Partial | An alerts table exists and incident paths record alerts. The metrics writer currently stores zero placeholders for several operational measurements; there is no dashboard or paging integration. | Instrument actual ingest duration/rate, lag, RSS, credit burn and decisions; define actionable alert routes and test them. |
| C15 | Fair allocation of credits and approver attention | Cut | There is a per-workflow request cap and a credit reserve for prompt work, but no tenant-level fair scheduler. | Add tenant budgets/quotas and starvation tests across workflows with different incident rates. |

The selection favored a working governance-backed experiment loop, incident response, and durable audit/restart state over broad but shallow UI and optimization features. The implementation is not yet a compliant final controller: objective/guardrail selection and public_03 proof are submission-critical follow-ups.

## 3. Stakeholders

### 01 — Sales
- **Accepted:** Show measured outcomes separately by workflow and keep request volume controlled.
- **Pushed back on:** “Winning by lunch” is not enough evidence to promote; no blended KPI can replace pickup and engagement. I did not roll out a demo prompt across workflows.
- **Wrong against policy:** Kestrel’s auto-apply is not “for everything”: its public_02 policy permits only listed dimensions and limits maximum shift. Northwind KYC has auto-apply disabled. Northwind renewal permits only schedule/caller-ID auto-apply, not customer-facing prompt/voice/welcome changes.
- **Reply (≤120 words):** “We can show the measured lift by workflow, but we will not blend pickup and engagement or promote on an early noisy lead. Kestrel’s policy allows only specific auto-applied changes within a shift limit; Northwind KYC requires approval, and Northwind renewal does not allow auto-apply for customer-facing changes. We’ll use the permitted path and share the evidence and approval status. We will not deploy the persuasive prompt across clients based on another account’s demo.”

### 02 — Northwind Compliance
- **Accepted:** Human approval for customer-facing changes, validation evidence for prompt changes, auditability, conservative behavior, and approver-hour timing.
- **Pushed back on:** No request for broader automation; I would not interpret auditability as a guarantee of named-user identity beyond what the simulator returns.
- **Wrong against policy:** No material policy mismatch found in the memo. The contract still distinguishes allowed operational auto-apply from customer-facing changes; the controller uses approval for welcome/voice/prompt proposals.
- **Reply (≤120 words):** “We route customer-facing changes through your approval process and attach prompt-validation evidence. Each applied version is linked to its proposal, approval ID, changed dimensions, rationale, and observed outcomes in the controller’s audit store. We schedule requests inside the configured approver hours. Operational changes are auto-applied only when the workflow policy explicitly permits the dimension and shift; we will summarize those changes and their rationale.”

### 03 — Platform & SRE
- **Accepted:** Persistent restart state, idempotent ingestion/application, actionable incident records, and avoiding new stateful infrastructure for this assignment.
- **Pushed back on:** SQLite is suitable only for this single-process prototype; I am not claiming it is a production multi-replica store. The requested public_03 performance objective has not been measured, so it is not claimed as met.
- **Wrong against policy:** No memo claim is disproved by the current evidence. The log demonstrates one recovery case, not survival from every possible kill point.
- **Reply (≤120 words):** “The controller stores chunk cursors, event IDs, proposals, approvals, experiment state, and decisions in SQLite/WAL. The included test log demonstrates a forced stop and restart, but it does not prove every failure boundary. I have not measured the public_03 target on the specified Linux host; I will not claim the ten-minute objective until that benchmark and memory profile are recorded. Production multi-instance operation needs a shared transactional store and explicit ownership/locking.”

### 04 — Kestrel Operations Supervisor
- **Accepted:** Respect supervisor edits, keep request rate/time bounded, record changes, and reject strict/verbatim prompt candidates.
- **Pushed back on:** A consolidated screen is not implemented; separate alerts are not an acceptable substitute in production.
- **Wrong against policy:** The public_02 Kestrel policy allows up to four requests per 24 hours, while the controller voluntarily clamps itself to two and waits until at least 11:00. The stricter local limit is intentional.
- **Reply (≤120 words):** “We detect your config edits and refresh our baseline rather than silently overwriting them; a relevant edit resets the experiment. We voluntarily cap requests at two per rolling 24 hours and do not send before 11:00, even where the workflow policy allows more. We exclude strict/verbatim prompt candidates. A single supervisor screen is not yet built, so I would not call this production-ready until that surface is available.”

### 05 — Product
- **Accepted:** Separate pickup/engagement reporting, shared persisted cohort IDs, evidence-bearing proposals, and not learning from unapproved experiments.
- **Pushed back on:** Per-person timing and a consolidated view were cut; not every wishlist can be a P0 for this delivery.
- **Wrong against policy:** The wishlist labels are product priorities, not simulator-enforced workflow policy. Also, the assignment’s objective requires each workflow’s configured primary KPI and guardrail; the current code does not correctly honor that for pickup-primary workflows.
- **Reply (≤120 words):** “The audit and experiment records use stored cohort IDs, and proposals carry a diff, affected-lead estimate, dry-run summary, and rationale. Per-person timing and the consolidated view were cut. I found a more urgent correctness gap: analysis currently treats engagement as primary and does not apply the engagement guardrail for pickup-primary workflows. I would fix and test that before presenting the experiment loop as complete.”

### 06 — CTO
- **Accepted:** Prefer explainable regulated outcomes, spend credits only when needed, choose scope deliberately, and document operational gaps.
- **Pushed back on:** I do not claim the scale envelope or every workflow policy is satisfied from the public_01/02 results alone.
- **Wrong against policy:** No specific false policy claim in this memo; its instruction to trust the contract is consistent with the assignment.
- **Reply (≤120 words):** “I chose a durable, auditable controller with experiments, governance, and incident paths rather than implementing every requested surface. The public_02 outcome is positive overall but includes one harmful KYC change, and the code has a primary/guardrail-selection defect for pickup-primary workflows. Public_03 performance is unmeasured. Those are release blockers, not footnotes; I would fix the KPI logic and benchmark the load case before any production rollout.”

## 4. Architecture

```text
Simulator HTTP API
  /events/chunks (history, then live)      /workflows, /sim/state
                 |                                |
                 +---------- controller.main -----+
                                  |
                      controller.ingest
              chunk cursor + event-ID dedupe
                                  |
                         SQLite / WAL
       leads, calls, aggregates, cohorts, metrics,
       proposals, approvals, experiments, decisions
                                  |
                         controller.engine
       bootstrap -> experiment -> checkpoint/promotion
       caller-ID / hallucination incident -> decay check
                                  |
                       controller.governance
              /approvals -> poll/reconcile -> /apply
              policy-permitted auto-apply -> config PUT
                                  |
                       simulator config changes
                                  |
                      ingest/audit on next tick
```

`run.sh <SIM_URL>` starts the Python module. `controller.main` binds the SQLite state to a simulator run, ingests history before advancing, then advances the simulator, ingests new chunks, and runs an engine tick. SQLite is the durable state store. Chunk writes and cursor advancement are committed together; primary event IDs are deduplicated. On restart, the controller re-reads current configs, reconciles pending proposals against the simulator’s approvals, and continues from stored cursors and experiment state.

## 5. The learning loop
- **Cohorts:** Historical lead features are joined from SQLite. The base partition uses age bands present in at least 3% of the workflow population. Evidence-backed intersections use a 2% minimum share and a cap of 20 cohorts; feature values come from the API data, not scenario truth. Cohorts are persisted with IDs and referenced by config/analysis. This reduces fragmentation but does not establish that every decision/view uses the same cohort version over time.
- **Experiment design:** Bootstrap proposes randomized welcome-message and voice allocations while preserving caller-ID mix and schedule. The simulator randomizes the dimensions independently; analysis estimates global effects and selected age/feature segments separately. There is no explicit interaction test for welcome × voice, so interaction effects can be missed. The exploration window begins at 36 hours, extends in 12-hour increments, and is capped at 72 hours.
- **Deciding a winner:** Global candidates require at least 250 connected calls per arm (and 100 for the incumbent); segment candidates require at least 220 connected calls per arm. The code uses two-proportion tests, 95% risk-difference intervals, a minimum 3% relative lift, and Benjamini-Hochberg at q=0.10 across tested candidates. For engagement-primary workflows, the lower confidence bound on pickup change must be at least -1.5 percentage points. The code currently hard-codes engagement as the primary metric; thus these thresholds do **not** provide the required guarantee for pickup-primary workflows. No empirical false-winner rate was measured.
- **Decay and rollback:** After promotion, the code compares the latest two days to the preceding three-day window. With at least 200 connected calls in each, it flags a relative decline of at least 5% when the 95% interval is below zero and p<0.05, then proposes a rollback through approval. Public_02 shows W3 in-population share falling to zero by day 5, but that report alone does not prove the rollback caused it or that a tested rollback restored outcomes.
- **Call time and caller ID:** Historical call-hour rates inform bounded hourly weights globally and for sufficiently sampled age bands. There is no per-person or next-best-attempt policy. Caller-ID health uses a six-hour window, at least 300 dials on the candidate and 600 peer dials, and requires at least a 25% relative and 5-point absolute pickup deficit with p<0.001. The controller proposes a smoothed redistribution away from unhealthy IDs, using auto-apply only if workflow policy permits.
- **Guardrail KPI:** For engagement-primary comparisons the code checks a pickup non-inferiority bound. However, the current primary/guardrail implementation is incomplete: pickup-primary workflows are still evaluated on engagement and their engagement guardrail is not checked. This violated the required design intent and must be fixed before submission as a finished solution.

## 6. Governance
- **Lifecycle:** A proposal is persisted as `prepared` before network submission, then tracked through pending/approved/applying/applied or failure states. Restart reconciliation matches stored proposal IDs against `/approvals`. Duplicate POSTs are looked up before retrying; stale base versions are marked stale; approval expiry/rejection, lapsed apply windows, and network failures are recorded. The code does not yet demonstrate a complete fresh-data re-proposal path for every terminal status.
- **Timing and fatigue:** Requests are sent no earlier than 11:00 and before the workflow’s configured close (with a one-hour buffer). The local cap is `min(2, workflow max_requests_per_24h)` per rolling 24 hours. TTLs are bounded by workflow policy and proposal type. This is request limiting, not general multi-change batching; a promotion can bundle multiple related dimensions in one config proposal.
- **Auto-apply:** The code uses auto-apply for caller-ID rotation only when enabled dimensions and maximum shift satisfy policy. Experiment, promotion, rollback, and prompt changes go through approval even if an auto path might exist. For example, public_02 Kestrel permits selected dimensions within a 0.2 shift, but the controller still routes experiments through approval. Northwind KYC auto-apply is disabled.
- **Proposal metadata:** `impact_score` is clipped relative lift × population share divided by 10%; some paths instead pass fixed values (for example 0.3 for caller-ID rotation, 0.6 for a prompt fix, 0.2 for rollback), so scores are not consistently calibrated. Affected leads are estimated from unique non-engaged leads in the recent three-day period plus the estimated matching share of the next batch. Dry runs use smoothed observed arm rates over a historical window, not a full counterfactual simulation; these are estimates, not guarantees.
- **Supervisor edits:** Config-change events identify supervisor edits. The controller refreshes current config and interrupts/restarts relevant experiment measurement rather than writing over the edit. Public_02 records a Kestrel supervisor edit at D3 11:30 and a reported 330-minute MTTR; Northwind renewal was also edited at D5 15:00.

## 7. Incidents
Caller-ID detection checks populations hourly, not individual calls, and requires the sample and effect-size thresholds described above. Hallucination detection checks a six-hour lookback after a six-hour warmup; it requires at least 300 connected calls, five hallucinations, at least 1,000 connected baseline calls, a rate increase of at least 3 percentage points, and p<0.001. It reserves at least 20% of initial credits (and at least 10 credits), limits attempts to three per workflow/day, asks the LLM only for candidates, filters invalid/strict candidates, runs validation at n=400, requires pass rate ≥0.985, and submits the change for approval with validation evidence. The LLM does not select experiment winners.

Public_02 evidence:
1. **D2 10:00:** the grader reports the Kestrel DID incident; the flagged-ID share was 0.219 on day 2 and 0.0 on subsequent reported days. The report shows a caller-ID-only v3 change and a beneficial result.
2. **D3 11:30:** Kestrel supervisor edit; report gives 330 minutes to the controller-approved fix.
3. **D5 15:00:** Northwind renewal supervisor edit is recorded.
4. The run used 11 credits, two validation runs, and one LLM fix call. The grader reports hallucinated calls of 178 versus 416 in the baseline. Its incident list does not give a hallucination-detection/fix timestamp, so a precise prompt-incident timeline and causal verification cannot be claimed from this report.

The configured significance values describe statistical screening thresholds, not a demonstrated false-positive rate. Threshold calibration and post-apply verification need additional tests.

## 8. Scale
**Not measured on `public_03_load`.** No honest values are available here for events/second, decision lag, peak memory, or wall-clock time per simulated day. The controller’s metrics table currently receives placeholder zeroes for several measurements, so it cannot be used as evidence. The required 4-vCPU/8-GB Linux, one-simulated-day-under-10-minutes target is unverified.

At 10×, the likely pressure points are raw calls retained in SQLite, indexed queries/joins over calls and leads, and per-tick scans across workflows. This is an engineering hypothesis, not a measured bottleneck. A benchmark should first profile the current single-process implementation; likely partitioning is by tenant/workflow for decision ownership and sharded event/call storage, with one durable shared governance/audit boundary and fair tenant quotas.

## 9. Reliability
`reports/crash_test_01.log` records a test controller stopping before ingest at simulated minute 5040, restarting at minute 5040, and finishing the run. The controller stores per-kind chunk cursors and `last_event_id`; `calls.event_id` is the primary key and writes use `INSERT OR IGNORE`. Ingest rows and cursor updates share a transaction. The governance client records proposal intent before a request and reconciles simulator approvals on restart. The log is evidence for one recovery path, not every kill point.

HTTP behavior is mixed but explicit in the governance path: duplicate proposal responses are reconciled by proposal ID; stale conflicts are marked stale; a 410 apply is marked lapsed; other non-success statuses are recorded and marked failed/rejected. Network exceptions create warning alerts and return without pretending success. There is no general exponential retry/backoff policy for all 4xx/5xx responses, and the implementation needs failure-injection tests for ambiguous timeouts after remote success.

## 10. Audit & observability
Example query against the controller’s SQLite store:

```sql
SELECT c.ts, c.workflow_id, c.version, c.actor, c.changed_dims,
       c.proposal_id, c.approval_id, a.status, a.reasons,
       p.kind, p.payload
FROM config_changes c
LEFT JOIN approvals a ON a.approval_id = c.approval_id
LEFT JOIN proposals p ON p.proposal_id = c.proposal_id
WHERE c.workflow_id = 'nw_kyc'
ORDER BY c.minute;
```

On the recorded `public_01` state, `nw_kyc` changed to v2 at `2027-01-04T14:00:00+05:30`, actor `controller_approved`, dimensions `cohorts`, `welcome_message`, and `voice`, proposal `nw_kyc:experiment:1:a563f07c6f68a1ec:ba7693d5`, approval `apr_00002`. The corresponding audit decision says v2 was applied; the grader reports engagement lift of +0.074 for that change. The simulator exposes an approval ID/status, not a human approver’s name, so the store cannot truthfully identify an individual beyond the returned approval metadata.

Incident paths record critical hallucination alerts, warning-level caller-ID degradation/variant decay, and informational supervisor edits. These records are not wired to paging. A production policy should page on sustained critical hallucination or unreconciled apply/network ambiguity; it should not page on ordinary experiment checkpoints or informational supervisor edits. Actual lag, event rate, memory, credit burn, and decisions/hour need real instrumentation before alert thresholds can be set.

## 11. Results

### `reports/public_01.md`
| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ks_cancel | 0.346 | +1.2% | 0.243 | -0.8% | 0.501 | +1.9% | -2% | 62 (57) |
| nw_kyc | 0.382 | +4.1% | 0.240 | -0.7% | 0.624 | +4.5% | 32% | 22 (21) |
| nw_renewal | 0.422 | +1.3% | 0.262 | -0.8% | 0.554 | +2.3% | 14% | 51 (54) |
| ALL | 0.379 | +1.8% | 0.249 | -0.8% | 0.543 | +2.6% | 12% | 135 (132) |

Three changes were applied: two beneficial and one neutral. No incidents were reported. Credits used: 0.

### `reports/public_02.md`
| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ks_cancel | 0.287 | +15.8% | 0.199 | +8.4% | 0.506 | +6.2% | 45% | 75 (330) |
| nw_kyc | 0.369 | -1.4% | 0.246 | +1.5% | 0.582 | -1.7% | -23% | 40 (31) |
| nw_renewal | 0.442 | +3.7% | 0.267 | +2.0% | 0.554 | +1.1% | 20% | 63 (55) |
| ALL | 0.356 | +6.5% | 0.232 | +4.4% | 0.541 | +2.0% | 28% | 178 (416) |

Seven changes were applied: six beneficial and **one harmful**. The harmful change was Northwind KYC v2, with engagement delta -0.020; this is a serious failure, not hidden by the aggregate result. Kestrel caller-ID v3 was beneficial, and the grader reports substantial reduction in hallucinated calls overall. Credits used: 11; validation runs: 2; LLM fix calls: 1.

The public_01 aggregate improves versus its no-op baseline, and public_02 has positive aggregate deltas. Those results do not prove policy compliance or safe per-workflow decisions. In particular, Northwind KYC is pickup-primary with an engagement guardrail, while the engine currently tests engagement as the primary metric. I would fix KPI selection and guardrail enforcement, then rerun both scenarios and require no harmful promotion before calling the assignment complete.

## 12. Trade-offs & rejected alternatives
| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Let the LLM choose winners | Could speed up experimentation and simplify decision logic. | Non-deterministic and violates the contract; deterministic count-based tests choose winners. |
| Promote on early apparent lift | Faster business response. | Risks false winners and harmful changes; use minimum samples, intervals, and multiple-comparison correction. |
| Per-person next-call optimization | Product wishlist and potentially higher relevance. | More complex, privacy-sensitive, and not required for the selected loop; only bounded historical hour weights were implemented. |
| Full supervisor UI | Would satisfy the “one screen” request. | Cut for scope; audit tables and alerts exist, but this is a material missing product surface. |
| PostgreSQL/Redis | Familiar production infrastructure. | Adds operational components for a single-process assignment; SQLite/WAL was chosen, with multi-replica limitations stated. |
| Auto-apply every permitted change | Reduces approval latency. | The controller uses approval for experiments and prompt changes; auto-apply is limited to policy-permitted caller-ID rotation. |
| Tune to public scenario truth | Could improve the visible public score. | Disallowed and brittle for hidden scenarios; decisions use API-observable evidence only. |

## 13. Known weaknesses
1. **Release blocker:** primary KPI and guardrail are not selected per workflow. Current analysis assumes engagement primary; it can promote a change that hurts a pickup-primary workflow or violates its engagement guardrail.
2. No public_03 load benchmark or trustworthy operational metrics are included.
3. One harmful public_02 change was applied; the aggregate score does not excuse it.
4. Welcome/voice interaction effects are not tested.
5. No consolidated supervisor view, per-person call-time optimizer, or fair tenant credit scheduler.
6. Impact scores include hard-coded heuristics; affected-lead counts and dry runs are estimates.
7. Decay rollback and hallucination post-apply verification lack sufficiently explicit end-to-end evidence.
8. Crash evidence covers one forced-restart point; ambiguous remote-success/network-timeout and concurrent-controller cases need testing.
9. The name and actual hours-spent field above must be verified/replaced before submission.

## 14. If this went to production at Formi next month
1. **Days 1–2:** Block customer-impacting autonomy until primary KPI and guardrail handling are fixed per workflow; add regression tests for both engagement-primary and pickup-primary cases.
2. **Days 3–4:** Re-run public_01/public_02, investigate the harmful KYC decision, and require per-workflow guardrail checks before approval submission.
3. **Days 5–6:** Run public_03 on the specified Linux host; record events/sec, end-to-end decision lag, peak RSS, simulated-day wall time, and 10× capacity analysis.
4. **Days 7–8:** Add governance fault injection for duplicate submissions, stale versions, expiry, lapsed apply windows, timeouts after remote success, and supervisor edits.
5. **Days 9–10:** Replace placeholder telemetry with measured metrics; define actionable alert ownership and test alerts.
6. **Days 11–12:** Build a role-authorized consolidated supervisor view showing experiments, cohorts, pending proposals, changes, rationale, approvals, and outcomes.
7. **Days 13–14:** Review data retention/access controls, SQLite single-writer limits, tenant fairness, and rollback playbooks with SRE and client compliance before any staged rollout.
