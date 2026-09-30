# Phase 3 — Tier 1 (Mitigate) + Verification Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Phoenix graph a remediation engine that executes one Tier 1 mitigation and a verification engine that confirms whether it worked, with a bounded attempt cap and a hard read-only boundary on the observer.

**Architecture:** One graph, extended. The router's confidence exit changes meaning from "end" to "we have a finding worth acting on" and routes to a new `remediator_node`, which checks a policy gate, snapshots the signal, and dispatches through a remediation table disjoint from `TOOL_DISPATCH`. A new `verifier_node` re-reads the signal against that snapshot and routes to `END` (resolved), back to `observer` (failed, attempts left), or `END` (exhausted / inconclusive). Category→action and category→check live in two pure deterministic modules that never import the LLM client.

**Tech Stack:** Python 3.12.4, langgraph 1.1.10 (pinned), pydantic v2, requests, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-30-phase3-tier1-mitigation-design.md` — read it before Task 1; the plan argues from it, so the spec travels with this document.

## Global Constraints

- **Tests are offline and deterministic.** No live LLM, no network, no live database, no Docker. `psycopg` is not installed. Every test that needs a tool result stubs it.
- **The LLM never selects an action, never sets confidence, and never decides routing.** `Hypothesis` keeps having no score field. The router reads only deterministic state.
- **Observer tools stay read-only.** `TOOL_SCHEMAS` names exactly these five, spelled out as literals in the boundary test: `query_prometheus`, `query_loki`, `get_container_state`, `inspect_health`, `get_recent_deployments`.
- **Mutation code never joins `TOOL_DISPATCH`.** Remediation lives in `remediation_dispatch.py` and is dispatched only by `remediator_node`.
- **`validate_assignment=True` on `AgentState` is load-bearing.** Every node writes typed values; never assign a raw dict where a model is annotated.
- **A failure is never a measurement, and "could not check" is never "recovered."** An unobtainable verification signal yields `inconclusive`, never `pass`.
- **New code carries a docstring in the house voice** — the *why*, the failure it forecloses, the alternative rejected. Look at `scoring._is_usable` and `graph.should_continue` for the register. This codebase documents its reasoning; do not write `"""Restart a container."""`.
- **Persistence is `audit_log` only.** No migration, no new table. Use `record_audit(incident_id, node, event_type, detail, reasoning_text)`.
- **Scenario 3 names the service `checkout-service`**, which is also its `container_name` in `docker-compose.yml`. Phase 3 uses `state.service_name` as the container name.
- **Commit after every task.** Message style matches Step C: `fix:`, `feat:`, `test:`, `docs:`, `chore:` with a short subject.

## Review Focus

Five input classes the spec implies that the tasks below would otherwise leave untested. Each is assigned to the task that owns the code.

1. **The container does not exist** (service name that docker does not know, or a proxy that returns 404). Expected: the action reports an error, the run escalates, and **verification never runs** — a health check against a nonexistent container must never be read as a pass.
2. **Prometheus answers but has no data for the query** — `{"status": "success", "data": {"result": []}}`. Expected: `inconclusive`, distinct from the unreachable case and never `pass`. An empty result is the most likely way this loop meets a working-but-silent Prometheus.
3. **A pre-action snapshot taken for a previous attempt is reused on a retry.** Expected: the comparison uses the snapshot taken immediately before *this* action, so a retry cannot verify itself against a stale reading.
4. **`hypotheses` is empty when `remediator_node` runs** (a misroute, or a run that cleared its hypotheses). Expected: no action, no tool call, escalated. Distinct from `action_unavailable`, which has a hypothesis with no mapped action.
5. **A caller builds `AgentState` with a `policy_mode` that is not one of the two literals.** Expected: `validate_assignment` rejects it at construction, so an unknown policy string cannot become a default-allow.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `phoenix/tools/loki_tool.py` | Modify: validate `minutes` before arithmetic | 1 |
| `phoenix/graph/nodes.py` | Modify: `_evidence_words` skips unusable reads | 1 |
| `phoenix/tools/test_loki_tool.py` | Create: the `minutes` guard | 1 |
| `phoenix/graph/state.py` | Modify: six new fields, `status` widened | 2 |
| `phoenix/graph/test_state.py` | Create: defaults + `validate_assignment` | 2 |
| `phoenix/graph/remediation_policy.py` | Create: category→action table, `plan_action` | 3 |
| `phoenix/graph/test_remediation_policy.py` | Create: the table, no-action cases, no LLM import | 3 |
| `phoenix/graph/remediation_dispatch.py` | Create: the mutating dispatch table | 4 |
| `phoenix/graph/test_remediation_dispatch.py` | Create: the read-only boundary | 4 |
| `phoenix/graph/verification.py` | Create: `read_signal`, `run_check` | 5 |
| `phoenix/graph/test_verification.py` | Create: every check, every inconclusive path | 5 |
| `phoenix/graph/nodes.py` | Modify: add `remediator_node`, `verifier_node` | 6, 7 |
| `phoenix/graph/test_nodes.py` | Modify: node tests | 6, 7 |
| `phoenix/graph/graph.py` | Modify: nodes, edges, destinations, router exit | 8 |
| `phoenix/graph/test_graph.py` | Modify: threshold test, happy-path compiled test | 8 |
| `phoenix/graph/test_graph.py` | Modify: hard-path compiled tests | 9 |

`nodes.py` grows past 400 lines across Tasks 6 and 7. That is acceptable for now — splitting the whole read-only observer out is a refactor Phase 3 does not need, and doing it mid-phase would put unrelated churn in a safety-critical diff. If it passes 500 lines, note it in the phase summary rather than splitting.

---

### Task 1: Close the two deferred safety hazards

The spec's D5. Two one-liners, but the first one changes from "kills a read-only process" to "kills the process mid-remediation" the moment Task 6 exists, so it goes first.

**Files:**
- Modify: `phoenix/tools/loki_tool.py:8-15`
- Modify: `phoenix/graph/nodes.py:60-75`
- Create: `phoenix/tools/test_loki_tool.py`
- Test: `phoenix/graph/test_nodes.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `query_loki(logql: str, minutes: int = 15) -> dict` unchanged in signature, now returning `{"status": "error", "error": ...}` for a bad `minutes` instead of attempting the allocation. `phoenix.tools` gains a test file, which it did not have.

- [ ] **Step 1: Write the failing test for the Loki guard**

Create `phoenix/tools/test_loki_tool.py`:

```python
import pytest

from phoenix.tools import loki_tool


@pytest.mark.parametrize("bad", ["15", 0, -5, 100000, 15.5, True, None])
def test_a_minutes_value_that_is_not_a_sane_int_is_refused_without_allocating(bad):
    result = loki_tool.query_loki('{container="x"}', minutes=bad)

    assert result == {"status": "error", "error": f"invalid minutes: {bad!r}"}
```

