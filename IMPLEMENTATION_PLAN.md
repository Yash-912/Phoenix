# Phoenix — Implementation Plan

This plan turns the PRD (`phoenix_incident_response_prd.md`) into an ordered sequence of buildable slices. Each phase matches PRD Section 12's effort estimate; each phase is broken into slices small enough to build, run, and understand in one sitting. For every slice: **what we build**, **why it's needed** (tied to the PRD requirement it satisfies), the **key technical concepts** it teaches, and the **definition of done** (how we'll know it actually works before moving on).

Build order matters here: every phase depends on infrastructure the previous phase stood up. We do not skip ahead — e.g., there is no Diagnoser (Phase 2) until the Observer (Phase 2) has real evidence to hand it, and there is no Observer until Phase 1's observability stack exists for it to query.

---

## Repository structure (target, grows incrementally)

```
Agentic/
├── docker-compose.yml
├── services/
│   └── demo-checkout/          # target microservice Phoenix will monitor/remediate
├── observability/
│   ├── prometheus/              # prometheus.yml + alerting rules
│   ├── alertmanager/            # alertmanager.yml (webhook route)
│   ├── loki/                    # loki config
│   └── promtail/                # promtail config (log shipping)
├── phoenix/                      # the agent itself
│   ├── api/                      # FastAPI app: webhook receiver, dashboard REST API
│   ├── graph/                    # LangGraph definition: nodes, edges, state schema
│   ├── tools/                    # Observer/Executor tool implementations (Prometheus/Loki/Docker clients)
│   ├── risk/                     # deterministic Risk Engine (allowlist tables)
│   ├── db/                       # Postgres models, migrations, audit log writer
│   └── slack/                    # Slack Bolt app (approval workflow)
├── chaos/                        # chaos injection scripts, one per scenario
├── eval/                         # evaluation harness, metrics computation
└── dashboard/                    # Next.js + Tailwind frontend (3 pages)
```

---

## Phase 1 — Docker Environment + Observability Stack + Alert Ingestion

**Goal:** stand up a realistic microservice + full observability stack, ending with a real alert reaching an HTTP endpoint Phoenix controls. Nothing agentic yet — this phase only builds Phoenix's "senses" and the front door alerts walk through.

| Slice | What we build | Why it's needed | Key concepts | Done when |
|---|---|---|---|---|
| 1.1 | `services/demo-checkout` — a minimal FastAPI microservice instrumented with `prometheus-fastapi-instrumentator`; Dockerfile to containerize it | Phoenix needs something real to observe and (later) break. App-level HTTP metrics (latency, error rate — FR-1) come from here | Dockerfile layering, FastAPI routing, Prometheus client instrumentation, containers as isolated processes | `docker compose up`, `GET /metrics` returns real Prometheus-format text |
| 1.2 | Root `docker-compose.yml` wiring demo-checkout + **cAdvisor** + **Prometheus**; `observability/prometheus/prometheus.yml` scrape config | Container-level metrics (CPU/mem/disk — FR-1) come from cAdvisor reading cgroups, not the app. Prometheus needs to know what to scrape | Docker Compose networking/service discovery, cgroups→cAdvisor pipeline, PromQL scrape_configs, pull-based metrics | Prometheus UI (`:9090/targets`) shows all targets `UP`; can run a live PromQL query |
| 1.3 | **Loki** + **Promtail** added to Compose; `observability/promtail/promtail.yml` | Logs are the other evidence source (FR-1, FR-3) — Promtail tails container stdout/stderr and ships to Loki, labeled by container | Event-driven vs. sample-driven telemetry, Loki's label-indexed/text-scanned storage model, LogQL | Can query demo-checkout's logs in Loki via LogQL and see real request logs |
| 1.4 | Prometheus **alerting rules** (`observability/prometheus/alert.rules.yml`) + **Alertmanager** service + `alertmanager.yml` webhook route | An alert needs to *exist* (a PromQL condition sustained over time) before it can be routed anywhere (FR-1 → FR-2) | Alerting rule syntax (`expr`, `for`), Alertmanager grouping/dedup/routing, why Alertmanager sits between Prometheus and the receiver | Manually trigger load against demo-checkout, watch an alert go from `pending`→`firing` in Alertmanager's UI |
| 1.5 | `phoenix/api/` FastAPI app with a `POST /webhooks/alertmanager` endpoint (Pydantic-validated payload) + **Postgres** container + minimal `incidents` table; find-or-create dedup logic (FR-2) | This is the literal front door — where a Prometheus alert becomes a Phoenix "Incident." Needs durable storage (Postgres) so incident state survives across requests | Webhooks (push vs. pull), Pydantic request validation as a trust boundary, DB unique-constraint + `ON CONFLICT` for race-safe dedup (not naive check-then-act) | Firing the same alert twice within the dedup window creates exactly one incident row in Postgres, visible via a `GET /incidents` debug endpoint |

**Phase 1 exit criteria (mirrors PRD MVP DoD items 1–2):** a real injected load spike on demo-checkout produces a real Prometheus alert, which reaches Phoenix's webhook, deduplicates correctly, and creates exactly one incident row in Postgres — no LLM involved yet, purely the plumbing.

---

## Phase 2 — Observer + Diagnoser + Audit Logging

**Goal:** the first agentic piece. Given an incident, gather real evidence and produce a ranked, evidence-scored hypothesis — with every step written immutably to Postgres.

| Slice | What we build | Why it's needed | Key concepts | Done when |
|---|---|---|---|---|
| 2.1 | Full Postgres schema: `incidents`, `evidence`, `hypotheses`, `audit_log` (append-only, `INSERT`-only DB role) | FR-10 requires a complete, immutable audit trail; relational structure is what lets later evaluation queries (Phase 5) aggregate across incidents | ACID transactions, append-only enforcement via DB permissions (not app-level promises), schema design for auditability | Attempting an `UPDATE`/`DELETE` against `audit_log` as the app's DB role fails with a permission error |
| 2.2 | `phoenix/graph/` — LangGraph `StateGraph` skeleton: state schema (evidence list, hypotheses, iteration count, cost spent), Observer and Diagnoser as nodes, a conditional edge between them | This *is* the "stateful graph, not a fixed pipeline" architecture from PRD Section 6 — the Diagnoser↔Observer loop (FR-4) requires cycles a linear pipeline can't express | LangGraph nodes/edges/conditional routing, why cycles need a graph model, shared state object as the live precursor to the audit log | Graph compiles and runs a no-op traversal Observer→Diagnoser→end without errors |
| 2.3 | Observer node: real tool implementations in `phoenix/tools/` — `query_prometheus`, `query_loki`, `get_container_state` (read-only Docker SDK client, ideally behind a socket-proxy) | FR-3: Observer is read-only, evidence is timestamped/source-tagged. This is where "the LLM only ever requests tool calls; your code executes them" becomes real code | Tool/function calling mechanics, least-privilege tool allowlisting (Observer's LLM is *only* ever offered these three tools), Docker socket proxy for infra-enforced read-only access | Given a real incident, Observer produces evidence rows in Postgres pulled from live Prometheus/Loki/Docker data, each tagged with source + timestamp |
| 2.4 | Diagnoser node: structured-output hypothesis generation (Pydantic schema forced via tool-calling), **deterministic** evidence-weighted scoring rubric (your code tallies independent evidence categories, not an LLM confidence number), iteration/cost cap tracked in graph state, escalation path when cap is hit | FR-4 exactly — ranked, evidence-grounded hypotheses; hard cap to guarantee termination (the "liveness guarantee" problem) | Structured/schema-constrained LLM output, why scoring must be code-computed not LLM-asserted, global iteration/cost budgets vs. per-node caps, systematic-bias risk in LLM-judged evidence categories | Running a real chaos scenario (borrowed early from Phase 5) produces a ranked hypothesis list with a numeric score your code computed, and a full audit trail reconstructing evidence→hypothesis reasoning from Postgres alone |

**Phase 2 exit criteria (MVP DoD items 3–4):** collect evidence from ≥2 independent sources, produce a ranked hypothesis respecting the iteration/cost cap, with a complete audit trail.

---

## Phase 3 — Planner + Risk Engine + Executor + Slack Approval

**Goal:** go from "we know what's probably wrong" to "we do something about it, safely."

| Slice | What we build | Why it's needed | Key concepts | Done when |
|---|---|---|---|---|
| 3.1 | Planner node: structured remediation proposal (`action_type`, `target`, `expected_impact`) — no risk self-assessment requested from the LLM | FR-5: candidates need description + tool(s) + expected impact. Deliberately *not* asking the LLM to rate its own risk (Phase 6/8 lesson: keep judgment calls out of the LLM where the failure mode is unsafe) | Separating "what to do" (LLM's job) from "how risky" (never the LLM's job) | Planner outputs a schema-valid remediation proposal referencing an actual allowlisted action type |
| 3.2 | `phoenix/risk/` — a static, version-controlled action→risk-tier table (fail-closed: unrecognized action types default to Critical/rejected) | FR-6's table, made real: deterministic code, auditable by reading one file, no model involved in the tier decision | Least privilege, fail-closed defaults, why this must be pure code (testable with exact assertions) | Unit test: every action in the allowlist maps to exactly one tier; unknown actions always reject |
| 3.3 | Executor node: Docker SDK calls restricted to the allowlisted actions only (restart, scale, clear cache); exact command/API call + raw result logged to `audit_log` in the same transaction as execution | FR-7 + FR-9's atomicity concern from Phase 4 of the course — action and its audit record must not be separable by a crash | Docker SDK restart mechanics (new process, not resumed), transactional coupling of action + audit write, "no arbitrary shell execution" | Low-risk action auto-executes end-to-end against demo-checkout; audit_log has a matching immutable row even if you kill the process immediately after |
| 3.4 | `phoenix/slack/` — Slack Bolt app: posts Block Kit approval messages for High-risk actions, verifies Slack request signatures, checks responder against an authorized-approver allowlist (authentication ≠ authorization) | FR-6 + Section 8's "authenticated, role-restricted responder" | Slack Bolt interactivity, HMAC request signing verification, authn vs authz as distinct checks, Medium-risk bounded-timeout auto-proceed | A High-risk proposal posts to Slack; only an allowlisted user's Approve click proceeds; a non-allowlisted click is rejected even though it's a validly-signed Slack event |

**Phase 3 exit criteria (MVP DoD items 5–6):** propose a remediation with a risk tag; auto-execute if low-risk, or send a real Slack approval request if high-risk, enforced entirely by deterministic code.

---

## Phase 4 — Recovery Verification + Rollback

**Goal:** never trust that a remediation worked just because it ran — check, and undo if it made things worse.

| Slice | What we build | Why it's needed | Key concepts | Done when |
|---|---|---|---|---|
| 4.1 | Verification node: re-polls Prometheus/Loki after a bounded timeout window; conditional edge back to Diagnoser (with new evidence) if unresolved | FR-8 — "can't tell slow-recovering from still-broken," so a bounded timeout is the only way to make a decision that terminates | Bounded timeout as a decision point (same pattern as Phase 0's "can't tell slow from dead"), feeding fresh evidence back into an existing cycle | A deliberately insufficient fix (e.g., restarting the wrong container) correctly routes back to the Diagnoser with updated evidence rather than declaring false success |
| 4.2 | Rollback logic: on verification showing *worse* health, revert by restarting from last-known-good image/config; escalate to human afterward | FR-9 — leans on containers' inherent disposability (Phase 1 of the course) rather than an in-place undo mechanism | Why "rollback" = redeploy from known-good image, not state-restoration; escalation as the required follow-up to any rollback | Intentionally-bad remediation in testing triggers an automatic rollback and a human-escalation record in the audit trail |

**Phase 4 exit criteria (MVP DoD item 7, Success Criteria's rollback demonstration):** verify recovery after any action; roll back automatically if verification fails, with at least one demonstrated bad-remediation-then-rollback test case.

---

## Phase 5 — Chaos Engineering Scenarios + Evaluation Harness

**Goal:** objective proof the whole loop works, against six failure types with known ground truth — this is what makes the accuracy/safety claims in PRD Section 14 real rather than assumed.

| Slice | What we build | Why it's needed | Key concepts | Done when |
|---|---|---|---|---|
| 5.1 | `chaos/` — six scripted injection scenarios (container crash, memory leak, CPU spike, disk full, DB connection timeout, bad deployment), each hitting a dedicated endpoint/mechanism added to demo-checkout (and a couple of satellite services for the DB-dependency and deploy scenarios) | Section 10 — you can only measure root-cause accuracy if *you* already know the true cause, because you caused it deterministically | Chaos engineering methodology, why each scenario is chosen to stress a *different* evidence signal (trend vs. snapshot vs. log signature vs. timing) | Each scenario reliably reproduces its intended failure signature and reliably clears when the correct remediation is applied |
| 5.2 | `eval/` — harness that runs all six scenarios N times each, captures Phoenix's actual hypothesis/action/outcome per run, computes the four metric categories (diagnostic, operational, safety, efficiency) from audit log data | Section 10 + 14's concrete success criteria (>80% root-cause accuracy, 0% unsafe-action rate) need to be *measured*, not asserted | Testing non-deterministic (LLM) components statistically vs. deterministic components with exact assertions, systematic-bias detection via ground-truth comparison | Harness produces a results table across all six scenarios with all four metric categories populated from real runs |

**Phase 5 exit criteria (MVP DoD items 4, 10; Success Criteria):** report accuracy/timing/safety/efficiency metrics across the full six-scenario chaos set.

---

## Phase 6 — Dashboard (3 Pages) + Postmortem Generation

**Goal:** make everything Phoenix has been doing legible to a human, without needing to query Postgres by hand.

| Slice | What we build | Why it's needed | Key concepts | Done when |
|---|---|---|---|---|
| 6.1 | Postmortem generator: LLM-drafted summary/timeline/root-cause/outcome, built strictly from audit-log rows already written (never new unverified claims) | FR-11 — the postmortem must be traceable back to real recorded evidence, not the LLM inventing a plausible-sounding narrative | Grounding generated text in retrieved structured data rather than open-ended generation, keeping "reasoning text" honest per Phase 8's integrity concerns | Auto-generated postmortem for a real chaos run matches the actual audit trail (spot-checkable claim by claim) |
| 6.2 | `dashboard/` — Next.js + Tailwind, 3 pages: Incident Dashboard (`GET /incidents` list), Incident Detail/Reasoning Trace (chronological render of one incident's audit rows), Evaluation Dashboard (Phase 5's aggregate metrics) | Section 11 — scoped to exactly 3 pages, deliberately not the full 10-page future-roadmap dashboard | Next.js file-based routing, React component/data flow, Tailwind utility classes, REST endpoints as the frontend/backend contract (Phase 3 of the course) | All 3 pages render real data end-to-end against a live Phoenix backend; Reasoning Trace page reconstructs a full incident timeline from audit_log alone |

**Phase 6 exit criteria (MVP DoD items 8–9, Success Criteria's "full reasoning trace reconstructable from audit log alone"):** complete, demoable end-to-end system across all 3 dashboard pages.

---

## How we'll work through this

Same rhythm as the conceptual course: for each slice, I explain the concepts and design choices *before or alongside* writing the actual files, we run it and confirm it actually works (not just "looks right"), then pause for your questions before moving to the next slice. We do not jump ahead to a later phase's slice before the current one is confirmed working — each phase's exit criteria are a real checkpoint, not a formality.
