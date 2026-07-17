# Product Requirements Document (PRD)

# Phoenix: Autonomous Incident Response Agent (V1)

**Version:** 2.0 (Scoped)
**Status:** Draft
**Author:** Yash Doke
**Date:** July 2026

---

# 1. Executive Summary

Phoenix is an autonomous incident response agent for Docker-based microservice environments. It behaves like a junior SRE: it watches for infrastructure alerts, investigates using real observability data, forms ranked root-cause hypotheses, proposes or executes remediations within explicit safety boundaries, verifies recovery, rolls back on failure, and produces an auditable postmortem.

This document scopes a solo-buildable V1: Docker Compose only, no Kubernetes, no cloud provider APIs, no knowledge graph. The full long-term vision is documented in Section 15 (Future Roadmap) but is explicitly out of scope for V1.

---

# 2. Problem Statement

Monitoring tools detect failures well but leave investigation and remediation entirely manual: an engineer correlates logs and metrics, forms a hypothesis, executes a fix, verifies recovery, and writes a postmortem. This is repetitive, slow, and inconsistent across engineers. Phoenix automates this loop while keeping humans in control of anything risky.

---

# 3. Goals (V1)

- Autonomously investigate a triggered alert using real telemetry (metrics, logs, container state)
- Produce ranked, evidence-grounded root-cause hypotheses (not unexplained confidence numbers)
- Propose remediations with explicit risk tags
- Execute low-risk remediations automatically; require human approval for high-risk ones; never auto-execute critical-risk ones
- Verify recovery after any action; roll back automatically if health worsens
- Maintain a complete, immutable audit trail of evidence, reasoning, decisions, and actions
- Evaluate the system objectively using chaos-injected failures with known ground truth

# 4. Non-Goals (V1)

- Kubernetes, cloud provider APIs (AWS/Azure), Terraform, Ansible
- Database schema migrations or data deletion
- Modifying application source code
- Knowledge graph / long-term semantic memory
- Multi-tenant or multi-cluster support
- Replacing human SRE judgment on ambiguous or critical incidents

---

# 5. Target Users

DevOps/SRE/platform engineers evaluating agentic reliability tooling; recruiters/reviewers assessing production-oriented agent design; AI practitioners interested in safe tool-use patterns.

---

# 6. High-Level Architecture

Target Docker Compose environment → Observability layer (Prometheus, Loki, Alertmanager) → Alert webhook → Incident Coordinator → Agent loop (Observer → Diagnoser → Planner → Risk Engine → Executor → Auditor) → Recovery verification → Postgres audit log → Dashboard (3 pages)

The agent loop runs as a stateful graph (LangGraph), not a fixed pipeline — the Diagnoser can request more evidence from the Observer before committing to a hypothesis.

---

# 7. Functional Requirements

## FR-1 Infrastructure Monitoring
Track container health, CPU, memory, disk, HTTP latency, error rate, DB health, queue depth, and recent deploy events.

## FR-2 Incident Detection & Deduplication
Alerts arrive via Alertmanager webhook and become Incidents. Alerts referencing the same service within a configurable time window (e.g., 5 minutes) are merged into a single active incident rather than spawning duplicate investigations, to handle flapping alerts.

## FR-3 Observer Agent
Read-only. Queries Prometheus, Loki, and the Docker API for evidence. Never takes remediation actions. Evidence is timestamped and tagged with its source for later audit.

## FR-4 Diagnoser Agent
Generates multiple hypotheses and ranks them using an evidence-weighted rubric: each candidate hypothesis is scored based on how many independent evidence signals support it (e.g., matching log pattern + matching metric anomaly + matching deploy timing), not a single LLM-asserted percentage. If no hypothesis clears a minimum evidence threshold, the Diagnoser requests additional evidence from the Observer, up to a hard cap (e.g., 5 iterations or a fixed token/cost budget per incident) to prevent runaway investigation loops. If the cap is hit without resolution, the incident is escalated to a human with partial findings.

## FR-5 Planner Agent
Produces remediation candidates, each with: description, required tool(s), risk tag, and expected impact.

## FR-6 Risk Classification

| Risk | Action |
|------|--------|
| Low | Execute automatically (e.g., restart container, clear cache) |
| Medium | Execute with optional approval window (auto-proceeds after timeout if no objection) |
| High | Human approval required via Slack before execution |
| Critical | Never auto-executed under any condition |

## FR-7 Executor Agent
Executes approved actions via the Docker SDK only in V1. Logs exact command/API call issued and raw result.

## FR-8 Recovery Verification
After any remediation, re-polls Prometheus/Loki to confirm the alert condition has cleared and metrics have stabilized. If not resolved within a timeout, returns to the Diagnoser with updated evidence.

