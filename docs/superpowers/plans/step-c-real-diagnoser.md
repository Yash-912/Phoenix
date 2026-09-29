# Step C — Real Diagnoser (Phase 2 Completion)

## Context

Phoenix is an agentic reliability engineer. A LangGraph loop (`phoenix/graph/graph.py`)
collects evidence via LLM-chosen tools, then routes on a `confidence` field to decide
whether to keep investigating.

Phase 2 is incomplete. `diagnoser_node` in `phoenix/graph/nodes.py` is a stub whose
confidence is `min(1.0, len(state.evidence) * 0.2)` — a number with no relationship to
whether a root cause was found. Meanwhile `phoenix/graph/scoring.py` (120 lines, 8 passing
tests) and `phoenix/graph/schemas.py`'s `DiagnoserOutput` are written but imported by
nothing.

Step C replaces the stub with a real Diagnoser: the LLM proposes root-cause hypotheses,
`scoring.py` decides how much to believe them in pure code, and the result is persisted to
Postgres. This completes Phase 2 and unblocks Phase 3.

## Global Constraints

These bind every task in this plan.

- **The LLM never decides confidence.** `phoenix/graph/scoring.py` is pure deterministic
  code with no model calls. The LLM may only *describe* hypotheses; it may not assign,
  adjust, or influence their scores. This is the project's core safety property (Learning
  Record 0007: guarantees live below the untrusted actor).
- **The loop-continuation decision is never LLM-driven.** `should_continue` in
  `phoenix/graph/graph.py` routes on deterministic thresholds. This must not change.
- **Observer tools stay read-only.** The five tools in `TOOL_DISPATCH`
  (`phoenix/graph/nodes.py`) are the only tools the LLM's decisions can ever execute.
  Remediation actions are executor-only and are never offered to the observer.
- **No network, no LLM, no database in unit tests.** Tests under `phoenix/graph/` must run
  offline and deterministically.
- **Follow existing code style.** Python 3.12, `from __future__ import annotations` where
  already present, module-level UPPER_CASE constants for tunables, no new dependencies.
- **Test files import absolutely** (`from phoenix.graph.scoring import ...`) and must be run
  as `python -m pytest phoenix/graph/test_scoring.py` from the repository root.
- **Add no inline comments** unless the task text explicitly calls for one.
- Do not modify files outside the ones a task names. Do not reformat untouched code.

## Weight Model (decided, binding on Task 1)

The scoring formula has three *primary* signal sources and two *secondary* sources.

Primary (unchanged from current code):
- `query_prometheus` → 0.4
- `query_loki` → 0.3
- `get_container_state` → 0.3

Secondary (new):
- `inspect_health` → 0.15
- `get_recent_deployments` → 0.15

The asymmetry is principled, not arbitrary. `inspect_health` is a *synthesis* — it calls
container state plus the app's `/health` endpoint, so counting it at full weight would
double-count evidence already seen. `get_recent_deployments` is a *change ledger* — it
records what was deployed, not whether the deployment caused the incident. Corroborating,
never independently confirming.

Consequence, and it is intended: a `deploy`-category hypothesis supported only by
`get_recent_deployments` scores 0.15, far below the 0.75 termination threshold. Confirming
a deploy as *causal* requires telemetry corroboration. This is correct behaviour, not a gap.

---

### Task 1: Fix the scoring source bug

**Files:** `phoenix/graph/scoring.py`, `phoenix/graph/test_scoring.py`

`scoring.py` buckets evidence into exactly three hardcoded sources:

```python
blobs_by_source: dict[str, list[str]] = {SOURCE_PROM: [], SOURCE_LOKI: [], SOURCE_DOCKER: []}
for item in usable:
    src = item.get("source", "")
    if src in blobs_by_source:
        blobs_by_source[src].append(_blob(item))
```

Evidence from `inspect_health` and `get_recent_deployments` is collected by the observer,
then silently discarded. `distinct_sources_with_data` counts only those same three, so the
contradiction penalty misfires as well.

This is fatal for Scenario 1 (bad deploy v18), the project's Phase 4 test: the deploy
evidence comes from `get_recent_deployments`, so a `deploy`-category hypothesis scores
0.0 unless the literal string "v18" happens to appear in Prometheus or Loki output. The
loop can then only terminate via `max_iterations`, i.e. the failure path.

#### Required change

Replace the three-key dict and the three `has_prom` / `has_loki` / `has_docker` booleans
with a source→weight map driven by the weight model above. `distinct_sources_with_data`
must be derived from evidence that actually landed in the map.