The `15.5` and `True` cases are the ones that matter beyond the obvious: `isinstance(True, int)` is `True` in Python, so a naive `isinstance` guard passes a bool through and computes `60 * 1_000_000_000`, silently querying one minute while the caller asked for a truthy value. The test names the behaviour the guard has to have.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest phoenix/tools/test_loki_tool.py -q`
Expected: the string case `"15"` raises `MemoryError` or the process is killed; the int cases fail the assertion. A hard kill here is the bug, not a broken environment.

- [ ] **Step 3: Add the guard to `query_loki`**

Add a module constant `MAX_LOKI_MINUTES = 1440` beside `LOKI_URL`. As the first statement of `query_loki`, before `now_ns = time.time_ns()`:

```python
if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= MAX_LOKI_MINUTES:
    return {"status": "error", "error": f"invalid minutes: {minutes!r}"}
```

Document why the type check exists and not just the range: the caller is `TOOL_DISPATCH`, which forwards `args.get("minutes", 15)` straight from LLM-authored arguments, so a JSON string arrives as `str` and `"15" * 60 * 1_000_000_000` asks for roughly 900 GB. `except requests.RequestException` cannot catch it — a `MemoryError` is catchable, but under `vm.overcommit_memory=1` the allocation draws an OOM-kill, which is SIGKILL and uncatchable.

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest phoenix/tools/test_loki_tool.py -q`
Expected: PASS, 7 cases.

- [ ] **Step 5: Write the failing test for the evidence-retirement hazard**

Add to `phoenix/graph/test_nodes.py`:

```python
def test_a_failed_read_does_not_retire_an_evidence_request():
    request = "confirm the prometheus error rate is climbing"
    evidence = [
        {
            "source": "query_prometheus",
            "summary": "query_prometheus({'promql': 'x'})",
            "raw_data": {"status": "error", "error": "Connection refused: error rate confirm prometheus"},
        }
    ]

    assert nodes._pending_evidence_requests(_hypotheses_needing(request), evidence) == [request]
```

Use whatever helper `test_nodes.py` already has for building a `ScoredHypothesis` whose `hypothesis.needs_evidence` is `[request]`; match the file's existing style rather than inventing a second one.

- [ ] **Step 6: Run it to verify it fails**

Run: `python -m pytest phoenix/graph/test_nodes.py -q -k failed_read_does_not_retire`
Expected: FAIL — the request is retired, because `_evidence_words` counts `"Connection refused"`.

- [ ] **Step 7: Make `_evidence_words` skip unusable reads**

In `nodes.py`, `_evidence_words` gains a guard at the top of the loop:

```python
for item in evidence:
    if not scoring._is_usable(item):
        continue
    words.update(_WORD.findall(_returned_text(item.get("raw_data")).lower()))
```

`nodes.py` already does `from phoenix.graph import scoring`, so no import changes. Extend the existing docstring to say why: a failure's words are the transport's complaint, and a request must not retire against one — the same reason `scoring._is_usable` exists and the same false-all-clear family it was written for.

- [ ] **Step 8: Run the full graph suite to verify nothing regressed**

Run: `python -m pytest phoenix -q`
Expected: PASS. 165 total: the 157 from Step C, plus 8 new. Note the suite is `phoenix`, not `phoenix/graph` — Task 1 adds the first test under `phoenix/tools`, and a `phoenix/graph` run would silently skip it.

- [ ] **Step 9: Commit**

```bash
git add phoenix/tools/loki_tool.py phoenix/tools/test_loki_tool.py phoenix/graph/nodes.py phoenix/graph/test_nodes.py
git commit -m "fix: refuse unusable Loki minutes and failed-read evidence text"
```

---

### Task 2: Extend AgentState

**Files:**
- Modify: `phoenix/graph/state.py`
- Create: `phoenix/graph/test_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces — six fields on `AgentState`, relied on by Task 3 (`policy_mode`, `hypotheses`), Task 5 (`verification_delay_seconds`), and Tasks 6–7 (all of them):

```python
remediation_attempts: int = 0
max_remediation_attempts: int = 2
policy_mode: Literal["autonomous_lab", "guarded"] = "autonomous_lab"
planned_action: Optional[dict] = None
verification_result: Optional[dict] = None
verification_delay_seconds: int = 15
status: Literal["investigating", "confident", "resolved", "action_unavailable", "escalated"] = "investigating"
```

- [ ] **Step 1: Write the failing tests**

Create `phoenix/graph/test_state.py`:

```python
import pytest
from pydantic import ValidationError

from phoenix.graph.state import AgentState


def _state(**overrides) -> AgentState:
    return AgentState(incident_id=1, service_name="checkout-service", **overrides)


def test_a_fresh_state_has_taken_no_action_and_acts_under_the_default_policy():
    state = _state()

    assert state.remediation_attempts == 0
    assert state.max_remediation_attempts == 2
    assert state.policy_mode == "autonomous_lab"
    assert state.planned_action is None
    assert state.verification_result is None
    assert state.verification_delay_seconds == 15
    assert state.status == "investigating"


def test_a_policy_mode_outside_the_two_literals_is_rejected():
    with pytest.raises(ValidationError):
        _state(policy_mode="yolo")


def test_a_status_outside_the_five_literals_is_rejected():
    with pytest.raises(ValidationError):
        _state(status="rebooting")


def test_the_attempt_cap_can_be_lowered_but_not_negative():
    with pytest.raises(ValidationError):
        _state(max_remediation_attempts=-1)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest phoenix/graph/test_state.py -q`
Expected: FAIL on the attribute errors and on the two `ValidationError` cases for literals that do not exist yet.

- [ ] **Step 3: Add the fields to `state.py`**

Append the six fields after `escalation_reason`, each with a `Field(..., description=...)` in the style of the existing `tokens_spent` and `token_budget`. Widen the `status` annotation to the five literals.

The descriptions carry decisions the implementer would otherwise have to re-derive:

- `remediation_attempts` — "Mutating actions executed so far. Incremented only after a dispatch returns, never before, so a blocked or failed action and a successful one are distinguishable in the trail."
- `max_remediation_attempts` — "Hard ceiling on mutating actions for one incident, independent of token_budget: exhausting tokens does not buy extra restarts."
- `policy_mode` — "'guarded' means the action does not execute and the run escalates. The approval workflow is Phase 7; Phase 3 ships the boundary and the test, not the queue."
- `verification_result` — "Outcome is one of pass, fail, inconclusive. A signal that could not be read is inconclusive, never pass."
- `verification_delay_seconds` — "Settling time between an action and its check. 15 in a real run, 0 in tests."

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest phoenix/graph/test_state.py -q`
Expected: PASS, 4 tests.

- [ ] **Step 5: Run the graph suite, because `status` is validated on every node write**

Run: `python -m pytest phoenix -q`
Expected: PASS. If a node writes a status outside the five, this is where it surfaces.

- [ ] **Step 6: Commit**

```bash
git add phoenix/graph/state.py phoenix/graph/test_state.py
git commit -m "feat: add remediation and verification fields to AgentState"
```

---

### Task 3: `remediation_policy` — the category→action table

