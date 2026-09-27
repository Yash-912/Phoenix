# Phoenix — Implementation Plan

Companion to the Phoenix PRD (V1). Sequenced so that something end-to-end works early, and each phase adds one capability the PRD depends on — rather than building all infrastructure before any agent logic exists.

## Guiding principle

Get **one full incident loop working on the easiest scenario first** (detect → investigate → mitigate → verify), before adding Tier 2, Tier 3, or additional scenarios. A working narrow loop de-risks the architecture; a wide but non-functional build does not.

## Phase 0 — Environment ("Phoenix Lab")

**Goal:** a running, observable, breakable system with nothing agentic yet.

- Docker Compose: API service, Auth service, Payment service, Worker, Postgres, Redis, a queue
- OpenTelemetry instrumentation across services → traces
- Prometheus for metrics, Loki for logs, Grafana optional for your own sanity-checking (not the product dashboard)
- A real GitHub repo for the application code, with a real test suite (needed later for Tier 3)
- A basic Alertmanager rule or scripted trigger that fires when error rate/latency crosses a threshold

**Exit criteria:** you can manually break the system (e.g., stop a container, deploy a bad image) and see it reflected in metrics/logs/traces within seconds.

## Phase 1 — Manual chaos injection for Scenario 1 and 2

**Goal:** deterministic, repeatable failure injection before the agent exists.

- Script to deploy `checkout-api` v18 (bad) over v17 (good) — Scenario 1
- Script to swap in a slow SQL query via a commit — Scenario 2
- Each injection script should be one command, idempotent, and resettable back to healthy

**Exit criteria:** running the injection script reliably reproduces the same telemetry signature every time. This determinism is what makes evaluation possible later.

## Phase 2 — Observer + Supervisor skeleton (no remediation yet)

**Goal:** the agent can investigate and explain, but not act.

- Stand up LangGraph with a single Supervisor node
- Implement Observability tools: `query_metrics`, `query_logs`, `query_traces`, `inspect_health`
- Implement Change tools: `get_recent_deployments`, `get_git_commits`, `get_git_diff`
- Agent loop: on manual trigger, investigate Scenario 1's injected failure and produce a text root-cause explanation — no action taken

**Exit criteria:** given Scenario 1, the agent correctly states "deployment v18 correlates with the error spike" without being told this in advance, using only tool calls it chose itself.

## Phase 3 — Tier 1 (Mitigate) + Verification Engine

**Goal:** the agent can act and confirm the action worked.

- Implement `restart_service`, `scale_service`, `pause_worker`, `resume_worker`, `clear_approved_cache`
- Implement Verification Engine: `compare_health_before_after`, `run_synthetic_check`
- Wire the full loop: investigate → decide Tier 1 is sufficient → act → verify → mark resolved or re-investigate
- Add the iteration/cost guardrail here, early, before it's needed for real (cap loop steps, cap token spend per incident)