The `breakdown` dict returned by `score_hypothesis` must keep its existing keys —
`has_prometheus_signal`, `has_loki_signal`, `has_docker_signal`, `sources_supporting`,
`agreement_bonus`, `contradiction_penalty`, `weights`, `category` — because
`phoenix/graph/test_scoring.py` asserts on them. Add entries for the new sources. The
`weights` sub-dict must reflect all five.

Preserve existing behaviour exactly for the three primary sources. All 8 current tests must
still pass with unchanged expected values.

#### Required new tests

Add regression tests that are **red against the current implementation**:

1. `inspect_health` evidence alone scores non-zero for a matching category.
2. `get_recent_deployments` evidence alone scores non-zero for a `deploy` hypothesis.
3. The contradiction penalty counts `inspect_health` / `get_recent_deployments` toward
   `distinct_sources_with_data` — i.e. usable evidence from two secondary sources with
   nothing matching still incurs the 0.3 penalty.

Use the existing `_ev(source, text)` helper at `test_scoring.py:7`. Do not add fixtures,
parametrization, or a `conftest.py`.

#### Verification

```powershell
python -m pytest phoenix/graph/test_scoring.py -v
```

Must show 11 passed, output pristine (no warnings). Before the fix, the 3 new tests must
fail — capture that output as RED evidence in your report.

---

### Task 2: LLM hypothesis generation

**Files:** `phoenix/graph/llm_client.py`

Add `decide_hypotheses(service_name: str, evidence_so_far: list[dict]) -> DiagnoserOutput`,
mirroring the existing `decide_tool_calls` contract at `llm_client.py:88`.

`DiagnoserOutput` is already defined at `phoenix/graph/schemas.py:17` and imported by
nothing. It yields 1–4 ranked `Hypothesis` objects, each with `description`, `category`
(one of `crash`, `overload`, `deploy`, `config`, `network`, `unknown`), and `needs_evidence`.

The LLM returns **descriptions only**. No score field exists on `Hypothesis` and none may be
added — scoring is Task 3's job and lives in pure code.

#### Structured-output mechanism

The configured endpoint is an OpenAI-compatible provider read from `LLM_BASE_URL`,
`LLM_API_KEY`, `LLM_MODEL` (`llm_client.py:6-8`). Support for JSON-schema `response_format`
varies between free-tier providers.

Prefer `client.beta.chat.completions.parse` with the `DiagnoserOutput` schema. Wrap the call
so that if the provider rejects the structured-output request, the function falls back to
asking for JSON in the prompt and validating with `DiagnoserOutput.model_validate_json`.

Match the existing error philosophy at `llm_client.py:130-138`: one malformed item must not
take down the batch. On a total validation failure, return an empty `DiagnoserOutput` rather
than raising, and print a diagnostic to stdout in the same style as the existing discard
message.

This function must not execute anything and must not gain a tool-calling path.

---

### Task 3: Integrate scoring into the Diagnoser

**Files:** `phoenix/graph/nodes.py`, `phoenix/graph/state.py`

Rewrite `diagnoser_node` (`nodes.py:59-68`) to:

1. Call `decide_hypotheses(state.service_name, state.evidence)`.
2. Score every hypothesis with `phoenix.graph.scoring.score_all`.
3. Store the ranked results on the state.
4. Set `state.confidence = scoring.top_confidence(state.evidence, hypotheses)`.

**Delete** the line `state.confidence = min(1.0, len(state.evidence) * 0.2)` and remove the
docstring that describes it as simulated.

#### State shape change

`AgentState.hypotheses` is currently `list[Hypothesis]` (`state.py:12`), which has nowhere to
put a score or a breakdown, yet the `hypotheses` table in Postgres has `score NUMERIC NOT
NULL` and `score_breakdown JSONB NOT NULL` columns (`phoenix/db/init/002_evidence_hypotheses_audit.sql:19-20`).

Introduce a Pydantic model in `phoenix/graph/schemas.py` capturing a scored hypothesis:
the `Hypothesis` itself, its `score: float`, and its `score_breakdown: dict`. Change
`AgentState.hypotheses` to `list[ScoredHypothesis]`.

`ScoredHypothesis` is stored on the graph state only. `scoring.score_all` continues to
return `list[tuple[Hypothesis, float, dict]]` — it is pure and is not modified by this task
beyond what Task 1 requires.

#### Verification

Run the graph against a live Scenario 1 and confirm the printed confidence value changes
when the observer's collected evidence changes. A confidence that stays at a
multiples-of-0.2 value means the old stub is still in place.

---

### Task 4: Close the needs_evidence loop

**Files:** `phoenix/graph/llm_client.py`

