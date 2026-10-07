"""The Tier 3 investigator is told where the service's code lives.

It used to search for the deployment marker, which appears only in chaos tooling and
deployment records, and guess directories that do not exist. The service's source
directory and the repository layout are now handed to it, and nothing here is specific
to any one service or defect."""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from types import SimpleNamespace

from langgraph.graph import END

import phoenix.graph.llm_client as llm_client
import phoenix.graph.tier3_nodes as tier3_nodes
from phoenix.graph.llm_client import DefectDecision, ToolCallDecision
from phoenix.graph.schemas import CodeDefect, Hypothesis
from phoenix.graph.state import AgentState

CONTEXT = {
    "top_level": ["chaos/", "deployments/", "phoenix/", "services/", "README.md"],
    "service_dir": "services/payment-service",
    "service_files": ["services/payment-service/Dockerfile", "services/payment-service/app.py"],
    "service_files_truncated": False,
}
NO_SERVICE_DIR = {**CONTEXT, "service_dir": None, "service_files": []}


def _recorder(monkeypatch) -> list[dict]:
    recorded: list[dict] = []

    def fake_create(**kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None, content=""))],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=1, total_tokens=5),
        )

    monkeypatch.setattr(
        llm_client, "client", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    )
    return recorded


def _system(recorded) -> str:
    return recorded[0]["messages"][0]["content"]


def test_the_prompt_names_the_services_source_directory_and_its_files(monkeypatch):
    recorded = _recorder(monkeypatch)

    llm_client.decide_code_investigation_calls("payment-service", "payment is slow", [], CONTEXT)

    system = _system(recorded)
    assert "services/payment-service" in system
    assert "services/payment-service/app.py" in system


def test_the_prompt_shows_the_top_level_layout(monkeypatch):
    recorded = _recorder(monkeypatch)

    llm_client.decide_code_investigation_calls("payment-service", "payment is slow", [], CONTEXT)

    system = _system(recorded)
    assert "services/" in system and "phoenix/" in system and "chaos/" in system


def test_a_service_with_no_directory_is_told_so_and_still_gets_the_layout(monkeypatch):
    recorded = _recorder(monkeypatch)

    llm_client.decide_code_investigation_calls("payment-service", "payment is slow", [], NO_SERVICE_DIR)

    system = _system(recorded)
    assert "no directory" in system.lower()
    assert "services/" in system


def test_a_truncated_file_list_says_it_was_cut(monkeypatch):
    recorded = _recorder(monkeypatch)

    llm_client.decide_code_investigation_calls(
        "payment-service", "payment is slow", [], {**CONTEXT, "service_files_truncated": True}
    )

    assert "more files" in _system(recorded).lower()


def test_without_a_context_the_prompt_is_the_one_it_always_was(monkeypatch):
    recorded = _recorder(monkeypatch)

    llm_client.decide_code_investigation_calls("payment-service", "payment is slow", [])

    system = _system(recorded)
    assert "services/payment-service" not in system
    assert "Tier 3 Code Investigator" in system


# ---- the node hands the context to the model, once, before it loops --------------------


def _state() -> AgentState:
    return AgentState(
        incident_id=1,
        service_name="payment-service",
        hypotheses=[
            {"hypothesis": Hypothesis(description="payment /charge is slow", category="slow_query"), "score": 0.8, "score_breakdown": {}}
        ],
    )


def _no_defect(monkeypatch) -> None:
    monkeypatch.setattr(tier3_nodes, "decide_defect", lambda *a: DefectDecision(
        CodeDefect(defect_found=False, description="n/a", fix_approach="n/a", confidence_rationale="n/a"), 0
    ))


def test_the_investigator_passes_the_services_context_on_every_pass(monkeypatch):
    seen = []
    monkeypatch.setattr(
        tier3_nodes, "decide_code_investigation_calls",
        lambda *a: (seen.append(a), ToolCallDecision([{"name": "search_repository", "arguments": {"query": "x"}}], 1))[1],
    )
    _no_defect(monkeypatch)
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)
    monkeypatch.setitem(tier3_nodes.TOOL_DISPATCH_TIER3, "search_repository", lambda args: {"status": "ok", "matches": []})
    state = _state()

    tier3_nodes.code_investigator_node(state)

    assert len(seen) == state.max_tier3_iterations
    assert all(call[3]["service_dir"] == "services/payment-service" for call in seen)
    assert all("services/payment-service/app.py" in call[3]["service_files"] for call in seen)


def test_the_context_is_recorded_on_the_audit_trail(monkeypatch):
    rows = []
    monkeypatch.setattr(tier3_nodes, "decide_code_investigation_calls", lambda *a: ToolCallDecision([], 0))
    _no_defect(monkeypatch)
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda incident, node, event, detail, text: rows.append((event, detail)))

    tier3_nodes.code_investigator_node(_state())

    detail = dict(rows)["investigation_context"]
    assert detail["service_dir"] == "services/payment-service"
    assert detail["service_file_count"] >= 1


def test_a_context_that_cannot_be_built_does_not_stop_the_investigation(monkeypatch):
    seen = []
    monkeypatch.setattr(tier3_nodes.repo_tool, "repo_context", lambda name: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr(tier3_nodes, "decide_code_investigation_calls", lambda *a: (seen.append(a), ToolCallDecision([], 0))[1])
    _no_defect(monkeypatch)
    monkeypatch.setattr(tier3_nodes, "record_audit", lambda *a, **k: None)

    command = tier3_nodes.code_investigator_node(_state())

    assert command.goto == END
    assert seen and seen[0][3] is None
