# Phoenix — Autonomous Reliability Engineer
### Product Requirements Document (V1)

## 1. Summary

Phoenix is an agentic AI reliability engineer for a distributed application. Given a reliability objective — not a fixed runbook — it investigates production incidents across telemetry, deployment history, and source code, tests competing root-cause hypotheses, and autonomously selects the **least invasive remediation that the evidence supports**: mitigate, roll back, or patch code. It verifies recovery against live SLO signals and records the incident for future reasoning.

**Core principle:** Phoenix is given an objective (*resolve the incident, restore the SLO*), not a procedure. The agent decides what to investigate next, what hypothesis to test, and which of three remediation tiers to apply.

## 2. Problem with a narrower design

An agent that only understands "service unhealthy → restart" cannot distinguish diagnosis from action — a restart looks identical whether the cause was correctly identified or not. Phoenix must be able to act at three different levels of permanence so that its remediation choice is evidence of what it actually understood about the root cause.

## 3. The Three Remediation Tiers

```
INCIDENT → Investigate → Hypothesize → Test hypothesis → Root cause
                                              │
                          ┌───────────────────┼───────────────────┐
                          ▼                   ▼                   ▼
                       TIER 1              TIER 2              TIER 3
                      Mitigate            Recover              Fix
                  restart / scale /    rollback deploy /    inspect code →
                  pause / cache      rollback config       patch → tests →
                                                              PR (human merge)
                          └───────────────────┼───────────────────┘
                                              ▼
                                       Verify vs SLO baseline
                                    ┌──────────┴──────────┐
                                 Healthy               Unhealthy
                                    │                       │
                                  Done              Re-investigate / escalate
```

| Tier | Action space | What it proves | Autonomy |
|---|---|---|---|
| 1 — Mitigate | restart_service, scale_service, pause/resume_worker, clear_approved_cache | Restored availability; does not require root cause | Autonomous |
| 2 — Recover | rollback_deployment, rollback_config | Correlated a specific change with the incident and reversed it | Autonomous, policy-gated |
| 3 — Fix | inspect code → generate patch → run existing tests → open PR | Understood the code-level root cause | **Stops at PR; human merges** |

Phoenix does not default to the strongest tier. It picks the least invasive action with sufficient evidence, and can escalate tiers if a lower one proves insufficient.

## 4. Goals

- Demonstrate genuine agentic behavior: the agent chooses its next tool call based on evidence, not a fixed pipeline.
- Distinguish **mitigation** (temporary) from **root-cause remediation** (permanent) in every incident record.
- Reason across telemetry, deployment history, and source code as one investigation, not separate tools.
- Test competing hypotheses via targeted evidence-gathering, not single-shot LLM judgment.
- Produce one polished, inspectable reasoning trace per incident.

## 5. Non-Goals (V1)

- Autonomous build, deploy, or promotion of a Tier 3 code patch (PR + human merge only).
- Autonomous PR merging.
- Kubernetes, Terraform, or any cloud-infra remediation.
- A persistent knowledge graph (a lightweight, dynamically-constructed dependency map is in scope).
- Large semantic/RAG memory (structured JSON incident records only).
- Multi-agent swarm architectures.
- Cascading multi-service failure and queue/worker overload scenarios — deferred to V2 (see §11).

## 6. System Architecture

```
                 ┌────────────────────┐
                 │  Phoenix Supervisor │
                 └──────────┬──────────┘
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
  System Observer   Change Investigator   Hypothesis Engine
  metrics/logs/      deployments/git      evidence, competing
  traces/health      diffs/config          hypotheses, next
                                            discriminating test
        └───────────────────┬───────────────────┘
                             ▼
                    Remediation Engine
                (choose tier: mitigate /
                 rollback / patch / no-op)
                             ▼
                    Verification Engine
              (compare live SLOs to baseline)
                             │
                     ┌───────┴───────┐
                  Healthy         Unhealthy
                     │                 │
              Incident Memory     Rollback / re-investigate
```

The Supervisor (LangGraph) repeatedly asks "what should happen next?" rather than executing a fixed sequence. Tools are grouped by capability; the LLM decides which to call.

### Tool inventory

| Category | Tools |
|---|---|
| Observability | query_metrics, query_logs, query_traces, inspect_health |
| Change management | get_recent_deployments, get_git_commits, get_git_diff |
| Code | search_repository, read_file, run_tests, run_linter |
| Runtime (Tier 1) | restart_service, scale_service, pause_worker, resume_worker, clear_approved_cache |
| Recovery (Tier 2) | rollback_deployment, rollback_config |
| Fix (Tier 3) | generate_patch, run_tests, open_pull_request |
| Validation | run_synthetic_check, compare_health_before_after |

## 7. Environment — "Phoenix Lab"