**Files:**
- Create: `phoenix/graph/remediation_policy.py`
- Create: `phoenix/graph/test_remediation_policy.py`

**Interfaces:**
- Consumes: `AgentState.hypotheses` (`list[ScoredHypothesis]`, already ranked, index 0 is top), `AgentState.service_name`.
- Produces — relied on by Task 6:

```python
@dataclass(frozen=True)
class ActionPlan:
    available: bool
    action: str | None
    container: str | None
    check: str | None
    reasoning: str

CATEGORY_ACTIONS: dict[str, tuple[str, ...]] = {
    "crash": ("restart_service",),
    "overload": ("restart_service",),
    "deploy": (),
    "config": (),
    "network": (),
    "unknown": (),
}

def plan_action(state: AgentState) -> ActionPlan: ...
```

- [ ] **Step 1: Write the failing tests**

Create `phoenix/graph/test_remediation_policy.py`:

```python
from phoenix.graph.remediation_policy import CATEGORY_ACTIONS, plan_action
from phoenix.graph.schemas import Hypothesis, ScoredHypothesis
from phoenix.graph.state import AgentState

SERVICE = "checkout-service"


def _state(category: str | None, **overrides) -> AgentState:
    hypotheses = (
        [ScoredHypothesis(hypothesis=Hypothesis(description="d", category=category), score=0.9, score_breakdown={})]
        if category
        else []
    )
    return AgentState(incident_id=1, service_name=SERVICE, hypotheses=hypotheses, **overrides)


def test_a_crash_category_plans_a_restart_of_the_service_container():
    plan = plan_action(_state("crash"))

    assert plan.available is True
    assert plan.action == "restart_service"
    assert plan.container == SERVICE
    assert plan.check == "crash"
    assert "crash" in plan.reasoning


def test_an_overload_category_plans_a_restart():
    assert plan_action(_state("overload")).action == "restart_service"


def test_a_deploy_category_plans_no_action_and_says_which_tier_would():
    plan = plan_action(_state("deploy"))

    assert plan.available is False
    assert plan.action is None
    assert "Tier 2" in plan.reasoning


def test_no_action_is_available_without_a_hypothesis():
    plan = plan_action(_state(None))

    assert plan.available is False
    assert plan.action is None
```

Plus, for the four unmapped categories and the determinism requirement:

```python
def test_every_category_in_the_hypotheses_enum_either_maps_to_an_action_or_maps_to_nothing():
    from phoenix.graph.schemas import HYPOTHESIS_CATEGORIES

    assert set(HYPOTHESIS_CATEGORIES) == set(CATEGORY_ACTIONS)


def test_planning_twice_gives_the_same_plan():
    state = _state("crash")
    assert plan_action(state) == plan_action(state)
```

Check `phoenix/graph/schemas.py` for the actual name of the category literal collection and use it; if it has no such constant, derive the set from `scoring.CATEGORY_KEYWORDS` instead, which is the table the categories are already defined by, and say so in the test's docstring.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest phoenix/graph/test_remediation_policy.py -q`
Expected: FAIL on import — no module.

- [ ] **Step 3: Implement `remediation_policy.py`**

Pure module. Imports `dataclasses.dataclass` and `phoenix.graph.state.AgentState` only — **no `llm_client` import**, and Task 3's test suite pins that.

`plan_action` in full:

1. If `not state.hypotheses`, return `ActionPlan(False, None, None, None, "no hypothesis survived scoring, so there is nothing to act on")`.
2. `category = state.hypotheses[0].hypothesis.category`.
3. `actions = CATEGORY_ACTIONS.get(category, ())`. If empty, return `ActionPlan(False, None, None, None, f"a {category} root cause has no Tier 1 action; the correct action is Tier 2 (rollback), which is Phase 4")`.
4. Otherwise return `ActionPlan(True, actions[0], state.service_name, category, f"top hypothesis is {category}, which maps to {actions[0]}")`.

Document, in the module docstring, the two decisions a reader cannot recover from the code:

- **Why the table is two rows wide.** `overload` is genuinely ambiguous — memory exhaustion wants `restart_service`, queue saturation wants `pause_worker` — and nothing in today's evidence separates them. `pause_worker` as a fallback would be a guess, and a guess here is the LLM-influenced decision the whole module exists to prevent. So `pause_worker`, `resume_worker`, and `clear_approved_cache` stay implemented and allowlisted in `remediation_tool.py` but unrouted; they enter this table as a data change when a scenario demands them.
- **Why the LLM's category reaches the action.** The LLM picks the category, which selects this row. It cannot reach any other row. An LLM can name whichever family the evidence already supports, which is the boundary the spec records, not a violation of it.

Also note that entries are ordered tuples where the first is the least invasive that could suffice — the extension point for fallbacks, none of which exist yet.

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest phoenix/graph/test_remediation_policy.py -q`
Expected: PASS.

- [ ] **Step 5: Add the no-LLM-import test**

```python
def test_the_policy_module_never_reaches_the_llm_client():
    source = (Path(__file__).parent / "remediation_policy.py").read_text(encoding="utf-8")

    assert "llm_client" not in source
```

Read it as a source check rather than an import check on purpose: an import check passes if the module imports the client lazily inside a function, which is exactly the shape this must not take. Add the `from pathlib import Path` import.

- [ ] **Step 6: Commit**

```bash
git add phoenix/graph/remediation_policy.py phoenix/graph/test_remediation_policy.py
git commit -m "feat: deterministic category to action policy with explicit no-action"
```

---

### Task 4: The remediation dispatch table and the read-only boundary

The boundary is a global invariant that is easy to lose in a later refactor, so it gets its own task and its own review gate.

**Files:**
- Create: `phoenix/graph/remediation_dispatch.py`
- Create: `phoenix/graph/test_remediation_dispatch.py`

**Interfaces:**
- Consumes: `phoenix.tools.remediation_tool.restart_service(container_name: str) -> dict`.
- Produces:

```python
REMEDIATION_DISPATCH: dict[str, Callable[[str], dict]] = {
    "restart_service": remediation_tool.restart_service,
}
```

Every value takes exactly one positional argument, the container name, and returns the tool's `{"status": "ok"|"error", ...}` envelope unchanged.

- [ ] **Step 1: Write the failing boundary test**

Create `phoenix/graph/test_remediation_dispatch.py`:

```python
from phoenix.graph import nodes
from phoenix.graph.llm_client import TOOL_SCHEMAS
from phoenix.graph.remediation_dispatch import REMEDIATION_DISPATCH

OBSERVER_TOOLS = {
    "query_prometheus",
    "query_loki",
    "get_container_state",
    "inspect_health",
    "get_recent_deployments",
}


def test_no_mutating_action_is_reachable_from_the_observer():
    assert set(REMEDIATION_DISPATCH) & set(nodes.TOOL_DISPATCH) == set()


def test_every_action_the_policy_can_plan_is_dispatchable():
    from phoenix.graph.remediation_policy import CATEGORY_ACTIONS

    for actions in CATEGORY_ACTIONS.values():
        for action in actions:
            assert action in REMEDIATION_DISPATCH, action


