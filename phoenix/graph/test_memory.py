"""Unit tests for incident memory -- no database, no network, no LLM.

Persistence is a stub pool, installed where persist.py looks for one, so the
tests see every statement memory.py hands the driver and the order they run in.
The contract under test is narrow: the record says what happened, a prior match
is written as a note and nothing else, and no failure in here reaches the run.
"""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from pathlib import Path

import pytest

from phoenix.graph import memory, persist
from phoenix.graph.schemas import Hypothesis, ScoredHypothesis
from phoenix.graph.state import AgentState

INIT_DIR = Path(__file__).resolve().parents[1] / "db" / "init"

SERVICE = "checkout-service"
INCIDENT = 12
PRIOR_ID = 7

CRASH = Hypothesis(description="checkout-service is crash looping", category="crash")
OVERLOAD = Hypothesis(description="checkout-service is saturated", category="overload")


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row else []


class _Conn:
    def __init__(self, pool):
        self._pool = pool

    def execute(self, sql, params=None):
        self._pool.statements.append((sql, params))
        if self._pool.read_error is not None and sql.lstrip().upper().startswith("SELECT"):
            raise self._pool.read_error
        row = self._pool.prior_row if sql.lstrip().upper().startswith("SELECT") else None
        return _Result(row)

    def commit(self):
        self._pool.commits += 1


class _Pool:
    def __init__(self, prior_row=None, read_error=None):
        self.prior_row = prior_row
        self.read_error = read_error
        self.statements: list[tuple[str, object]] = []
        self.commits = 0

    def connection(self):
        pool = self

        class _Ctx:
            def __enter__(self_inner):
                return _Conn(pool)

            def __exit__(self_inner, *exc):
                return False

        return _Ctx()


class _Jsonb:
    def __init__(self, obj):
        self.obj = obj


@pytest.fixture
def pool(monkeypatch):
    fake = _Pool()
    monkeypatch.setattr(persist, "_get_pool", lambda: fake)
    monkeypatch.setattr(persist, "_jsonb", lambda value: _Jsonb(value))
    persist._reported.clear()
    return fake


def _scored(hypothesis, score):
    return ScoredHypothesis(hypothesis=hypothesis, score=score, score_breakdown={})


def _state(**overrides) -> AgentState:
    base = dict(
        incident_id=INCIDENT,
        service_name=SERVICE,
        status="resolved",
        hypotheses=[_scored(CRASH, 0.9), _scored(OVERLOAD, 0.1)],
        evidence=[
            {"source": "query_prometheus", "summary": "up == 0 on checkout-service"},
            {"source": "query_loki", "summary": "panic: index out of range"},
            {"source": "query_loki", "summary": "panic: index out of range"},
        ],
        planned_action={
            "action": "restart_container",
            "container": "checkout-service",
            "check": "crash",
            "expected_version": None,
        },
        remediation_attempts=1,
        verification_result={"outcome": "pass", "check": "crash", "action": "restart_container"},
    )
    base.update(overrides)
    return AgentState(**base)


# --- build_record: what happened, read from state ---------------------------


def test_tier1_restart_record_names_the_action_and_its_verdict():
    record = memory.build_record(_state())

    assert record["service"] == SERVICE
    assert record["status"] == "resolved"
    assert record["root_cause"]["category"] == "crash"
    assert record["tier_used"] == 1
    assert record["fix"] == {"action": "restart_container", "container": SERVICE, "rollback_target": None}
    assert record["verification"] == {"outcome": "pass", "check": "crash"}


def test_tier2_rollback_record_carries_the_rollback_target():
    state = _state(
        hypotheses=[_scored(Hypothesis(description="v18 broke it", category="deploy"), 0.9)],
        planned_action={
            "action": "rollback_deployment",
            "container": SERVICE,
            "check": "deploy",
            "expected_version": "v17",
        },
    )
    record = memory.build_record(state)

    assert record["tier_used"] == 2
    assert record["fix"]["action"] == "rollback_deployment"
    assert record["fix"]["rollback_target"] == "v17"


def test_tier3_record_points_at_the_pr():
    state = _state(
        hypotheses=[_scored(Hypothesis(description="slow query", category="slow_query"), 0.9)],
        planned_action=None,
        tier3_status="pr_opened",
        status="pr_opened",
        pr_result={"status": "opened", "url": "https://github.com/example/repo/pull/42", "branch": "x"},
        verification_result=None,
    )
    record = memory.build_record(state)

    assert record["tier_used"] == 3
    assert record["fix"] == {"pr_url": "https://github.com/example/repo/pull/42", "pr_status": "opened"}
    assert record["verification"] is None


def test_a_run_that_never_scored_or_acted_records_nulls_not_a_guess():
    state = _state(
        status="insufficient_evidence",
        hypotheses=[],
        evidence=[],
        planned_action=None,
        verification_result=None,
        escalation_reason="iteration cap reached (5/5)",
    )
    record = memory.build_record(state)

    assert record["root_cause"] is None
    assert record["tier_used"] is None
    assert record["fix"] is None
    assert record["symptoms"] == []
    assert record["escalation_reason"] == "iteration cap reached (5/5)"


