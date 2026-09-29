"""Unit tests for the durable trail — no database, no network, no LLM.

The driver is not installed here, which is the same fact persist.py is built to
survive, so these tests put a stub where the module imports psycopg from: a
ConnectionPool whose connection() is a context manager, a connection with
execute and commit, and a Jsonb that carries the value it wrapped. The stub
records every statement and parameter the module hands the driver, which is the
only thing worth asserting about a write that cannot happen: the SQL, the
values, and the order they arrive in.

The second half of the file is about the wiring, because a trail nothing writes
is not a trail. The observer, the diagnoser and the router are driven with
persistence replaced by a recorder, so what each call site passes is asserted
directly, and one test drives the compiled graph against the stub driver to
prove the order a real run's trail arrives in.

The last two tests read 002 and 004 off disk and compare them to the tool names
the observer dispatches. The evidence.source CHECK rejects every one of those
names until 004 has been applied to the live database, and that is invisible
from Python; asserting it here is the closest thing to checking the migration
without a database to check it against.
"""

import ast
import inspect
import os
import re
import sys
import types
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

import pytest
from langgraph.graph import END

from phoenix.graph import graph, nodes, persist
from phoenix.graph.llm_client import HypothesisDecision, ToolCallDecision
from phoenix.graph.schemas import DiagnoserOutput, Hypothesis, ScoredHypothesis
from phoenix.graph.state import AgentState

SERVICE = "checkout-service"
INCIDENT = 7

INIT_DIR = Path(__file__).resolve().parents[1] / "db" / "init"

SERVICE_DOWN = {"status": "success", "text": "ServiceDown firing, up==0"}
PANIC = {"status": "success", "text": "panic: index out of range, process exit 1"}

CRASH = Hypothesis(description="checkout-service is crash looping", category="crash")
DEPLOY = Hypothesis(description="the v18 rollout broke checkout-service", category="deploy")

BREAKDOWN = {
    "has_prometheus_signal": 1,
    "has_loki_signal": 1,
    "has_docker_signal": 0,
    "has_health_signal": 0,
    "has_deploy_signal": 0,
    "sources_supporting": 2,
    "agreement_bonus": 0.2,
    "contradiction_penalty": 0.0,
    "category": "crash",
}

SCORED_CRASH = ScoredHypothesis(
    hypothesis=CRASH, score=0.9, score_breakdown=dict(BREAKDOWN)
)
SCORED_DEPLOY = ScoredHypothesis(
    hypothesis=DEPLOY, score=0.0, score_breakdown={**BREAKDOWN, "category": "deploy"}
)

EVIDENCE_ITEM = {
    "iteration": 1,
    "source": "query_prometheus",
    "collected_at": "2026-01-01T00:00:00+00:00",
    "summary": "query_prometheus({'promql': 'up == 0'})",
    "raw_data": SERVICE_DOWN,
}

AUDIT_DETAIL = {"iteration": 1, "confidence": 0.9, "escalation_reason": None}


class StubJsonb:
    """Stands in for psycopg's Jsonb: the value, and the fact it was wrapped.

    Exposes .obj because the real adapter does, so a test written against this
    is a test written against the driver's own contract.
    """

    def __init__(self, obj):
        self.obj = obj


class StubConnection:
    def __init__(self, recorder: "Recorder"):
        self._recorder = recorder

    def execute(self, sql, params):
        self._recorder.statements.append((sql, params))
        if self._recorder.execute_error is not None:
            raise self._recorder.execute_error

    def commit(self):
        self._recorder.commits += 1


class StubPool:
    def __init__(self, recorder: "Recorder", conninfo, kwargs):
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.opened = False
        self.closed = False
        self._recorder = recorder

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    @contextmanager
    def connection(self):
        if self._recorder.connection_error is not None:
            raise self._recorder.connection_error
        yield StubConnection(self._recorder)


class Recorder:
    """Everything the module asked the driver to do, in the order it asked."""

    def __init__(self, connection_error=None, execute_error=None):
        self.statements: list[tuple[str, dict]] = []
        self.commits = 0
        self.pools: list[StubPool] = []
        self.connection_error = connection_error
        self.execute_error = execute_error

    @property
    def sql(self) -> list[str]:
        return [sql for sql, _ in self.statements]

    @property
    def params(self) -> list[dict]:
        return [params for _, params in self.statements]

    @property
    def tables(self) -> list[str]:
        return [sql.split()[2] for sql in self.sql]