def test_the_llm_is_offered_exactly_the_five_read_only_tools():
    assert {schema["function"]["name"] for schema in TOOL_SCHEMAS} == OBSERVER_TOOLS
    assert set(nodes.TOOL_DISPATCH) == OBSERVER_TOOLS
```

The third test is the Step C fix from the final review: the set is spelled out as a literal rather than derived from `TOOL_DISPATCH`, so adding a mutating tool to the dispatch without adding it to `TOOL_SCHEMAS` fails here. Keep it that way.

If the `TOOL_SCHEMAS` entry shape is not `{"function": {"name": ...}}`, read lines 66–130 of `phoenix/graph/llm_client.py` and index the actual key. Do not change `llm_client.py` to make the test easier.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest phoenix/graph/test_remediation_dispatch.py -q`
Expected: FAIL on import — no module.

- [ ] **Step 3: Implement `remediation_dispatch.py`**

```python
"""The only table a mutating action can be dispatched through.

Separate from nodes.TOOL_DISPATCH on purpose. That table is what the LLM's tool
decisions resolve into, so anything in it is reachable from the observer, and the
observer is read-only by the spec's first safety property. Keeping the mutating
actions in their own table is what makes that a property the test suite can
enforce rather than a property the code comments assert.
"""
```

Then the one-entry dict. Note in the docstring that `pause_worker`, `resume_worker`, and `clear_approved_cache` are allowlisted in `remediation_tool.py` and deliberately absent here, matching `CATEGORY_ACTIONS`.

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest phoenix/graph/test_remediation_dispatch.py -q`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add phoenix/graph/remediation_dispatch.py phoenix/graph/test_remediation_dispatch.py
git commit -m "feat: separate remediation dispatch table, pin the read-only boundary"
```

---

### Task 5: `verification` — reading a signal and judging it

**Files:**
- Create: `phoenix/graph/verification.py`
- Create: `phoenix/graph/test_verification.py`

**Interfaces:**
- Consumes: `phoenix.tools.prometheus_tool.query_prometheus(promql: str) -> dict`, `phoenix.tools.health_tool.inspect_health(service_name: str) -> dict`, `phoenix.tools.docker_tool.get_container_state(container_name: str) -> dict`, and `scoring._is_usable` for the shared failure-envelope rule.
- Produces — relied on by Task 6 (for the pre-action snapshot) and Task 7 (for the check):

```python
OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
OUTCOME_INCONCLUSIVE = "inconclusive"

MEMORY_DROP_RATIO = 0.5      # after must sit below this fraction of before
SLOPE_WINDOW_MINUTES = 5     # short window; the alert's 30m is not for verification

def read_signal(category: str, service_name: str) -> dict: ...
def run_check(category: str, service_name: str, before: dict, action_at: str | None) -> tuple[str, dict]: ...
```

`read_signal` returns either a usable payload or the tool's own error envelope, unchanged. `run_check` returns `(outcome, detail)` where `detail` carries the signal, the before and after values, and the reason.

- [ ] **Step 1: Write the failing tests**

Create `phoenix/graph/test_verification.py`. The load-bearing test is the first one — write it first, alone, and get it failing before writing any other:

```python
def test_a_signal_that_cannot_be_read_is_never_a_pass():
    monkeypatch.setattr(verification, "query_prometheus", lambda promql: {"status": "error", "error": "Connection refused"})

    outcome, detail = verification.run_check("overload", "checkout-service", {"bytes": 900_000_000}, None)

    assert outcome == "inconclusive"
    assert detail["reason"] != ""
```

Then the unreachable-but-successful case from Review Focus #2:

```python
def test_a_prometheus_that_answers_with_no_data_is_inconclusive_not_a_pass(monkeypatch):
    monkeypatch.setattr(verification, "query_prometheus", lambda promql: {"status": "success", "data": {"result": []}})

    outcome, _ = verification.run_check("overload", "checkout-service", {"bytes": 900_000_000}, None)

    assert outcome == "inconclusive"
```

Then the passing and failing memory cases:

```python
def test_memory_that_dropped_below_half_the_pre_action_reading_passes(monkeypatch):
    monkeypatch.setattr(verification, "query_prometheus", lambda promql: _series("bytes", 400_000_000))

    outcome, _ = verification.run_check("overload", "checkout-service", {"bytes": 900_000_000}, None)

    assert outcome == "pass"


def test_memory_that_did_not_drop_fails(monkeypatch):
    monkeypatch.setattr(verification, "query_prometheus", lambda promql: _series("bytes", 880_000_000))

    outcome, _ = verification.run_check("overload", "checkout-service", {"bytes": 900_000_000}, None)

    assert outcome == "fail"
```

`_series` is a local helper building the **exact** shape `query_prometheus` returns — Prometheus's raw instant-vector response, value as a **string**:

```python
def _series(bytes_value: int) -> dict:
    """The shape query_prometheus actually returns: a raw instant vector whose
    sample value is a string, because that is what Prometheus's JSON encodes."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {
                        "__name__": "container_memory_working_set_bytes",
                        "name": SERVICE,
                    },
                    "value": [1759000000.123, str(bytes_value)],
                }
            ],
        },
    }
```

`prometheus_tool.py` returns `response.json()` unchanged, so this is not a shape this project invented — it is Prometheus's `/api/v1/query` response. The value is a **string** and must be parsed with `float()`.

Getting this wrong is the Step C defect the final review caught: tests that pin 0.9 for `crash` feed a `{"status", "text"}` envelope that `query_prometheus` never returns, so they pass while the real path cannot. A test whose fixture the tool cannot produce is a test of the fixture.

Then the crash category, using the `action_at` comparison:

```python
def test_a_container_that_was_already_running_before_the_action_does_not_verify(monkeypatch):
    monkeypatch.setattr(verification, "get_container_state", lambda name: _state_payload(status="running", started_at="2026-09-30T09:00:00Z"))
    monkeypatch.setattr(verification, "inspect_health", lambda name: {"container": {"status": "running"}, "app": {"status": "ok"}})

    outcome, _ = verification.run_check("crash", "checkout-service", {}, action_at="2026-09-30T10:00:00Z")

    assert outcome == "fail"


def test_a_container_started_after_the_action_which_is_now_healthy_passes(monkeypatch):
    monkeypatch.setattr(verification, "get_container_state", lambda name: _state_payload(status="running", started_at="2026-09-30T10:00:30Z"))
    monkeypatch.setattr(verification, "inspect_health", lambda name: {"container": {"status": "running"}, "app": {"status": "ok"}})

    outcome, _ = verification.run_check("crash", "checkout-service", {}, action_at="2026-09-30T10:00:00Z")

    assert outcome == "pass"
```

And a synthetic check, per the spec:

```python
def test_a_healthy_container_whose_app_health_probe_fails_does_not_verify(monkeypatch):
    monkeypatch.setattr(verification, "get_container_state", lambda name: _state_payload(status="running", started_at="2026-09-30T10:00:30Z"))
    monkeypatch.setattr(verification, "inspect_health", lambda name: {"container": {"status": "running"}, "app": {"status": "error", "error": "503"}})

    outcome, _ = verification.run_check("crash", "checkout-service", {}, action_at="2026-09-30T10:00:00Z")

    assert outcome == "fail"
```

**The spec's "open implementation detail" — resolved with no new code.** The spec flagged that no read-only tool returns container start time, and left two acceptable resolutions. One already exists: `get_container_state` (`docker_tool.py:18`) fetches `GET /containers/{name}/json` through the proxy and returns `response.json()` **unchanged**, so the whole docker inspect payload is already in hand — `State.Status` and `State.StartedAt` both. Read `State.StartedAt` off the payload `read_signal` already collected.

So: no new proxy route, no new tool, no edit to `ALLOWED_DOCKER_ACTIONS`, and nothing added to `TOOL_DISPATCH`. The tests above stub `get_container_state` with a docker-inspect payload whose `State` carries both fields. Note this in the phase summary — the spec's open question is closed by a read that was already being made.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest phoenix/graph/test_verification.py -q`
Expected: FAIL on import — no module.

- [ ] **Step 3: Implement `verification.py`**

The module docstring carries the decision that is not recoverable from the code: **the Task 7 alert's 30m `deriv` window is deliberately slow so one request cannot trip it, and reusing it for verification would make the agent wait ~40 minutes to confirm a restart.** A container that was just restarted is at baseline by definition, so verification compares absolute working set before and after, which is both faster and the more direct test.

`read_signal(category, service_name)`:
- `overload` → `query_prometheus` with a code-authored PromQL for `container_memory_working_set_bytes{name="<service>"}`, returning the single most recent sample as `{"bytes": <int>}`.
- `crash` → `{"state": get_container_state(service_name), "health": inspect_health(service_name)}`.
- anything else → the error envelope `{"status": "error", "error": f"no verification check for category {category!r}"}`.

It never raises. A tool that raises is caught and returned as the failure envelope, because a verification that crashes the graph is the one thing that must not happen once a real action has run.

`run_check(category, service_name, before, action_at)`:
- Read the signal with `read_signal`. If the result is not usable — reuse `scoring._is_usable`, the same predicate Step 1 made the evidence path use — return `(OUTCOME_INCONCLUSIVE, {"reason": ...})`. **This single line is the reason Task 1 existed.**
- `overload` → pass when `after["bytes"] < before["bytes"] * MEMORY_DROP_RATIO` **and** the short-window slope is not climbing; fail when the reading is obtainable and the drop did not happen; inconclusive when the slope query returns nothing. Document that the first condition proves the restart freed the memory and the second catches a leak that came straight back.
- `crash` → pass when the container is running, `started_at` is **newer than `action_at`**, and `inspect_health`'s app channel is ok; fail when any of those is obtainable and untrue; inconclusive when `started_at` cannot be read. The start-time comparison is what proves *this action* restarted it, rather than the container having been up already.
- `before` missing or empty → `(OUTCOME_INCONCLUSIVE, ...)` for `overload`, which cannot compare without a snapshot. `crash` does not need `before` and ignores it.

Add a module docstring note: the PromQL here is authored by this module, never by the model, which is what keeps the observer's five tools the whole read surface.

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest phoenix/graph/test_verification.py -q`
Expected: PASS, 8 tests.

- [ ] **Step 5: Run the full graph suite**

Run: `python -m pytest phoenix -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add phoenix/graph/verification.py phoenix/graph/test_verification.py
git commit -m "feat: category-driven verification with an explicit inconclusive outcome"
```

---

### Task 6: `remediator_node`

**Files:**
- Modify: `phoenix/graph/nodes.py` (append)
- Modify: `phoenix/graph/test_nodes.py`

**Interfaces:**
- Consumes: `plan_action(state) -> ActionPlan` (Task 3), `REMEDIATION_DISPATCH` (Task 4), `read_signal(category, service_name) -> dict` (Task 5), `record_audit(incident_id, node, event_type, detail, reasoning_text)` (existing).
- Produces:

```python
def remediator_node(state: AgentState) -> Command: ...
```

Returns `Command(goto="verifier")` after a successful dispatch, `Command(goto=END, update={"status": ..., "escalation_reason": ...})` otherwise. Writes `state.planned_action`, and increments `state.remediation_attempts` only after a dispatch that returned `status == "ok"`.

Audit event types, all with `node="remediator"`: `action_unavailable`, `action_blocked_by_policy`, `action_attempts_exhausted`, `action_failed`, `action_executed`.

- [ ] **Step 1: Write the failing tests**

Add to `phoenix/graph/test_nodes.py`. The gating order is the point of this node, so test it as an ordering claim:

```python
def test_guarded_mode_does_not_reach_the_action(monkeypatch):
    called = []
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: called.append(name))
    state = _remediator_state(category="crash", policy_mode="guarded")

    command = nodes.remediator_node(state)

    assert called == []
    assert command.goto == END
    assert state.remediation_attempts == 0


def test_the_attempt_cap_stops_a_misrouted_remediator_before_it_dispatches(monkeypatch):
    called = []
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: called.append(name))
    state = _remediator_state(category="crash", remediation_attempts=2)

    nodes.remediator_node(state)

    assert called == []


def test_a_deploy_finding_calls_no_tool_at_all(monkeypatch):
    called = []
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: called.append(name))
    state = _remediator_state(category="deploy")

    command = nodes.remediator_node(state)

    assert called == []
    assert command.goto == END
    assert state.status == "action_unavailable"


def test_a_successful_action_snapshots_the_signal_and_counts_the_attempt(monkeypatch):
    monkeypatch.setattr(verification, "read_signal", lambda category, service: {"bytes": 900_000_000})
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: {"status": "ok"})
    state = _remediator_state(category="overload")

    command = nodes.remediator_node(state)

    assert command.goto == "verifier"
    assert state.remediation_attempts == 1
    assert state.status == "confident"
    assert state.planned_action["action"] == "restart_service"
    assert state.planned_action["pre_action_signal"] == {"bytes": 900_000_000}


def test_a_dispatch_that_reports_an_error_never_counts_as_an_attempt_and_never_verifies(monkeypatch):
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: {"status": "error", "error": "404 Not Found"})
    state = _remediator_state(category="crash")

    command = nodes.remediator_node(state)

    assert command.goto == END
    assert state.remediation_attempts == 0
    assert state.status == "escalated"


def test_a_run_with_no_surviving_hypothesis_acts_on_nothing(monkeypatch):
    called = []
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: called.append(name))
    state = _remediator_state(category=None)

    command = nodes.remediator_node(state)

    assert called == []
    assert command.goto == END
    assert state.status == "action_unavailable"