def test_symptoms_are_deduplicated_compacted_and_capped():
    long_text = "x" * 500
    evidence = [{"summary": long_text}] + [{"summary": f"signal {i}"} for i in range(10)]
    symptoms = memory.build_record(_state(evidence=evidence))["symptoms"]

    assert len(symptoms) == memory.SYMPTOM_LIMIT
    assert len(symptoms[0]) == memory.SYMPTOM_CHARS
    assert len(set(symptoms)) == len(symptoms)


def test_leading_root_cause_is_the_highest_score_not_the_first_listed():
    state = _state(hypotheses=[_scored(OVERLOAD, 0.2), _scored(CRASH, 0.8)])

    assert memory.build_record(state)["root_cause"]["category"] == "crash"


# --- remember_run: write, match, note -------------------------------------


def test_a_terminal_run_writes_one_record_row(pool):
    memory.remember_run(_state())

    inserts = [s for s in pool.statements if "INSERT INTO incident_memory" in s[0]]
    assert len(inserts) == 1
    params = inserts[0][1]
    assert params["incident_id"] == INCIDENT
    assert params["service_name"] == SERVICE
    assert params["root_cause_category"] == "crash"
    assert params["status"] == "resolved"
    assert params["record"].obj["tier_used"] == 1
    assert pool.commits >= 1


def test_a_transit_status_writes_nothing(pool):
    memory.remember_run(_state(status="tier3_investigating"))

    assert pool.statements == []


def test_a_result_that_is_not_an_investigation_state_is_skipped_without_touching_the_db(pool, capsys):
    memory.remember_run({"status": "resolved"})
    memory.remember_run("not a state")

    assert pool.statements == []
    assert "[memory] no record" in capsys.readouterr().out


def test_a_dict_result_from_langgraph_is_accepted(pool):
    state = _state()
    memory.remember_run(state.model_dump())

    assert any("INSERT INTO incident_memory" in sql for sql, _ in pool.statements)


def test_a_prior_match_is_noted_before_the_record_is_written(pool):
    pool.prior_row = (
        PRIOR_ID,
        "resolved",
        {"status": "resolved", "tier_used": 1, "root_cause": {"category": "crash"}},
    )
    memory.remember_run(_state())

    kinds = [
        "audit" if "INSERT INTO audit_log" in sql else "record" if "INSERT INTO incident_memory" in sql else "read"
        for sql, _ in pool.statements
    ]
    assert kinds == ["read", "audit", "record"]

    audit_params = next(p for sql, p in pool.statements if "INSERT INTO audit_log" in sql)
    assert audit_params["node"] == "memory"
    assert audit_params["event_type"] == "prior_incident_match"
    assert audit_params["detail"].obj["matches_incident"] == PRIOR_ID
    assert audit_params["detail"].obj["supporting_context_only"] is True
    assert f"this matches incident #{PRIOR_ID}" in audit_params["reasoning_text"]


def test_the_match_is_on_service_and_category_and_excludes_this_incident(pool):
    memory.remember_run(_state())

    sql, params = next((s, p) for s, p in pool.statements if "FROM incident_memory" in s)
    assert params == {"service_name": SERVICE, "root_cause_category": "crash", "incident_id": INCIDENT}
    assert "incident_id <> %(incident_id)s" in sql


def test_no_prior_means_no_note_only_the_record(pool):
    memory.remember_run(_state())

    assert not any("INSERT INTO audit_log" in sql for sql, _ in pool.statements)


def test_a_failed_match_read_still_writes_the_record(monkeypatch):
    failing = _Pool(read_error=RuntimeError("relation incident_memory does not exist"))
    monkeypatch.setattr(persist, "_get_pool", lambda: failing)
    monkeypatch.setattr(persist, "_jsonb", lambda value: _Jsonb(value))
    persist._reported.clear()

    memory.remember_run(_state())

    assert any("INSERT INTO incident_memory" in sql for sql, _ in failing.statements)


def test_no_database_is_silent_and_does_not_raise(monkeypatch):
    monkeypatch.setattr(persist, "_get_pool", lambda: None)

    memory.remember_run(_state())


def test_the_note_does_not_change_the_run_it_describes(pool):
    """The match is supporting context: the state the run returned is the same
    object-for-object after a match as before one."""
    pool.prior_row = (PRIOR_ID, "resolved", {"status": "resolved", "tier_used": 1, "root_cause": {"category": "crash"}})
    state = _state()
    before = state.model_dump()

    memory.remember_run(state)

    assert state.model_dump() == before


def test_a_repeat_write_for_the_same_incident_is_a_no_op_in_sql():
    assert "ON CONFLICT (incident_id) DO NOTHING" in memory.INSERT_RECORD


# --- migration: grants match the role model ---------------------------------


def _migration() -> str:
    return (INIT_DIR / "008_incident_memory.sql").read_text()


def test_migration_008_creates_the_table_keyed_on_incident():
    sql = _migration()

    assert "CREATE TABLE incident_memory" in sql
    assert "incident_id BIGINT PRIMARY KEY REFERENCES incidents(id)" in sql


def test_migration_008_grants_the_agent_role_insert_and_select_only():
    sql = _migration()

    assert "GRANT SELECT, INSERT ON incident_memory TO phoenix_app;" in sql
    assert "UPDATE" not in sql.split("GRANT")[1].split(";")[0]
    assert "DELETE" not in sql.split("GRANT")[1].split(";")[0]

