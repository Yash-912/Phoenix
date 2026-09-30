# Phase 3 — Tier 1 (Mitigate) + Verification Engine — Design

Date: 2026-09-30
Status: Approved in conversation. Awaiting spec review.
Branch: `step-c-real-diagnoser` (Phase 2 / Step C complete at `4cfd609`)

## Context

Phoenix is an agentic reliability engineer. Phase 2 gave it a real diagnoser: the LLM proposes
root-cause hypotheses, deterministic pure code scores them, a router decides whether to keep
investigating, and the run persists to Postgres. Phase 2 is read-only by design — the agent can
investigate and explain, but not act.

Phase 3 is the first phase in which the agent **acts**. It adds a remediation engine that executes
Tier 1 mitigations, and a verification engine that confirms whether the action actually worked.

The phase's exit criterion, from `phoenix-implementation-plan.md`:

> a simple resource-exhaustion scenario (can reuse Scenario 3's memory leak, mitigate-only) runs
> autonomously end-to-end with a passing verification.

### What already exists

Phase 3 is closer to "wire up and govern what exists" than "build from scratch":

- `phoenix/tools/remediation_tool.py` (68 lines) already implements `restart_service`,
  `pause_worker`, `resume_worker`, and `clear_approved_cache` behind an `ALLOWED_DOCKER_ACTIONS`
  gate. It is imported by nothing.
- `docker-socket-proxy` is wired in `docker-compose.yml` with `CONTAINERS=1, POST=1`.
- Step C added a cAdvisor memory-growth alert rule
  (`observability/prometheus/alert.rules.yml:80-91`), which gives Scenario 3 a firing signal it
  previously lacked. Phase 3's verification depends on this rule's underlying metric.
- `docker-compose.yml` sets `container_name` explicitly for every service, so cAdvisor's `name`
  label is a known static string.

### What does not exist

`scale_service` (named in the implementation plan) is **not** in the allowlist and is not needed
for the exit criterion. `compare_health_before_after` and `run_synthetic_check` do not exist. The
graph has no nodes that mutate anything.

---

## Decisions

Five decisions were made in conversation. Each is binding on the implementation.

### D1 — Code maps category to action; the LLM never chooses

A deterministic module maps the top-scoring hypothesis's `category` to a candidate Tier 1 action.
The LLM proposes the hypothesis; code alone decides the action.

**Why.** Learning Record 0007 — *guarantees live below the untrusted actor* — already governs
confidence. A remediation decision is far more consequential than a confidence number: restarting a
healthy container is a real action in a real system. This keeps the LLM's influence exactly where
Step C left it, at picking a label.

**Known boundary, carried forward from the Step C final review.** The LLM picks `category`, which
selects the keyword set and therefore reaches the score. Under this design it also reaches the
action. An LLM can name whichever family the evidence already supports. This is the plan's own
boundary rather than a violation, but it is the single largest remaining influence point and should
be named in any demo.

**Rejected:** LLM-proposes-action-gated-by-code (moves LLM influence onto a real action);
code-decides-from-evidence-only (discards the diagnoser's reasoning, which the PRD calls the
product).

### D2 — Verification is category-driven, not a single generic health check

The hypothesis's category names which signal gets re-checked.

**Why.** Scenario 3 produces **no error-rate signal**. The leak appends 10 KB per request; requests
keep succeeding, so error rate and latency stay flat while memory climbs toward an OOM kill. A
generic "error rate returned to baseline" check would pass on a container one request from dying —
reporting recovery while the incident is worse. Phase 3 can only meet its exit criterion because
Step C built the cAdvisor signal.

**Rejected:** alert-inactive check via Alertmanager (elegant, but needs Alertmanager API access and
the 10m `for:` means verification waits minutes); one generic health check for all (cannot verify
Scenario 3).

### D3 — Bounded remediation attempts, then escalate

`max_remediation_attempts` (default 2) is a hard ceiling on mutating actions per incident. A failed
verification returns to the observer for re-investigation with fresh post-action evidence until the
cap is reached, then the run escalates.

**Why.** Step C's guardrails bound investigation iterations and tokens. **Nothing bounded
mutating actions.** An agent that restarts, verifies, fails, restarts again has no ceiling — a
container that cannot be fixed by restarting gets restarted until the token budget runs out, which
is minutes of real mutating actions driven by a hypothesis nobody confirmed. That is precisely the
failure mode PRD §2 names: *"a restart looks identical whether the cause was correctly identified
or not."*

### D4 — A minimal execution policy gate ships in Phase 3

`policy_mode: autonomous_lab | guarded` on state, checked in `remediator_node` before any dispatch.
`autonomous_lab` is the default so the Phase 3 demo runs end-to-end. In `guarded` mode the action
does **not** execute; the run ends escalated with `action_blocked_by_policy`.

**Why.** The implementation plan sequences the full Safety Model to Phase 7, but PRD §9 makes the
pitch *"an agent with an explicit policy boundary around what it may execute."* A boundary
introduced after the action machinery exists is the retrofit that never gets built. Phase 7's full
surface (per-tier config, approval workflow) still depends on Phases 4 and 5 and stays deferred; only
the boundary and its test land now.

**Why `guarded` means "do not execute" rather than "await approval".** The approval workflow needs
Tier 2 and Tier 3 gates that do not exist yet. Blocking and escalating honestly is correct Phase 3
behaviour; inventing a half-approval flow would be worse.

### D5 — Two Step C hazards fold in as the first task

- `phoenix/tools/loki_tool.py:15` computes `"15" * 60 * 1_000_000_000` on a malformed `minutes`,
  asking for ~900 GB. `except Exception` catches the `MemoryError`, but under
  `vm.overcommit_memory=1` the allocation draws an **OOM-kill — SIGKILL, uncatchable**. Today that
  kills a read-only process; in Phase 3 it would kill the process mid-remediation.
- `phoenix/graph/nodes.py:60` `_evidence_words` counts a failed read's error text as "returned
  words", so an evidence request can be retired by a `Connection refused` that came from a failure
  rather than a reading. Same false-all-clear family the Step C final review closed elsewhere;
  `scoring._is_usable` is already the correct predicate, sitting unused at that site.

Both are one-liners. Both become Task 1 of Phase 3.

**Still deferred:** partial-read keyword bleed (errs toward a false positive, not a false
all-clear); `TOOL_SCHEMAS` dispatchability at request time (the static set-equality test closes the
static half); and the two data-gated items — 0.75 confidence reachability and the 1024 B/s alert
threshold — which need live lab measurements and must not be tuned blind.

---

## Graph shape

```
observer → diagnoser → router ─┬─→ observer              (confidence below threshold)
                               │
                               ├─→ remediator → verifier ─┬─→ END       (verified recovered)
                               │                          ├─→ observer (verification failed, attempts left)
                               │                          └─→ END       (attempts exhausted → escalate)
                               │
                               └─→ END                    (budget or iteration cap → escalate)
```

One graph. The whole incident lifecycle — investigate, act, verify, re-investigate — is provable in
a single compiled run.

**Why not two graphs.** Step C's Ruling 22 established that langgraph 1.1.10 silently discards state
writes from a `str`-returning branch function, and that the only reason it was caught is that a test
ran the compiled graph. A two-graph design splits the lifecycle across two invocations — exactly
the shape that hides that bug class. One graph keeps it testable end to end.

**Why this does not give up the read-only guarantee.** A process boundary is the usual way to prove
the investigating phase cannot mutate. That property is obtained here more cheaply and more
strongly: remediation gets its own dispatch table (`REMEDIATION_DISPATCH`) separate from
`TOOL_DISPATCH`, and a test pins the two sets are disjoint. A test can enforce that; a process
boundary can only be asserted.

**The confidence exit changes meaning.** Today "threshold met" ends the run. In Phase 3 it means
*we have a finding worth acting on* and routes to `remediator`.

**`action_unavailable` is a first-class outcome.** A `deploy`-category root cause is a confident
diagnosis with **no valid Tier 1 action** — the correct action is a rollback, which is Tier 2 and
does not exist until Phase 4. Restarting a service because its root cause was a bad deploy is
exactly the "unhealthy → restart" behaviour PRD §2 says Phoenix must be distinguishable from real
understanding. Such a run ends `action_unavailable`: a confident diagnosis plus a clear statement
that the correct tier is out of scope. That is a better demo artifact than a wrong restart.

**Known cost.** A single invocation now spans mutations, so a crash mid-remediation loses in-flight
state. Step C's persistence means the *trail* survives; the loss is bounded to the last node's
return value.

---

## State

`AgentState` (`phoenix/graph/state.py`) gains:

| Field | Type | Default | Purpose |
|---|---|---|---|
| `remediation_attempts` | `int` | `0` | Mutating actions executed so far this incident |
| `max_remediation_attempts` | `int` | `2` | Hard ceiling (D3) |
| `policy_mode` | `Literal["autonomous_lab", "guarded"]` | `"autonomous_lab"` | Execution boundary (D4) |
| `planned_action` | `Optional[dict]` | `None` | Action, container, and the policy's reasoning |
| `verification_result` | `Optional[dict]` | `None` | Signal, before/after values, outcome, obtainability |
| `verification_delay_seconds` | `int` | `15` | Settle time between action and check; `0` in tests |

`status` extends from `Literal["investigating", "confident", "escalated"]` to include
`"resolved"` and `"action_unavailable"`.

`confident` is retained and becomes reachable: threshold met, acting on it. Step C's review flagged
it as an unreachable literal with a test asserting `final["status"] == "investigating"` on a
threshold-reaching run; Phase 3 gives it meaning and that test is updated to match.

`validate_assignment=True` already makes these annotations load-bearing on every node write.

---

## Components

### `phoenix/graph/remediation_policy.py` (new)

Pure deterministic module. No LLM import, no I/O.

```python
CATEGORY_ACTIONS: dict[str, tuple[str, ...]] = {
    "crash": ("restart_service",),
    "overload": ("restart_service",),
    "deploy": (),
    "config": (),
    "network": (),
    "unknown": (),
}
```

Ordered tuples: first entry is the least invasive that could suffice, remainder are fallbacks on
retry. `plan_action(state) -> ActionPlan` returns the chosen action, the container name, the
verification check to run, and the reasoning string — or an explicit no-action result carrying
`status="action_unavailable"` and the reason.

**The table is deliberately narrow, and that is the honest position.** Two of six categories act,
and both do the same thing. `overload` is genuinely ambiguous — memory exhaustion wants
`restart_service`, queue saturation wants `pause_worker` — and nothing in today's evidence
distinguishes them. Adding pause as a fallback would be a guess, and a guess here is exactly the
LLM-influenced decision D1 exists to prevent.

`pause_worker`, `resume_worker`, and `clear_approved_cache` therefore stay implemented and
allowlisted in `remediation_tool.py` but **unrouted**. No Phase 3 scenario needs them. They enter
`CATEGORY_ACTIONS` as a data change when a scenario demands them.

### `phoenix/graph/remediation_dispatch.py` (new)

```python
REMEDIATION_DISPATCH: dict[str, Callable] = {
    "restart_service": remediation_tool.restart_service,
}
```

Deliberately separate from `TOOL_DISPATCH`. This separation **is** the enforcement point for
read-only observation, and a test pins `REMEDIATION_DISPATCH ∩ TOOL_DISPATCH = ∅`.

### `phoenix/graph/verification.py` (new)

Category → check, matching what was acted on. Each check returns
`(outcome, detail)` where outcome is `pass`, `fail`, or `inconclusive`.

**The "before" snapshot is taken by `remediator_node` immediately before executing**, not from
evidence gathered earlier in the loop, which may be several iterations stale. That is the tightest
available comparison and it does not depend on the observer having happened to query the right thing.

- **`crash`** — container running, `/health` returns ok, and the container's start time is **newer
  than the action timestamp** (proving the restart happened rather than the container having already
  been up), plus a synthetic request exercising a real endpoint (`run_synthetic_check`).

  **Open implementation detail.** No existing read-only tool returns container start time —
  `inspect_health` reports `State.Status` but not `StartedAt`. One of two things happens, both
  acceptable: `inspect_health` grows a `started_at` field, or `verification.py` reads
  `GET /containers/{name}/json` through the proxy's existing GET route. Either stays read-only. This
  is recorded because "the restart actually happened" is a core verification claim and it needs a
  real source, not an assumption that the container being up implies it.
- **`overload`** — `container_memory_working_set_bytes{name="<service>"}` has dropped meaningfully
  below the pre-action reading, and a short-window slope is not climbing.

**Absolute drop, not growth rate.** The Task 7 alert uses a 30m `deriv` window with a 10m `for:`
deliberately, so a single request cannot trip it. That is the right call for an alert and the wrong
one for verification: making the agent wait 40 minutes to confirm a restart is not a feature. A
container that was just restarted is at baseline by definition, so comparing absolute working set
before and after is both faster and the more direct test.

Verification issues **code-authored PromQL** through the existing read-only `query_prometheus`. No
LLM, no new tools, and the tool surface stays closed at five.

---

## Safety properties

Step C's four carry forward unchanged, plus one new:

1. **The LLM never decides confidence.** `scoring.py` remains pure; `Hypothesis` keeps having no
   score field.
2. **The loop decision is never LLM-driven.** The router reads only deterministic state.
3. **Observer tools stay read-only — now enforced by test.** `REMEDIATION_DISPATCH` is disjoint from
   `TOOL_DISPATCH`; `TOOL_SCHEMAS` names exactly the five read-only tools.
4. **Budget hard stop holds**, and `max_remediation_attempts` is a second, independent ceiling on
   mutating actions. Exhausting tokens does not buy extra restarts.
5. **The agent never acts on its own authority in `guarded` mode** (new). Checked before dispatch.

---

## Failure handling

Three paths, one of which is the dangerous one.

- **Action fails to execute.** The tool returns an error. Count it as an attempt, record it, and
  route to the retry-or-escalate path. **Do not run verification** — there is nothing to verify, and
  a verification pass over an action that never ran would be a false all-clear.

- **Policy blocks.** Record `action_blocked_by_policy`, end escalated. **No attempt is consumed**,
  because nothing was attempted.

- **Signal unobtainable.** Prometheus unreachable, metric missing, `/health` timed out.
  **This must not be recorded as recovered.** "Could not check" and "checked and healthy" are
  different answers. An unobtainable signal is `inconclusive`, never `pass`, and inconclusive routes
  to escalate with the reason.

  This is the same bug class the Step C final review caught twice: the failed-read-counted-as-
  evidence defect, where a `"Connection refused"` satisfied a hypothesis and *suppressed* a
  contradiction penalty. A run that cannot verify its own action must not claim it succeeded.

---

## Persistence

**No migration.** Remediation and verification both write to `audit_log`, which already exists, is
already append-only, and already has its grants.

- `node='remediator'`, `event_type='action_executed'`, `detail` carrying
  `{action, container, result, pre_action_signal}`, `reasoning_text` carrying the policy's reason.
- `node='verifier'`, `event_type='verification'`, `detail` carrying
  `{check, outcome, before, after}`, `reasoning_text` carrying why it passed, failed, or could not
  be obtained.

A `remediations` table would be schema invented before any consumer needs it. That is Phase 8's
problem (structured incident records, PRD §11).

---

## Testing

Roughly 45 new tests, offline and deterministic — no LLM, no network, no live database, no
Docker. `psycopg` is still not installed, which is why persistence tests stub the pool.

- **`remediation_policy`** — every category including all four no-action cases; determinism (same
  state → same action, repeatedly); and a test asserting the module does not import `llm_client`.
- **`verification`** — each check against stubbed signals. The load-bearing one: **every**
  signal-unavailable path asserts `inconclusive`, never `pass`.
- **`remediator_node` / `verifier_node`** — stubbed dispatch and policy. The `guarded` path asserts
  the tool function was **never called**.
- **The two compiled-graph tests** — happy path to `resolved`; verification failure looping back to
  the observer then ending `escalated` at the attempt cap. Not optional: Ruling 22's
  discarded-state-write bug was only ever caught because a test ran the compiled graph, and this is
  the phase where that shape bug is most expensive.
- **Regression** — `action_unavailable` for a `deploy`-category finding produces no tool call at all.
- **Boundary** — `REMEDIATION_DISPATCH ∩ TOOL_DISPATCH = ∅`, and `TOOL_SCHEMAS` names exactly the
  five read-only tools, spelled out as literals rather than derived.
- **Task 1 (D5)** — red-before-green proof for both hazards: a string `minutes` no longer reaches the
  allocation, and a failure's error text no longer retires an evidence request.

---

## Live prerequisites

A separate session owns live verification of Step C. The following must hold before an end-to-end
Phase 3 demo, and the first two are Step C leftovers that were never applied:

```bash
docker compose exec -T postgres psql -U phoenix -d phoenix \
  -f /docker-entrypoint-initdb.d/004_evidence_source_widen.sql
docker compose exec -T postgres psql -U phoenix -d phoenix -c "SELECT id FROM incidents WHERE id = 1"
```

Plus `DATABASE_URL` set for the graph process, the Docker daemon running, and
`python -m chaos.memory_leak` injected.

**Known gate on the demo, not the build.** Whether 0.75 confidence is reachable in the lab is
unverified. The Step C final review found that the 0.4 `query_prometheus` weight is only reachable
if the LLM's own PromQL contains `up==0`, because `query_prometheus` returns raw instant-vector JSON
whose joined leaf values do not contain the literal the crash keywords look for. Every test pinning
0.9 for `crash` feeds a `{"status","text"}` envelope that tool never returns. If the diagnoser
cannot clear the threshold, `remediator` is never reached. This must be settled by measurement, not
tuned blind.

---

## Out of scope

- **Graph ↔ API coupling** (Learning Record 0006, deferred "before Phase 3" by the Step C plan and
  still undecided). Phase 3 runs from the CLI. Deciding whether the API triggers the graph is a
  separate design question and is not required by the exit criterion.
- **Phases 4–9.** Tier 2 rollback, Tier 3 patching, escalation path, full safety model, incident
  memory, dashboard.
- **`scale_service`.** Named in the implementation plan, absent from the allowlist, not needed for
  the exit criterion. Adding it means extending `ALLOWED_DOCKER_ACTIONS` and is a scope increase
  with no scenario behind it.
- **Wiring `pause_worker` / `resume_worker` / `clear_approved_cache`** into the graph. See D5 and
  `CATEGORY_ACTIONS`.
- **A `remediations` table or any schema migration.** See Persistence.
- **Per-tier approval workflow.** Phase 7.
- **Historical baseline storage.** Verification compares against a snapshot taken immediately
  before the action, not against stored pre-incident history.
- **Live scenario runs.** Owned by a separate session.