**Exit criteria:** a simple resource-exhaustion scenario (can reuse Scenario 3's memory leak, mitigate-only) runs autonomously end-to-end with a passing verification.

## Phase 4 — Tier 2 (Recover): rollback

**Goal:** the agent correlates a change with an incident and reverses specifically that change.

- Implement `rollback_deployment` and `rollback_config`
- Extend the Hypothesis Engine: given multiple candidate causes, choose the discriminating evidence (e.g., "does the incident timestamp align with the deploy timestamp, within X seconds?") rather than asking the LLM to just guess
- Run Scenario 1 (bad deployment) fully autonomously: investigate → correlate → rollback → verify
- Run Scenario 4 (config regression) fully autonomously: investigate → correlate → config rollback → verify

**Exit criteria:** both scenarios resolve without human input, and the incident record correctly states which specific change was reverted and why.

## Phase 5 — Tier 3 (Fix): code patch + PR

**Goal:** the highest-value, highest-risk capability. Build last, once the loop around it is trustworthy.

- Implement `search_repository`, `read_file`, `generate_patch`, `run_tests`, `run_linter`, `open_pull_request`
- Run Scenario 2 (query regression): investigate → trace to slow query → diff → patch → existing tests pass → PR opened
- Run Scenario 3 (memory leak) as Tier 1 → Tier 3: restart now, patch the cache lifecycle bug, PR opened, record both the mitigation and the permanent fix separately

**Exit criteria:** PRs Phoenix opens are mergeable as-is (tests pass, diff is scoped to the actual bug) at least on your injected bugs — not necessarily generalizable to arbitrary code.

**Risk note:** if patch quality is inconsistent, keep the bug intentionally narrow (e.g., a missing index/obviously wrong join) rather than trying to make the agent robust to arbitrary regressions. The PRD's success bar is "root-cause-to-patch reasoning demoed," not "general-purpose autofix."

## Phase 6 — Escalation path (Scenario 5)

**Goal:** prove the agent can decide not to act.

- Construct a deliberately underdetermined incident (ambiguous signal, no clean correlation to any deploy/config/code change within the investigation budget)
- Add explicit "insufficient evidence" as a valid terminal state in the Remediation Engine, distinct from failure
- Verify the agent stops and reports rather than forcing a Tier 1 action out of habit

**Exit criteria:** the agent reaches "insufficient evidence, escalating" and the trace shows what it tried and why nothing was conclusive.

## Phase 7 — Safety model

**Goal:** implement the two-dimensional policy from the PRD, not just autonomous-everything.

- Config flag for execution policy: Autonomous Lab mode vs. Guarded mode
- In Guarded mode: Tier 1 auto-approved, Tier 2 configurable approval gate, Tier 3 always stops at PR (already true structurally from Phase 5, just needs to be policy-enforced, not incidental)

**Exit criteria:** flipping the policy flag visibly changes agent behavior on the same incident (e.g., Tier 2 pauses for approval in Guarded mode, proceeds immediately in Lab mode).

## Phase 8 — Incident memory (scoped small)

**Goal:** structured JSON record per incident, referenced as supporting context — not a retrieval system.

- Store: service, symptoms, root cause, tier used, fix reference, verification result
- On a repeat scenario, have Phoenix note "this matches incident #X" in its trace
- Keep this phase small; if it's eating more than a day or two, cut it — it's explicitly non-critical per the PRD

## Phase 9 — Dashboard

**Goal:** make the reasoning trace visible in under two minutes.

- Single incident-detail page: evidence gathered, hypotheses considered/discarded, tier chosen and why, verification result, final status
- Build this after Phase 6, not before — you need real traces to design the page around, not a mockup you then force the agent's output into

**Exit criteria:** you can open the dashboard cold and narrate any of the 5 scenarios from the trace alone, without reading logs.

## Suggested build order summary

| Phase | Adds | Depends on |
|---|---|---|
| 0 | Environment | — |
| 1 | Deterministic chaos injection | 0 |
| 2 | Observer + Supervisor (read-only) | 0, 1 |
| 3 | Tier 1 + Verification | 2 |
| 4 | Tier 2 (rollback) | 3 |
| 5 | Tier 3 (code patch + PR) | 4 |
| 6 | Escalation (no-op) | 2 |
| 7 | Safety policy | 3, 4, 5 |
| 8 | Incident memory (optional depth) | 3 |
| 9 | Dashboard | all of the above |

## What to cut first if time runs short

In order: Phase 8 (incident memory) → reduce Phase 5 to one clean bug only, skip the memory-leak Tier 3 fix and leave it Tier 1-only → reduce dashboard polish to a single JSON-trace viewer rather than a designed UI. Do not cut Phase 6 (escalation) — it's cheap to build and is the strongest evidence that the agent reasons rather than follows a script.

## Risks to track from day one

- **Patch quality variance in Phase 5** — mitigate by keeping injected bugs narrow (see Phase 5 risk note).
- **Loop runaway / cost blowout** — guardrail goes in at Phase 3, not bolted on later.
- **Nondeterministic chaos injection** — if a scenario doesn't reproduce identically each run, evaluation numbers become meaningless; fix this before building agent logic against it (Phase 1 exit criteria exists for this reason).
- **Dashboard becoming the time sink** — explicitly sequenced last and scoped to one page for this reason.