@pytest.fixture(autouse=True)
def clean_persist(monkeypatch):
    """Forget the pool, the memoised failure and the causes already reported.

    persist memoises all three for the life of the process, because a one-shot
    run should name each problem once. A test session is not a one-shot run, so
    every test starts from the state a first-ever write would see.
    """
    monkeypatch.setattr(persist, "_pool", None)
    monkeypatch.setattr(persist, "_pool_error", None)
    monkeypatch.setattr(persist, "_reported", set())
    monkeypatch.delenv(persist.DATABASE_URL_ENV, raising=False)


def _stub_driver(monkeypatch, *, connection_error=None, execute_error=None) -> Recorder:
    """Put a fake psycopg where persist.py imports it, and record its calls."""
    recorder = Recorder(connection_error, execute_error)

    def make_pool(conninfo, **kwargs):
        if connection_error is not None:
            raise connection_error
        pool = StubPool(recorder, conninfo, kwargs)
        recorder.pools.append(pool)
        return pool

    pool_module = types.ModuleType("psycopg_pool")
    pool_module.ConnectionPool = make_pool
    json_module = types.ModuleType("psycopg.types.json")
    json_module.Jsonb = StubJsonb

    for name, module in (
        ("psycopg", types.ModuleType("psycopg")),
        ("psycopg.types", types.ModuleType("psycopg.types")),
        ("psycopg.types.json", json_module),
        ("psycopg_pool", pool_module),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    return recorder


def _live(monkeypatch, *, connection_error=None, execute_error=None) -> Recorder:
    """A stub driver behind a DATABASE_URL, so writes actually reach it."""
    recorder = _stub_driver(
        monkeypatch, connection_error=connection_error, execute_error=execute_error
    )
    monkeypatch.setenv(
        persist.DATABASE_URL_ENV, "postgresql://phoenix_app:secret@localhost:5432/phoenix"
    )
    return recorder


def _allowed_sources(migration: str) -> set[str]:
    """The source names a migration's CHECK constraint accepts."""
    check = (INIT_DIR / migration).read_text(encoding="utf-8").split("CHECK", 1)[1]
    return set(re.findall(r"'([a-z_]+)'", check))


def _evidence_spy(monkeypatch) -> list[tuple[int, dict]]:
    """Replace the observer's evidence writer, keeping its real signature."""
    calls: list[tuple[int, dict]] = []

    def record_evidence(incident_id, evidence_item):
        calls.append((incident_id, evidence_item))

    monkeypatch.setattr(nodes, "record_evidence", record_evidence)
    return calls


def _hypotheses_spy(monkeypatch) -> list[dict]:
    """Replace the diagnoser's hypothesis writer, keeping its real signature."""
    calls: list[dict] = []

    def record_hypotheses(incident_id, scored_hypotheses, *, iteration=1):
        calls.append(
            {
                "incident_id": incident_id,
                "scored_hypotheses": scored_hypotheses,
                "iteration": iteration,
            }
        )

    monkeypatch.setattr(nodes, "record_hypotheses", record_hypotheses)
    return calls


def _audit_spy(monkeypatch, module) -> list[dict]:
    """Replace one module's audit writer, keeping its real signature."""
    calls: list[dict] = []

    def record_audit(incident_id, node, event_type, detail, reasoning_text):
        calls.append(
            {
                "incident_id": incident_id,
                "node": node,
                "event_type": event_type,
                "detail": detail,
                "reasoning_text": reasoning_text,
            }
        )

    monkeypatch.setattr(module, "record_audit", record_audit)
    return calls


def _tool_calls(requested, tokens: int = 0):
    return lambda service_name, evidence_so_far, evidence_requests: ToolCallDecision(
        requested, tokens
    )


def _hypotheses(proposed, tokens: int = 0):
    return lambda service_name, evidence_so_far: HypothesisDecision(
        DiagnoserOutput.model_construct(hypotheses=proposed), tokens
    )


def _state(**overrides) -> AgentState:
    return AgentState(incident_id=INCIDENT, service_name=SERVICE, **overrides)


def test_the_module_never_imports_the_api_and_shares_nothing_with_it():
    imported = {
        alias.name
        for node in ast.walk(ast.parse(inspect.getsource(persist)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not {name for name in imported if name.startswith("phoenix.api")}
    assert "db" not in imported
    assert not [name for name in sys.modules if name.startswith("phoenix.api")]


def test_the_evidence_row_is_filled_from_the_item_the_observer_appended(monkeypatch):
    recorder = _live(monkeypatch)

    persist.record_evidence(INCIDENT, EVIDENCE_ITEM)

    assert len(recorder.statements) == 1
    sql, params = recorder.statements[0]
    assert sql == persist.INSERT_EVIDENCE
    assert set(params) == {
        "incident_id",
        "source",
        "iteration",
        "collected_at",
        "summary",
        "raw_data",
    }
    assert params["incident_id"] == INCIDENT
    assert params["source"] == "query_prometheus"
    assert params["iteration"] == 1
    assert params["summary"] == EVIDENCE_ITEM["summary"]
    assert params["raw_data"].obj == SERVICE_DOWN
    assert recorder.commits == 1


def test_collected_at_reaches_the_timestamp_column_as_a_datetime(monkeypatch):
    recorder = _live(monkeypatch)

    persist.record_evidence(INCIDENT, EVIDENCE_ITEM)

    collected_at = recorder.params[0]["collected_at"]
    assert isinstance(collected_at, datetime)
    assert collected_at == datetime.fromisoformat(EVIDENCE_ITEM["collected_at"])
    assert collected_at.utcoffset().total_seconds() == 0


def test_a_scored_hypothesis_becomes_one_row_read_from_attributes_not_a_model_dump(monkeypatch):
    recorder = _live(monkeypatch)

    persist.record_hypotheses(INCIDENT, [SCORED_CRASH], iteration=2)

    sql, params = recorder.statements[0]
    assert sql == persist.INSERT_HYPOTHESIS
    assert set(params) == {
        "incident_id",
        "iteration",
        "description",
        "score",
        "score_breakdown",
    }
    assert params["description"] == CRASH.description
    assert isinstance(params["description"], str)
    assert params["score"] == 0.9
    assert params["score_breakdown"].obj == BREAKDOWN
    assert params["iteration"] == 2


def test_no_category_column_is_invented_for_a_table_that_has_none(monkeypatch):
    recorder = _live(monkeypatch)

    persist.record_hypotheses(INCIDENT, [SCORED_CRASH])

    assert "category" not in recorder.sql[0].upper().replace("SCORE_BREAKDOWN", "")
    assert "category" not in recorder.params[0]


def test_the_ranking_is_written_one_row_each_in_the_order_scoring_ranked_it(monkeypatch):
    recorder = _live(monkeypatch)

    persist.record_hypotheses(INCIDENT, [SCORED_CRASH, SCORED_DEPLOY], iteration=1)

    assert [params["description"] for params in recorder.params] == [
        CRASH.description,
        DEPLOY.description,
    ]
    assert [params["score"] for params in recorder.params] == [0.9, 0.0]


def test_an_empty_ranking_writes_nothing_at_all(monkeypatch):
    recorder = _live(monkeypatch)

    persist.record_hypotheses(INCIDENT, [])

    assert recorder.statements == []
    assert recorder.pools == []


def test_the_audit_row_is_written_with_the_detail_it_was_handed_unchanged(monkeypatch):
    recorder = _live(monkeypatch)

    persist.record_audit(INCIDENT, "router", "threshold_reached", AUDIT_DETAIL, "0.90 >= 0.75")

    sql, params = recorder.statements[0]
    assert sql == persist.INSERT_AUDIT
    assert set(params) == {
        "incident_id",
        "node",
        "event_type",
        "detail",
        "reasoning_text",
    }
    assert params["node"] == "router"
    assert params["event_type"] == "threshold_reached"
    assert params["detail"].obj == AUDIT_DETAIL
    assert params["reasoning_text"] == "0.90 >= 0.75"


def test_an_audit_row_never_states_its_own_timestamp(monkeypatch):
    recorder = _live(monkeypatch)

    persist.record_audit(INCIDENT, "diagnoser", "diagnosis", AUDIT_DETAIL, None)

    assert "created_at" not in recorder.sql[0]
    assert "created_at" not in recorder.params[0]


def test_a_null_reasoning_is_written_as_null_rather_than_as_an_empty_string(monkeypatch):
    recorder = _live(monkeypatch)

    persist.record_audit(INCIDENT, "observer", "observer_pass", {}, None)

    assert recorder.params[0]["reasoning_text"] is None


def test_no_value_is_ever_formatted_into_the_statement_text(monkeypatch):
    recorder = _live(monkeypatch)
    hostile = "'; DROP TABLE evidence; --"
    item = {
        **EVIDENCE_ITEM,
        "source": "inspect_health",
        "summary": f"inspect_health({hostile})",
        "raw_data": {"status": "success", "text": hostile},
    }
    hypothesis = Hypothesis(description=hostile, category="unknown")
    scored = ScoredHypothesis(hypothesis=hypothesis, score=0.1, score_breakdown={"n": hostile})

    persist.record_evidence(INCIDENT, item)
    persist.record_hypotheses(INCIDENT, [scored], iteration=1)
    persist.record_audit(INCIDENT, "observer", "observer_pass", {"n": hostile}, hostile)

    for sql, params in recorder.statements:
        assert hostile not in sql
        values = sql.split("VALUES", 1)[1]
        assert re.fullmatch(r"[\s(),%a-z0-9_]*", values)
        assert set(re.findall(r"%\((\w+)\)s", values)) == set(params)
    assert recorder.sql == [
        persist.INSERT_EVIDENCE,
        persist.INSERT_HYPOTHESIS,
        persist.INSERT_AUDIT,
    ]
    assert hostile in [recorder.params[0]["summary"], recorder.params[2]["reasoning_text"]]
    assert hostile in recorder.params[1]["score_breakdown"].obj.values()


def test_every_statement_the_module_holds_is_an_insert(monkeypatch):
    for sql in (persist.INSERT_EVIDENCE, persist.INSERT_HYPOTHESIS, persist.INSERT_AUDIT):
        assert sql.strip().upper().startswith("INSERT INTO")
        for forbidden in ("UPDATE", "DELETE", "TRUNCATE", "DROP", "ON CONFLICT"):
            assert forbidden not in sql.upper()


def test_every_statement_a_whole_run_issues_is_an_insert(monkeypatch):
    recorder = _live(monkeypatch)
    monkeypatch.setattr(
        nodes,
        "decide_tool_calls",
        _tool_calls([{"name": "query_prometheus", "arguments": {"promql": "up == 0"}}], 940),
    )
    monkeypatch.setattr(nodes, "decide_hypotheses", _hypotheses([CRASH], 1350))
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: SERVICE_DOWN)

    graph.build_graph().invoke(_state())

    assert recorder.tables
    assert set(recorder.tables) == {"evidence", "hypotheses", "audit_log"}
    for sql in recorder.sql:
        assert sql.strip().upper().startswith("INSERT INTO")


def test_the_evidence_source_the_module_writes_is_a_name_the_widened_check_allows():
    assert set(nodes.TOOL_DISPATCH) <= _allowed_sources("004_evidence_source_widen.sql")


def test_the_widening_is_load_bearing_because_the_original_check_rejects_every_tool_name():
    original = _allowed_sources("002_evidence_hypotheses_audit.sql")

    assert original == {"prometheus", "loki", "docker"}
    assert not set(nodes.TOOL_DISPATCH) & original


def test_the_evidence_source_is_the_tool_name_the_observer_recorded(monkeypatch):
    recorder = _live(monkeypatch)

    for tool in nodes.TOOL_DISPATCH:
        persist.record_evidence(INCIDENT, {**EVIDENCE_ITEM, "source": tool})

    assert [params["source"] for params in recorder.params] == list(nodes.TOOL_DISPATCH)


def test_the_pool_is_opened_lazily_shared_by_every_write_and_bounded_when_it_waits(monkeypatch):
    recorder = _live(monkeypatch)

    assert recorder.pools == []
    persist.record_evidence(INCIDENT, EVIDENCE_ITEM)
    persist.record_audit(INCIDENT, "observer", "observer_pass", {}, None)
    persist.record_hypotheses(INCIDENT, [SCORED_CRASH])

    assert len(recorder.pools) == 1
    pool = recorder.pools[0]
    assert pool.opened is True
    assert pool.conninfo.endswith("/phoenix")
    assert pool.kwargs["open"] is False
    assert pool.kwargs["min_size"] == persist.POOL_MIN_SIZE
    assert pool.kwargs["max_size"] == persist.POOL_MAX_SIZE
    assert pool.kwargs["timeout"] == persist.POOL_WAIT_SECONDS
    assert persist.POOL_WAIT_SECONDS < 30.0


def test_closing_persistence_closes_the_pool_it_opened(monkeypatch):
    recorder = _live(monkeypatch)
    persist.record_audit(INCIDENT, "observer", "observer_pass", {}, None)
    pool = recorder.pools[0]

    persist.close_persistence()

    assert pool.closed is True
    assert persist._pool is None


def test_closing_persistence_with_nothing_opened_is_harmless(monkeypatch):
    _live(monkeypatch)

    persist.close_persistence()

    assert persist._pool is None


def test_every_writer_returns_when_there_is_no_database_url(monkeypatch, capsys):
    recorder = _stub_driver(monkeypatch)

    assert persist.record_evidence(INCIDENT, EVIDENCE_ITEM) is None
    assert persist.record_hypotheses(INCIDENT, [SCORED_CRASH]) is None
    assert persist.record_audit(INCIDENT, "observer", "observer_pass", {}, None) is None

    printed = capsys.readouterr().out
    assert printed.count("DATABASE_URL is not set") == 1
    assert "leaves no trail" in printed
    assert recorder.pools == []
    assert recorder.statements == []


def test_every_writer_returns_when_the_pool_cannot_be_built(monkeypatch, capsys):
    _live(monkeypatch, connection_error=OSError("connection refused"))

    assert persist.record_evidence(INCIDENT, EVIDENCE_ITEM) is None
    assert persist.record_hypotheses(INCIDENT, [SCORED_CRASH]) is None
    assert persist.record_audit(INCIDENT, "observer", "observer_pass", {}, None) is None

    printed = capsys.readouterr().out
    assert printed.count("no connection pool available") == 1
    assert "connection refused" in printed


def test_a_connection_the_pool_cannot_hand_out_does_not_take_the_run_down(monkeypatch, capsys):
    _live(monkeypatch, execute_error=TimeoutError("timed out waiting for a connection"))

    assert persist.record_evidence(INCIDENT, EVIDENCE_ITEM) is None
    assert persist.record_audit(INCIDENT, "observer", "observer_pass", {}, None) is None

    assert "timed out waiting for a connection" in capsys.readouterr().out


def test_a_database_that_refuses_a_row_is_reported_once_not_once_per_row(monkeypatch, capsys):
    recorder = _live(monkeypatch, execute_error=RuntimeError("CHECK constraint failed"))

    for _ in range(5):
        assert persist.record_evidence(INCIDENT, EVIDENCE_ITEM) is None

    printed = capsys.readouterr().out
    assert len(recorder.statements) == 5
    assert printed.count("CHECK constraint failed") == 1
    assert "evidence row from 'query_prometheus' not written" in printed


def test_one_refused_hypothesis_row_does_not_abandon_the_rest_of_the_ranking(monkeypatch):
    recorder = _live(monkeypatch, execute_error=RuntimeError("CHECK constraint failed"))

    persist.record_hypotheses(INCIDENT, [SCORED_CRASH, SCORED_DEPLOY])

    assert [params["description"] for params in recorder.params] == [
        CRASH.description,
        DEPLOY.description,
    ]


def test_a_write_with_nowhere_to_go_never_reaches_for_the_driver_adapter(monkeypatch, capsys):
    def driver_absent(value):
        raise ImportError("No module named 'psycopg.types.json'")

    monkeypatch.setattr(persist, "_jsonb", driver_absent)

    assert persist.record_evidence(INCIDENT, EVIDENCE_ITEM) is None
    assert persist.record_hypotheses(INCIDENT, [SCORED_CRASH]) is None
    assert persist.record_audit(INCIDENT, "observer", "observer_pass", {}, None) is None

    printed = capsys.readouterr().out
    assert "DATABASE_URL is not set" in printed
    assert "psycopg" not in printed


def test_a_pool_that_opens_but_will_not_connect_still_names_a_cause(monkeypatch, capsys):
    recorder = _stub_driver(monkeypatch)
    monkeypatch.setenv(persist.DATABASE_URL_ENV, "postgresql://phoenix_app:x@localhost:1/phoenix")
    persist.record_audit(INCIDENT, "observer", "observer_pass", {}, None)

    recorder.pools[0]._recorder.connection_error = OSError("could not connect to server")
    assert persist.record_audit(INCIDENT, "router", "continuing", {}, None) is None

    assert "could not connect to server" in capsys.readouterr().out


def test_the_observer_writes_one_evidence_row_per_tool_call_it_ran(monkeypatch):
    written = _evidence_spy(monkeypatch)
    _audit_spy(monkeypatch, nodes)
    monkeypatch.setattr(
        nodes,
        "decide_tool_calls",
        _tool_calls(
            [
                {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
                {"name": "query_loki", "arguments": {"logql": '{container="x"}'}},
            ],
            940,
        ),
    )
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: SERVICE_DOWN)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: PANIC)

    state = nodes.observer_node(_state())

    assert [incident_id for incident_id, _ in written] == [INCIDENT, INCIDENT]
    assert [item["source"] for _, item in written] == ["query_prometheus", "query_loki"]
    assert written[0][1] is state.evidence[0]
    assert written[1][1] is state.evidence[1]


def test_the_observer_audits_the_pass_with_what_was_asked_and_what_ran(monkeypatch):
    _evidence_spy(monkeypatch)
    audit = _audit_spy(monkeypatch, nodes)
    monkeypatch.setattr(
        nodes,
        "decide_tool_calls",
        _tool_calls(
            [
                {"name": "restart_service", "arguments": {"service_name": SERVICE}},
                {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
            ],
            940,
        ),
    )
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: SERVICE_DOWN)

    state = nodes.observer_node(_state())

    assert len(audit) == 1
    assert audit[0]["incident_id"] == INCIDENT
    assert audit[0]["node"] == "observer"
    assert audit[0]["event_type"] == "observer_pass"
    assert audit[0]["detail"] == {
        "iteration": 1,
        "requested_tools": ["restart_service", "query_prometheus"],
        "dispatched_tools": ["query_prometheus"],
        "evidence_collected": 1,
        "tokens_spent": 940,
    }
    assert audit[0]["reasoning_text"] is None
    assert state.tokens_spent == 940


def test_an_observer_pass_that_asked_for_nothing_is_still_audited(monkeypatch):
    written = _evidence_spy(monkeypatch)
    audit = _audit_spy(monkeypatch, nodes)
    monkeypatch.setattr(nodes, "decide_tool_calls", _tool_calls([], 940))

    nodes.observer_node(_state())

    assert written == []
    assert audit[0]["detail"]["requested_tools"] == []
    assert audit[0]["detail"]["dispatched_tools"] == []
    assert audit[0]["detail"]["evidence_collected"] == 0


def test_the_diagnoser_writes_its_whole_ranking_stamped_with_the_pass_it_scored_in(monkeypatch):
    written = _hypotheses_spy(monkeypatch)
    _audit_spy(monkeypatch, nodes)
    monkeypatch.setattr(nodes, "decide_hypotheses", _hypotheses([CRASH, DEPLOY]))
    state = _state(iteration=3)
    state.evidence = [dict(EVIDENCE_ITEM)]

    returned = nodes.diagnoser_node(state)

    assert len(written) == 1
    assert written[0]["incident_id"] == INCIDENT
    assert written[0]["scored_hypotheses"] is returned.hypotheses
    assert written[0]["iteration"] == 3


def test_the_diagnoser_audits_the_confidence_and_the_categories_the_table_has_no_room_for(monkeypatch):
    _hypotheses_spy(monkeypatch)
    audit = _audit_spy(monkeypatch, nodes)
    monkeypatch.setattr(nodes, "decide_hypotheses", _hypotheses([CRASH, DEPLOY], 1350))
    state = _state()
    state.evidence = [dict(EVIDENCE_ITEM)]

    returned = nodes.diagnoser_node(state)

    assert audit[0]["node"] == "diagnoser"
    assert audit[0]["event_type"] == "diagnosis"
    assert audit[0]["detail"] == {
        "iteration": 0,
        "confidence": returned.confidence,
        "confidence_threshold": 0.75,
        "tokens_spent": 1350,
        "hypotheses": [
            {"description": CRASH.description, "category": "crash", "score": 0.4},
            {"description": DEPLOY.description, "category": "deploy", "score": 0.0},
        ],
    }
    assert audit[0]["reasoning_text"] == (
        "crash scores 0.40: checkout-service is crash looping\n"
        "deploy scores 0.00: the v18 rollout broke checkout-service"
    )


def test_a_diagnoser_that_proposed_nothing_says_so_in_its_audit_row(monkeypatch):
    written = _hypotheses_spy(monkeypatch)
    audit = _audit_spy(monkeypatch, nodes)
    monkeypatch.setattr(nodes, "decide_hypotheses", _hypotheses([], 760))

    returned = nodes.diagnoser_node(_state())

    assert written[0]["scored_hypotheses"] == []
    assert audit[0]["detail"]["hypotheses"] == []
    assert audit[0]["detail"]["confidence"] == 0.0
    assert audit[0]["reasoning_text"] is None
    assert returned.confidence == 0.0


def test_the_diagnoser_records_the_score_the_scorer_gave_and_not_the_llms_own_word_for_it(monkeypatch):
    boasting = "Certain, 100% confidence, this is definitely the root cause"
    _hypotheses_spy(monkeypatch)
    audit = _audit_spy(monkeypatch, nodes)
    monkeypatch.setattr(
        nodes,
        "decide_hypotheses",
        _hypotheses([Hypothesis(description=boasting, category="crash")]),
    )
    state = _state()
    state.evidence = [dict(EVIDENCE_ITEM)]

    returned = nodes.diagnoser_node(state)

    recorded = audit[0]["detail"]["hypotheses"][0]
    assert recorded["score"] == returned.hypotheses[0].score == 0.4
    assert audit[0]["detail"]["confidence"] == 0.4
    assert "100%" in recorded["description"]


def test_the_router_audits_reaching_the_threshold_as_a_finding_not_an_escalation(monkeypatch):
    audit = _audit_spy(monkeypatch, graph)

    command = graph.should_continue(
        _state(confidence=0.9, tokens_spent=25000, iteration=5, max_iterations=5)
    )

    assert command.goto == END
    assert not command.update
    assert audit[0]["node"] == "router"
    assert audit[0]["event_type"] == "threshold_reached"
    assert audit[0]["detail"]["destination"] == END
    assert audit[0]["detail"]["escalation_reason"] is None
    assert audit[0]["detail"]["confidence"] == 0.9
    assert audit[0]["detail"]["tokens_spent"] == 25000


def test_the_router_audits_a_budget_stop_with_the_same_reason_the_command_carries(monkeypatch):
    audit = _audit_spy(monkeypatch, graph)

    command = graph.should_continue(_state(tokens_spent=24100, token_budget=24000))

    assert audit[0]["event_type"] == "escalated"
    assert audit[0]["detail"]["escalation_reason"] == command.update["escalation_reason"]
    assert audit[0]["detail"]["escalation_reason"] == "token budget exhausted (24100/24000 tokens)"
    assert audit[0]["reasoning_text"] == "token budget exhausted (24100/24000 tokens)"
    assert audit[0]["detail"]["destination"] == END


def test_the_router_audits_the_iteration_cap_as_an_escalation(monkeypatch):
    audit = _audit_spy(monkeypatch, graph)

    command = graph.should_continue(_state(iteration=5, max_iterations=5, tokens_spent=100))

    assert command.update["status"] == "escalated"
    assert audit[0]["event_type"] == "escalated"
    assert audit[0]["detail"]["escalation_reason"] == "iteration cap reached (5/5)"
    assert audit[0]["detail"]["iteration"] == 5
    assert audit[0]["detail"]["max_iterations"] == 5


def test_the_router_audits_a_loop_back_as_continuing_and_not_as_a_stop(monkeypatch):
    audit = _audit_spy(monkeypatch, graph)

    command = graph.should_continue(_state(confidence=0.2, tokens_spent=100))

    assert command.goto == "observer"
    assert not command.update
    assert audit[0]["event_type"] == "continuing"
    assert audit[0]["detail"]["destination"] == "observer"
    assert audit[0]["detail"]["escalation_reason"] is None


def test_the_router_audits_every_pass_so_a_run_shows_how_it_got_where_it_stopped(monkeypatch):
    audit = _audit_spy(monkeypatch, graph)

    graph.should_continue(_state(confidence=0.2))
    graph.should_continue(_state(confidence=0.2, tokens_spent=30000))

    assert [call["event_type"] for call in audit] == ["continuing", "escalated"]


def test_auditing_a_decision_does_not_change_the_command_the_router_returns(monkeypatch):
    _audit_spy(monkeypatch, graph)
    state = _state(confidence=0.2, tokens_spent=24100, token_budget=24000)

    command = graph.should_continue(state)

    assert command.goto == END
    assert command.update == {
        "status": "escalated",
        "escalation_reason": "token budget exhausted (24100/24000 tokens)",
    }
    assert state.status == "investigating"


def test_auditing_never_asks_the_llm_anything(monkeypatch):
    asked = []

    def boom(*args, **kwargs):
        asked.append(args)
        raise AssertionError("persistence consulted the LLM")

    audit = _audit_spy(monkeypatch, graph)
    monkeypatch.setattr(nodes, "decide_tool_calls", boom)
    monkeypatch.setattr(nodes, "decide_hypotheses", boom)

    graph.should_continue(_state(confidence=0.9, tokens_spent=30000))

    assert asked == []
    assert audit[0]["event_type"] == "threshold_reached"


def test_a_whole_run_writes_its_trail_in_the_order_the_nodes_happened(monkeypatch):
    recorder = _live(monkeypatch)
    monkeypatch.setattr(
        nodes,
        "decide_tool_calls",
        _tool_calls(
            [
                {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
                {"name": "query_loki", "arguments": {"logql": '{container="x"}'}},
            ],
            940,
        ),
    )
    monkeypatch.setattr(nodes, "decide_hypotheses", _hypotheses([CRASH], 1350))
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: SERVICE_DOWN)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: PANIC)

    graph.build_graph().invoke(_state())

    assert [
        (table, params.get("event_type")) for table, params in zip(recorder.tables, recorder.params)
    ] == [
        ("evidence", None),
        ("evidence", None),
        ("audit_log", "observer_pass"),
        ("hypotheses", None),
        ("audit_log", "diagnosis"),
        ("audit_log", "threshold_reached"),
    ]
    assert {params["incident_id"] for params in recorder.params} == {INCIDENT}
    assert recorder.params[-1]["detail"].obj["destination"] == END
    assert recorder.params[-1]["detail"].obj["escalation_reason"] is None
    assert recorder.commits == 6


def test_a_run_with_no_database_still_produces_its_diagnosis(monkeypatch):
    recorder = _stub_driver(monkeypatch)
    monkeypatch.setattr(
        nodes,
        "decide_tool_calls",
        _tool_calls(
            [
                {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
                {"name": "query_loki", "arguments": {"logql": '{container="x"}'}},
            ],
            940,
        ),
    )
    monkeypatch.setattr(nodes, "decide_hypotheses", _hypotheses([CRASH], 1350))
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: SERVICE_DOWN)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: PANIC)

    final = graph.build_graph().invoke(_state())

    assert final["confidence"] == 0.9
    assert final["status"] == "investigating"
    assert final.get("escalation_reason") is None
    assert len(final["evidence"]) == 2
    assert recorder.pools == []