```

The second-to-last test is Review Focus #1: a container that does not exist. Add the matching assertion that `state.verification_result is None` — verification never ran.

The last test is Review Focus #4: the remediator reached with an empty `hypotheses` list, which is a misroute rather than a category with no mapped action. It ends the same way, but for a different reason, and the two must not be conflated: `action_unavailable` with a `deploy` category means "we know what is wrong and the right tier is out of scope", while an empty list means "there is nothing to act on at all". `_remediator_state(category=None)` builds a state with no hypotheses.

`_remediator_state` is a local helper building an `AgentState` with a single top-scoring `Hypothesis` of the given category. `monkeypatch.setitem` on `REMEDIATION_DISPATCH` rather than replacing the dict, so the tests cannot leak into each other.

- [ ] **Step 2: Run it to verify they fail**

Run: `python -m pytest phoenix/graph/test_nodes.py -q -k remediator or guarded or misrouted or dispatch`
Expected: FAIL — no `remediator_node`.

- [ ] **Step 3: Implement `remediator_node`**

Append to `nodes.py`. The gating order is the design and the docstring must state it, because a future edit that reorders these lines reintroduces the bug the order prevents:

1. **Attempt cap first.** `remediator_attempts >= max_remediation_attempts` → `action_attempts_exhausted`, escalate, `goto=END`. Checked before the policy gate and before the snapshot, because this is the last thing standing between a misrouted node and a real mutation. A run that has already acted enough must not act again, whatever the policy says and whatever the LLM proposed.
2. **`plan_action(state)`.** Not available → `action_unavailable`, `status="action_unavailable"`, `goto=END`, and **no `escalation_reason`**: a confident diagnosis is a successful end, and a row calling it a failure would file a finding as a failure. This mirrors the existing router's rule that reaching the threshold records no escalation.
3. **Policy gate.** `policy_mode == "guarded"` → `action_blocked_by_policy`, escalate, `goto=END`. No attempt is consumed, because nothing was attempted.
4. **Snapshot.** `state.planned_action = {"action": ..., "container": ..., "check": ..., "reasoning": ..., "pre_action_signal": verification.read_signal(plan.check, plan.container)}`. Taken here, immediately before the action, because evidence gathered earlier in the loop may be several iterations stale and the comparison is only meaningful against the reading from seconds ago. `read_signal` never raises.
5. **Dispatch.** `result = REMEDIATION_DISPATCH[plan.action](plan.container)` inside `try/except Exception`, since this is a real network call to a real socket and an exception here unwinds past the final print to lose the run's state. On exception, use the same `{"status": "error", "error": f"{type(exc).__name__}: {exc}"}` envelope `observer_node` already uses.
6. **Result.** `result.get("status") == "ok"` → `remediation_attempts += 1`, `status="confident"`, `action_executed`, `goto="verifier"`. Otherwise → `action_failed`, `status="escalated"`, `escalation_reason` set, `goto=END`, and **no verification**: a check over an action that never ran would be a false all-clear, the same failure the spec names for an unobtainable signal.

Each branch's `detail` carries the fields that let a reader reconstruct what happened: `action`, `container`, `policy_mode`, `remediation_attempts`, `max_remediation_attempts`, and `result` for the executed and failed branches. `reasoning_text` is `plan.reasoning`, or the escalation reason for the gated branches.

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest phoenix/graph/test_nodes.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add phoenix/graph/nodes.py phoenix/graph/test_nodes.py
git commit -m "feat: remediator node with attempt cap, policy gate, and pre-action snapshot"
```

---

### Task 7: `verifier_node`

**Files:**
- Modify: `phoenix/graph/nodes.py` (append)
- Modify: `phoenix/graph/test_nodes.py`

**Interfaces:**
- Consumes: `run_check(category, service_name, before, action_at) -> tuple[str, dict]` (Task 5), `state.planned_action`, `state.remediation_attempts`, `state.max_remediation_attempts`, `state.verification_delay_seconds`.
- Produces:

```python
def verifier_node(state: AgentState) -> Command: ...
```

`pass` → `Command(goto=END, update={"status": "resolved"})`. `inconclusive` → `goto=END`, `status="escalated"`. `fail` with attempts left → `Command(goto="observer", update={"status": "investigating"})`. `fail` with attempts spent → `goto=END`, `status="escalated"`.

Audit event type `verification`, `node="verifier"`, with `detail` carrying `check`, `outcome`, `before`, `after`, and `reason`.

- [ ] **Step 1: Write the failing tests**

Add to `phoenix/graph/test_nodes.py`:

```python
def test_a_passing_check_resolves_the_incident(monkeypatch):
    monkeypatch.setattr(verification, "run_check", lambda category, service, before, action_at: ("pass", {"reason": "memory fell"}))
    state = _verifier_state(outcome_ready=True)

    command = nodes.verifier_node(state)

    assert command.goto == END
    assert state.status == "resolved"
    assert state.verification_result["outcome"] == "pass"


def test_an_inconclusive_check_escalates_rather_than_claiming_recovery(monkeypatch):
    monkeypatch.setattr(verification, "run_check", lambda category, service, before, action_at: ("inconclusive", {"reason": "Connection refused"}))
    state = _verifier_state(outcome_ready=True)

    command = nodes.verifier_node(state)

    assert command.goto == END
    assert state.status == "escalated"
    assert "escalated" not in str(state.verification_result["outcome"])


def test_a_failed_check_with_attempts_left_loops_back_to_the_observer(monkeypatch):
    monkeypatch.setattr(verification, "run_check", lambda category, service, before, action_at: ("fail", {"reason": "memory did not drop"}))
    state = _verifier_state(outcome_ready=True, remediation_attempts=1, max_remediation_attempts=2)

    command = nodes.verifier_node(state)

    assert command.goto == "observer"
    assert state.status == "investigating"


def test_a_failed_check_that_has_spent_its_attempts_escalates(monkeypatch):
    monkeypatch.setattr(verification, "run_check", lambda category, service, before, action_at: ("fail", {"reason": "memory did not drop"}))
    state = _verifier_state(outcome_ready=True, remediation_attempts=2, max_remediation_attempts=2)

    command = nodes.verifier_node(state)

    assert command.goto == END
    assert state.status == "escalated"


def test_verification_compares_against_the_snapshot_taken_for_this_action(monkeypatch):
    seen = {}

    def fake_run_check(category, service, before, action_at):
        seen["before"] = before
        return "pass", {}

    monkeypatch.setattr(verification, "run_check", fake_run_check)
    state = _verifier_state(outcome_ready=True)

    nodes.verifier_node(state)

    assert seen["before"] == {"bytes": 400_000_000}
```

That last one is Review Focus #3: the `before` passed to the check is the one inside `state.planned_action`, taken by `remediator_node` for this action, and not a value left over from an earlier attempt.

`_verifier_state` builds a state with a `planned_action` already on it carrying `check`, `container`, and `pre_action_signal`, as `remediator_node` would have written it.

- [ ] **Step 2: Run it to verify they fail**

Run: `python -m pytest phoenix/graph/test_nodes.py -q -k verify or inconclusive or failed_check or passing_check or snapshot`
Expected: FAIL — no `verifier_node`.