A self-contained, intentionally-buggy sandbox so the agent can be given real authority without touching real production.

- 5–7 microservices (API, Auth, Payment, Worker) + Postgres + Redis + a queue
- OpenTelemetry traces, Prometheus metrics, Loki logs
- A real GitHub repository with commit history and a real test suite
- GitHub Actions for test execution (not deployment)
- Docker Compose for orchestration — sufficient distributed-systems complexity without Kubernetes

## 8. V1 Scenarios (4, fully built)

| # | Scenario | Root cause | Tier used | What it demonstrates |
|---|---|---|---|---|
| 1 | Bad deployment | Buggy release correlates with error spike | **Tier 2** — rollback deployment | Correlating telemetry with deploy history, not just "unhealthy → restart" |
| 2 | Query regression | A recent commit introduces an inefficient query | **Tier 3** — code patch + PR | Root-cause-to-patch reasoning; trace → diff → fix |
| 3 | Memory leak | Cache lifecycle bug causes monotonic memory growth | **Tier 1 → Tier 3** — restart now, patch for permanence | Explicit mitigation-vs-permanent-fix distinction |
| 4 | Configuration regression | A deploy silently shrank a connection pool | **Tier 2** — config rollback | A causal chain that doesn't require code changes at all |
| 5 | Ambiguous / insufficient evidence | Deliberately underdetermined signal | **No-op / escalate** | Proves the agent can decide *not* to act rather than force a fix |

Scenario 5 is required, not optional: at least one demo must end in "insufficient evidence, escalating" to show Phoenix isn't scripted to always find a fix.

## 9. Safety Model

Two independent dimensions, not one risk score:

**Remediation capability** (what kind of change): Mitigation → Rollback → Config change → Code modification, increasing in blast radius.

**Execution policy** (who can trigger it):
- *Autonomous Lab mode* — full authority inside the sandbox, for demo purposes.
- *Guarded mode* — low-risk runtime actions auto-approved; rollback and config changes configurable for approval; **Tier 3 code changes always stop at PR, human merges.**

This lets the pitch be "an agent with an explicit policy boundary around what it may execute," not "an LLM with production access."

## 10. Verification Model

A remediation is only marked resolved after three layers pass:
1. **Code validation** — existing tests, lint (Tier 3 only)
2. **Deployment validation** — container health, startup success (Tier 2/3)
3. **Behavioral validation** — error rate, latency, throughput vs. pre-incident baseline (all tiers)

"Tests passed" or "container running" alone is never sufficient to close an incident.

## 11. Incident Record (structured memory)

Each resolved incident is stored as a compact JSON record (service, symptoms, root cause, tier used, fix reference, verification result). This is scoped small deliberately — no RAG, no knowledge graph — and its only V1 job is to let Phoenix note "this matches a prior incident" as supporting evidence in the dashboard trace, not to change its own retrieval strategy.

## 12. Metrics

| Metric | Why it matters |
|---|---|
| Root-cause accuracy | Did it understand the incident? |
| Tier appropriateness | Did it choose the least invasive sufficient action? |
| Autonomous resolution rate | How many incidents needed no human step beyond PR merge? |
| Mean time to recovery | Speed of the mitigate step |
| Permanent-fix rate | Of code-caused incidents, how many got a real patch, not just a restart? |
| Rollback success rate | Can it recover from its own bad action? |
| Correct escalation rate | Did it avoid acting on insufficient evidence? |
| Cost per resolution | LLM/tool spend per incident |

## 13. Guardrails

- Hard cap on investigation loop iterations per incident (prevents runaway "investigate again" cycles).
- Hard token/cost budget per incident, enforced by the Supervisor, not just monitored after the fact.
- Tier 3 changes are always scoped to a single file/function diff — no multi-file refactors in V1.

## 14. Dashboard (required, deliberately lightweight)

One page per incident showing the live reasoning trace (evidence gathered, hypotheses considered and discarded, tier chosen and why, verification result). The trace is the product; the dashboard's only job is to make it visible in under two minutes for a reviewer. Not a multi-page analytics product.

## 15. V2 / Deferred

- Cascading multi-service failure scenario (root-cause isolation across a dependency chain).
- Queue/worker overload with autonomous scaling policy.
- Autonomous build → staging deploy → synthetic traffic → promote loop for Tier 3 patches.
- Proactive "reliability autopilot" mode that scans for SLO drift without waiting for an alert.
- Larger incident-memory retrieval used to actively bias investigation, not just annotate it.

## 16. Definition of Done (V1)

Phoenix autonomously resolves scenarios 1–4 end-to-end (detect → investigate → hypothesize → remediate at the correct tier → verify → record) and correctly escalates on scenario 5, with every step visible in the dashboard trace, running inside Phoenix Lab under the guarded execution policy.