def test_a_database_that_refuses_every_row_costs_a_run_nothing_but_its_trail(monkeypatch):
    def _forget_the_pool():
        monkeypatch.setattr(persist, "_pool", None)
        monkeypatch.setattr(persist, "_pool_error", None)
        monkeypatch.setattr(persist, "_reported", set())

    outcomes = []
    for refusing in (True, False):
        _forget_the_pool()
        recorder = _live(
            monkeypatch,
            execute_error=RuntimeError("CHECK constraint failed") if refusing else None,
        )
        monkeypatch.setattr(
            nodes,
            "decide_tool_calls",
            _tool_calls(
                [
                    {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
                    {"name": "query_loki", "arguments": {"logql": '{container="x"}'}},
                ],
                940,
            ),
        )
        monkeypatch.setattr(nodes, "decide_hypotheses", _hypotheses([CRASH], 1350))
        monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: SERVICE_DOWN)
        monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: PANIC)

        final = graph.build_graph().invoke(_state())
        outcomes.append((len(recorder.statements), final))

    refused_count, refused = outcomes[0]
    written_count, written = outcomes[1]

    assert refused_count == written_count == 6
    assert refused["confidence"] == written["confidence"] == 0.9
    assert refused["status"] == written["status"] == "investigating"
    assert refused["hypotheses"] == written["hypotheses"]
    assert len(refused["evidence"]) == len(written["evidence"]) == 2