## FR-9 Rollback
If remediation is followed by further degradation, automatically reverts the action where supported (e.g., restore previous container state) and escalates to human review.

## FR-10 Audit Trail
Every step (evidence pulled, hypothesis considered, decision made, action taken, verification result) is written to Postgres as an immutable, timestamped record, including model reasoning text.

## FR-11 Postmortem Generation
Auto-drafts a postmortem per incident: summary, timeline, root cause, evidence used, remediation taken, and outcome.

---

# 8. Safety & Security Requirements

- Observer has read-only credentials; cannot call any mutating API
- Executor restricted to an explicit action allowlist (restart, scale, clear cache — no arbitrary shell execution)
- All Slack approval requests require an authenticated, role-restricted responder
- Audit logs are append-only
- Evidence text (logs especially) is treated as untrusted input: log content is never used to construct or alter the agent's tool-calling permissions or system instructions, only as data to reason over, to mitigate prompt-injection-via-log-content risk
- Secrets (API keys, Slack tokens) managed via environment-based secret store, never hardcoded

---

# 9. Technology Stack — V1 Only

- Infra: Docker, Docker Compose
- Backend: Python, FastAPI
- Agent framework: LangGraph
- LLM: provider-agnostic via API
- Observability: Prometheus, Grafana, Loki, Alertmanager
- Database: PostgreSQL (audit log, incident records)
- Approval interface: Slack Bolt app
- Frontend: Next.js + Tailwind (3 pages only — see Section 11)

## Deferred to Future Roadmap (not built in V1)
Kubernetes, Terraform, AWS/Azure SDKs, Ansible, Neo4j knowledge graph, pgvector semantic memory, full 10-page dashboard.

---

# 10. Chaos Engineering & Evaluation

Chaos scenarios (V1 set): container crash, memory leak, CPU spike, disk full, DB connection timeout, bad deployment. Each has deterministic, scripted ground truth.

Metrics tracked per experiment:
- Diagnostic: root-cause accuracy, top-2 accuracy
- Operational: mean time to diagnose, mean time to recovery
- Safety: false-remediation rate, rollback success rate, unsafe-action rate (should be zero by design)
- Efficiency: tool calls per incident, token cost per incident, wall-clock runtime

---

# 11. Dashboard (Scoped to 3 Pages)

1. **Incident Dashboard** — active/past incidents, status, severity
2. **Incident Detail / Reasoning Trace** — evidence, hypotheses with scores, decisions, actions, verification, in chronological order
3. **Evaluation Dashboard** — chaos experiment results and metrics over time

(Live metrics/logs explorer, knowledge graph, and settings pages deferred to future roadmap.)

---

# 12. Effort Estimate (Solo Build)

| Phase | Scope | Estimate |
|-------|-------|----------|
| 1 | Docker environment + observability stack + alert ingestion | 1 week |
| 2 | Observer + Diagnoser + audit logging | 1.5 weeks |
| 3 | Planner + Risk Engine + Executor + Slack approval | 1.5 weeks |
| 4 | Recovery verification + rollback | 1 week |
| 5 | Chaos engineering scenarios + evaluation harness | 1 week |
| 6 | Dashboard (3 pages) + postmortem generation | 1 week |

Total: ~7 weeks part-time.

---

# 13. MVP Definition of Done

Phoenix V1 is considered functionally complete when it can, end-to-end and unattended for low-risk cases:

1. Detect an injected chaos failure via a real alert.
2. Deduplicate against any flapping repeat alerts.
3. Collect evidence from at least two independent sources (metrics + logs).
4. Produce a ranked hypothesis with an evidence-based score, respecting the iteration/cost cap.
5. Propose a remediation with a risk tag.
6. Auto-execute if low-risk, or send a Slack approval request if high-risk.
7. Verify recovery and roll back automatically if verification fails.
8. Write a complete, immutable audit trail for the incident.
9. Auto-generate a postmortem.
10. Report accuracy/timing/safety metrics across the full chaos scenario set (target: >80% root-cause accuracy, 0% unsafe-action rate).

---

# 14. Success Criteria

- >80% root-cause accuracy across the six V1 chaos scenarios
- 0% unsafe (unauthorized) autonomous actions
- Full reasoning trace reconstructable for every incident from the audit log alone
- Demonstrable rollback on at least one intentionally-bad remediation in testing

---

# 15. Future Roadmap (Post-V1, Not Scoped Here)

- Kubernetes support, GitOps integration
- Cloud provider APIs (AWS/Azure), Terraform-based remediation
- Knowledge graph (Neo4j) for infrastructure dependency reasoning
- Semantic memory (pgvector) over runbooks and past incidents
- Full 10-page dashboard including live metrics/logs explorer
- Multi-cluster, multi-tenant support
- Canary remediation strategies