`Hypothesis.needs_evidence` (`schemas.py:11`) is defined and never read. It is the PRD §6
"next discriminating test" mechanism and the prerequisite for Phase 4's correlation work.

Feed the surviving hypotheses' `needs_evidence` entries into the observer's next prompt
inside `decide_tool_calls`. Today that function forwards only evidence summaries
(`llm_client.py:96-99`) and has no access to hypotheses.

Extend its signature to accept the outstanding evidence requests. Do not change
`TOOL_SCHEMAS`, the allowlist, or the read-only guarantee.

---

### Task 5: Cost tracking

**Files:** `phoenix/graph/llm_client.py`, `phoenix/graph/state.py`, `phoenix/graph/graph.py`

`AgentState.cost_spent` and `cost_budget` (`state.py:17-18`) are declared and never read or
written. The plan requires a hard stop at the budget, enforced in the router.

- `llm_client` must capture `response.usage` from each LLM call and return it alongside the
  tool decisions.
- `nodes.py` accumulates into `state.cost_spent`.
- `should_continue` in `graph.py` gains a hard stop when the budget is exhausted, returning
  `"end"` and recording an escalation reason.

**Budget unit is LLM tokens, not dollars.** `cost_budget: float = 10.0` must be
renamed/reinterpreted to `token_budget: int` with a value of 20000. Renaming the field is
required — a float named `cost_budget` that counts tokens is a lie in the type system.

The budget check is deterministic code in the router. It must never consult the LLM.

---

### Task 6: Persist to Postgres

**Files:** `phoenix/graph/persist.py` (new), `phoenix/graph/nodes.py`, `phoenix/graph/graph.py`

Three tables exist with grants ready and **zero writers**: `evidence`, `hypotheses`, and
`audit_log` (`phoenix/db/init/003_create_app_role.sh:16-23`).

Create `phoenix/graph/persist.py` with its own `psycopg` connection pool. It cannot reuse
`phoenix/api/db.py` — that module uses flat sibling imports (`import db`,
`phoenix/api/main.py:5-6`) and `phoenix/api/` has no `__init__.py`. The graph also runs
host-side against `localhost:5432`.

Write three functions, one per table:
- `record_evidence(incident_id, evidence_item) -> None`
- `record_hypotheses(incident_id, scored_hypotheses) -> None`
- `record_audit(incident_id, node, event_type, detail, reasoning_text) -> None`

Wire them into the nodes so every investigation writes a complete trail.

`audit_log` has `SELECT, INSERT` only for `phoenix_app` — no `UPDATE`, no `DELETE`. Never
issue either. The grant is the enforcement mechanism; a promise in application code is not.

**Precondition:** confirm `phoenix/db/init/004_evidence_source_widen.sql` actually ran
against the live database. It widens the `evidence.source` CHECK constraint to the five tool
names, but `docker-entrypoint-initdb.d` scripts only execute on first database
initialisation. If the `postgres-data` volume predates that file, the CHECK still rejects
`inspect_health` and `get_recent_deployments` and every insert of that evidence fails.
Report which state you find; do not attempt to run migrations.

---

### Task 7: cAdvisor memory-growth alert rule

**Files:** `observability/prometheus/alert.rules.yml`

Scenario 3 (memory leak) is Phase 3's exit criterion, but it has no firing signal. All six
existing rules key on `http_*` metrics. The leak in `services/worker-service/app.py:38-39`
appends 10 KB to a module-level list per request — invisible to
`prometheus_fastapi_instrumentator`, visible only to cAdvisor, which is scraped
(`observability/prometheus/prometheus.yml:18-20`) and read by no rule.

Add a new alert group using cAdvisor's `container_memory_working_set_bytes` for the
`worker-service` container.

Two requirements that differ from the existing rules:
- The `service` label must be the **static literal** `worker-service`, not
  `{{ $labels.job }}`. cAdvisor series carry Docker labels, not the Prometheus `job` label
  the multi-service group relies on.
- The `for:` duration must be long enough that a single request cannot trip it. The leak
  is 10 KB per call, so it needs sustained traffic to become visible.

---

## Out of Scope

- **Graph↔API coupling.** `phoenix/graph/` and `phoenix/api/` remain disconnected; the graph
  still takes `<incident_id>` as a command-line argument. Learning Record 0006 records the
  open design question. Decided before Phase 3.
- **OpenTelemetry / `query_traces`.** Deferred; Phase 4's correlation work is
  diff-based and does not require traces.
- **Phases 3–9.** Plan 4.1's rollback semantics were rewritten during planning: the lab has
  no tagged images, so rollback means reverting chaos flags and writing a deployment marker,
  not `docker compose up` with a previous tag.