def test_an_escalated_run_records_its_reason_on_the_trail_and_in_the_final_state(monkeypatch):
    recorder = _live(monkeypatch)
    monkeypatch.setattr(nodes, "decide_tool_calls", _tool_calls([], 1))
    monkeypatch.setattr(nodes, "decide_hypotheses", _hypotheses([], 1))

    final = graph.build_graph().invoke(_state(token_budget=2, max_iterations=5))

    assert final["escalation_reason"] == "token budget exhausted (2/2 tokens)"
    assert recorder.params[-1]["event_type"] == "escalated"
    assert (
        recorder.params[-1]["detail"].obj["escalation_reason"] == final["escalation_reason"]
    )


def test_a_second_iteration_is_recorded_under_its_own_number(monkeypatch):
    recorder = _live(monkeypatch)
    passes = {"n": 0}

    def fake_tool_calls(service_name, evidence_so_far, evidence_requests):
        passes["n"] += 1
        if passes["n"] == 1:
            return ToolCallDecision([], 940)
        return ToolCallDecision(
            [
                {"name": "query_prometheus", "arguments": {"promql": "up == 0"}},
                {"name": "query_loki", "arguments": {"logql": '{container="x"}'}},
            ],
            940,
        )

    monkeypatch.setattr(nodes, "decide_tool_calls", fake_tool_calls)
    monkeypatch.setattr(nodes, "decide_hypotheses", _hypotheses([CRASH], 1350))
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_prometheus", lambda args: SERVICE_DOWN)
    monkeypatch.setitem(nodes.TOOL_DISPATCH, "query_loki", lambda args: PANIC)

    graph.build_graph().invoke(_state(token_budget=100000, max_iterations=5))

    hypothesis_rows = [params for params in recorder.params if "score" in params]
    assert [params["iteration"] for params in hypothesis_rows] == [1, 2]
    assert [params["score"] for params in hypothesis_rows] == [0.0, 0.9]
    assert {params["iteration"] for params in recorder.params if "source" in params} == {2}
    assert recorder.params[-1]["event_type"] == "threshold_reached"


def test_the_router_row_names_the_state_it_decided_on_and_nothing_else(monkeypatch):
    audit = _audit_spy(monkeypatch, graph)

    graph.should_continue(_state(confidence=0.9))

    assert set(audit[0]["detail"]) == {
        "iteration",
        "max_iterations",
        "confidence",
        "confidence_threshold",
        "tokens_spent",
        "token_budget",
        "destination",
        "escalation_reason",
    }