- [ ] **Step 3: Implement `verifier_node`**

Append to `nodes.py`.

`time.sleep(state.verification_delay_seconds)` when the value is greater than zero, immediately before the check. The delay is state, not an inline constant, so the tests above run with `verification_delay_seconds=0` and stay fast; a real run gets 15 seconds for a container to come up.

Then:

```python
check = state.planned_action["check"]
before = state.planned_action["pre_action_signal"]
outcome, detail = verification.run_check(
    check, state.planned_action["container"], before, state.planned_action.get("action_at")
)
state.verification_result = {"outcome": outcome, "check": check, **detail}
```

`action_at` is written by `remediator_node` at dispatch time as an ISO-8601 UTC string. **Add it in this task as a one-line addition to Task 6's `planned_action` write** — the verifier needs it for the `crash` start-time comparison, and retrofitting it into the remediator afterwards is how the two nodes drift out of sync.

Wrap the `run_check` call in `try/except Exception` for the same reason `observer_node` does: an exception after a real action has run unwinds past the final print and loses the run's state, including the record that the action happened.

Route on `outcome`, recording `action_executed`-style `verification` audit before returning. The `inconclusive` branch's `escalation_reason` must say the action's effect **could not be determined**, in those words — not that it failed. Those are different facts and a reader deciding whether to trust this agent needs the difference.

`pass` also clears nothing else: leave `remediation_attempts` as it is, so the trail shows how many actions it took.

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest phoenix/graph/test_nodes.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full graph suite**

Run: `python -m pytest phoenix -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add phoenix/graph/nodes.py phoenix/graph/test_nodes.py
git commit -m "feat: verifier node routing on pass, fail, and inconclusive"
```

---

### Task 8: Wire the graph

**Files:**
- Modify: `phoenix/graph/graph.py:10`, `:104-112`, `:144-154`
- Modify: `phoenix/graph/test_graph.py:172-197`

**Interfaces:**
- Consumes: `remediator_node`, `verifier_node` (Tasks 6–7).
- Produces: `ROUTER_DESTINATIONS == ("observer", "remediator", END)`; `build_graph()` with five nodes and the edges below.

- [ ] **Step 1: Write the failing test**

Modify the existing threshold test in `test_graph.py`, which currently asserts a confident run ends `investigating`. It must now assert the run leaves the diagnosis phase:

```python
def test_the_compiled_graph_routes_a_confident_run_to_the_remediator(monkeypatch):
    # the two monkeypatches from the existing threshold test, plus:
    monkeypatch.setattr(nodes, "remediator_node", lambda state: _stub_command("verifier"))
    monkeypatch.setattr(nodes, "verifier_node", lambda state: _stub_command(END, status="resolved"))
    seen = []

    def watch(state):
        seen.append("remediator")
        return _stub_command("verifier")

    monkeypatch.setattr(nodes, "remediator_node", watch)
    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert seen == ["remediator"]
    assert final["status"] == "resolved"
```

`_stub_command(goto, **update)` returns `Command(goto=goto, update=update or None)`. Add it as a local helper in the test file.

Do not keep the old assertion `final["status"] == "investigating"` — a threshold-reaching run now acts, and leaving that assertion would mean the test passes only because the stub never set a status. That is the deferred Step C finding about an unreachable literal, arriving again in a new place.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest phoenix/graph/test_graph.py -q -k routes_a_confident_run`
Expected: FAIL — the router still returns `goto=END` on the threshold.

- [ ] **Step 3: Add the nodes and edges in `build_graph`**

```python
graph.add_node("remediator", remediator_node, destinations=("verifier", END))
graph.add_node("verifier", verifier_node, destinations=("observer", END))
graph.add_edge("remediator", "verifier")
```

The explicit `destinations=` on the two new nodes is required, not decorative: langgraph validates a node's declared destinations at `compile()`, and `remediator_node` and `verifier_node` return `Command`s whose gotos would otherwise be undeclared. `verifier` declares `("observer", END)` because the retry loop is the only edge back into the investigation phase.

- [ ] **Step 4: Change the router's threshold exit**

Widen `ROUTER_DESTINATIONS` to `("observer", "remediator", END)`.

In `should_continue`, the threshold branch becomes:

```python
if state.confidence >= state.confidence_threshold:
    reason = f"confidence threshold met ({state.confidence:.2f} >= {state.confidence_threshold})"
    print(f"[router] {reason} -> remediator")
    _record_route(state, "threshold_reached", "remediator", reason)
    return Command(goto="remediator", update={"status": "confident"})
```

`event_type` stays `threshold_reached` and `escalation_reason` stays `None`, because reaching the threshold is a successful end in the sense the existing docstring means: a row calling it escalated would file a finding as a failure. What changed is that the run does not stop here.

Leave the budget and iteration branches exactly as they are. They are checked after the threshold, so a run that both met the threshold and spent its budget still routes to the remediator — correct, because the finding is real and the action is one call. Update the branch's docstring to say the threshold exit is no longer terminal and why: **a confident diagnosis with no action taken is not the deliverable the phase exists to produce.**

Do not add an attempt-cap check to the router. The cap belongs at the remediator, which is the last thing before a real mutation; duplicating it in the router would give two places to keep in sync and neither would be the safety-critical one.

- [ ] **Step 5: Run it to verify it passes**

Run: `python -m pytest phoenix/graph/test_graph.py -q`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest phoenix -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add phoenix/graph/graph.py phoenix/graph/test_graph.py
git commit -m "feat: route a confident run to the remediator and verifier"
```

---

### Task 9: Prove the hard paths on the compiled graph

Tasks 6–8 test the nodes in isolation. A node can be correct while the run it belongs to is not, and the shape bug this codebase already paid for — langgraph 1.1.10 silently discarding a `str`-returning branch function's state writes, per Ruling 22 in `nodes.py` — is only ever caught by running the compiled graph. These tests are therefore not optional and this task ships no production code.

**Files:**
- Modify: `phoenix/graph/test_graph.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces: no new interfaces. This task is the phase's proof.

**Read this before writing any test in this task.** `diagnoser_node` sets `state.confidence = scoring.top_confidence(...)` on every pass, so a `confidence` value injected into the initial state is **clobbered before the router ever sees it**. A test that injects `confidence=0.9` and stubs the observer to make no tool calls will collect no evidence, score 0.0, loop back to the observer, and end `escalated` at the iteration cap without ever reaching the remediator.

Confidence has to be earned by the run, exactly as the existing `SERVICE_DOWN` / `PANIC` test does. Add these two module-level fixtures beside `SERVICE_DOWN` and `PANIC`, both in the exact shapes their tools return:

```python
# query_prometheus returns Prometheus's raw instant vector; the sample value is a
# string. "container_memory_working_set_bytes" yields the word "memory", which is
# an overload keyword.
MEMORY_VECTOR = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {
                "metric": {"__name__": "container_memory_working_set_bytes", "name": SERVICE},
                "value": [1759000000.123, "9663676416"],
            }
        ],
    },
}

# query_loki returns Loki's raw query_range response. The log line carries
# "latency", "p95", and "memory" -- all overload keywords.
LOKI_PRESSURE = {
    "status": "success",
    "data": {
        "resultType": "streams",
        "result": [
            {
                "stream": {"container": SERVICE},
                "values": [["1759000000000000000", "p95 latency climbing, memory near limit"]],
            }
        ],
    },
}
```

`MEMORY_VECTOR` scores 0.4 (prometheus) and `LOKI_PRESSURE` scores 0.3 (loki); both support `overload`, so the 0.2 agreement bonus lands the hypothesis at **0.9**, comfortably over the 0.75 default. The observer must therefore be stubbed to *call* both tools, and both `TOOL_DISPATCH` entries replaced — an unused fixture is not evidence.

- [ ] **Step 1: Write the full-loop test**

The whole lifecycle in one compiled run, with the LLM and every tool stubbed:

```python
def test_a_run_verifies_its_own_restart_end_to_end(monkeypatch):
    order = []
    monkeypatch.setattr(nodes, "decide_tool_calls", lambda *a: (order.append("observer"), ToolCallDecision([
        {"name": "query_prometheus", "arguments": {"promql": "container_memory_working_set_bytes"}},
        {"name": "query_loki", "arguments": {"logql": '{container="checkout-service"}'}},
    ], 1))[1])
    monkeypatch.setattr(nodes, "decide_hypotheses", lambda *a: (order.append("diagnoser"), HypothesisDecision(
        DiagnoserOutput(hypotheses=[Hypothesis(description="memory is climbing", category="overload")]), 1))[1])
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: MEMORY_VECTOR)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: LOKI_PRESSURE)
    monkeypatch.setattr(verification, "read_signal", lambda category, service: {"bytes": 900_000_000})
    monkeypatch.setattr(verification, "run_check", lambda *a: (order.append("verifier"), ("pass", {"reason": "memory fell"}))[1])
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: {"status": "ok"})

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert final["status"] == "resolved"
    assert final["remediation_attempts"] == 1
    assert final["verification_result"]["outcome"] == "pass"
    assert order == ["observer", "diagnoser", "verifier"]
```

Assert on the **node order**, not only the final status: this is the test that proves the graph actually visits the remediator and the verifier in sequence, which is the whole of what a "successful" final state cannot tell you.

- [ ] **Step 2: Run it**

Run: `python -m pytest phoenix/graph/test_graph.py -q -k end_to_end`
Expected: FAIL on the first attempt, and read the failure before changing anything. A `Command`-update that never reaches the final state, a destination `compile()` rejects, or the router looping to the observer instead of reaching the remediator are all real findings about the wiring, not a broken test. **Do not work around any of them by loosening the assertion** — a failure here is the compiled graph disagreeing with a node that passes in isolation, which is precisely the class of bug this task exists to find. Fix the wiring and keep the assertions.

- [ ] **Step 3: Make it pass**

Nothing to change in production code unless Step 2 found a real defect. If it passed first time, that is the expected outcome and this step is a no-op; say so and move on rather than inventing a change.

- [ ] **Step 4: Write the bounded-retry test**

```python
def test_a_repeatedly_failing_action_stops_at_the_cap_and_escalates(monkeypatch):
    attempts = []
    monkeypatch.setattr(nodes, "decide_tool_calls", lambda *a: ToolCallDecision([
        {"name": "query_prometheus", "arguments": {"promql": "container_memory_working_set_bytes"}},
        {"name": "query_loki", "arguments": {"logql": '{container="checkout-service"}'}},
    ], 1))
    monkeypatch.setattr(nodes, "decide_hypotheses", lambda *a: HypothesisDecision(
        DiagnoserOutput(hypotheses=[Hypothesis(description="memory is climbing", category="overload")]), 1))
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: MEMORY_VECTOR)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: LOKI_PRESSURE)
    monkeypatch.setattr(verification, "read_signal", lambda category, service: {"bytes": 900_000_000})
    monkeypatch.setattr(verification, "run_check", lambda *a: ("fail", {"reason": "memory did not drop"}))
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: (attempts.append(name), {"status": "ok"})[1])

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=50)))

    assert len(attempts) == 2
    assert final["status"] == "escalated"
    assert "attempt" in final["escalation_reason"]
```

`max_iterations=50` is deliberate: it takes the iteration cap out of the picture so the assertion is about the **remediation** cap and nothing else. A run that stopped at 5 iterations would prove the wrong ceiling. This test is the direct answer to the spec's D3 concern — an agent that restarts a container that cannot be fixed by restarting, bounded.

- [ ] **Step 5: Write the no-action and policy tests**

```python
def test_a_deploy_finding_ends_as_action_unavailable_without_acting(monkeypatch):
    called = []
    monkeypatch.setattr(nodes, "decide_tool_calls", lambda *a: ToolCallDecision([], 1))
    monkeypatch.setattr(nodes, "decide_hypotheses", lambda *a: HypothesisDecision(
        DiagnoserOutput(hypotheses=[Hypothesis(description="a bad deploy", category="deploy")]), 1))
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: called.append(name))

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5)))

    assert called == []
    assert final["status"] == "action_unavailable"
    assert final.get("escalation_reason") is None


def test_a_guarded_run_never_restarts_anything(monkeypatch):
    called = []
    monkeypatch.setattr(nodes, "decide_tool_calls", lambda *a: ToolCallDecision([], 1))
    monkeypatch.setattr(nodes, "decide_hypotheses", lambda *a: HypothesisDecision(
        DiagnoserOutput(hypotheses=[Hypothesis(description="it crashed", category="crash")]), 1))
    monkeypatch.setitem(dispatch.REMEDIATION_DISPATCH, "restart_service", lambda name: called.append(name))

    final = _final(graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5, policy_mode="guarded")))

    assert called == []
    assert final["status"] == "escalated"
    assert "policy" in final["escalation_reason"]
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest phoenix -q`
Expected: PASS. Count the total and record it in the phase summary alongside Step C's 157.

- [ ] **Step 7: Commit**

```bash
git add phoenix/graph/test_graph.py
git commit -m "test: prove the remediation loop on the compiled graph"
```

- [ ] **Step 8: Verify the phase's safety properties still hold, as one command**

Run: `python -m pytest phoenix -q -k "read_only or llm or budget or tokens_spent or threshold or schema"`

Expected: PASS. These are Step C's four properties, still passing with the mutating path in the same graph. If any fails, Phase 3 has broken an invariant Step C established and that outranks every other finding in this phase.

---

## After the plan

- Live end-to-end verification is **owned by a separate session** and is not part of this plan. The two data-gated questions it must settle remain open: whether 0.75 confidence is reachable against real Prometheus payloads, and whether 1024 B/s is the right cAdvisor threshold. Do not tune either blind.
- The graph↔API coupling (Learning Record 0006) is still undecided and out of scope here.
- Open the PR against `main`: https://github.com/Yash-912/Phoenix/pull/new/phase-3-mitigation
