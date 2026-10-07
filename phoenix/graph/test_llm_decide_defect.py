"""The investigator is told what is and is not a defect before it names one.

A flag or toggle that selects between a good and a defective implementation is
scaffolding, and so is the function that dispatches on it. A run that named the
dispatcher and proposed flipping the flag handed the patch generator a target it
was forbidden to touch, so every patch was rejected. The client is a recorder:
no request leaves the process, and nothing here is specific to one defect."""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from types import SimpleNamespace

import phoenix.graph.llm_client as llm_client
from phoenix.graph.schemas import CodeDefect

USAGE = SimpleNamespace(prompt_tokens=4, completion_tokens=1, total_tokens=5)


def _recorder(monkeypatch) -> list[dict]:
    recorded: list[dict] = []
    defect = CodeDefect(
        defect_found=True, file_path="app/store.py", function_name="scan_all",
        description="d", fix_approach="f", confidence_rationale="c",
    )

    def fake_parse(**kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=defect))], usage=USAGE)

    def fake_create(**kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None))], usage=USAGE)

    monkeypatch.setattr(
        llm_client, "client",
        SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
            beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=fake_parse))),
        ),
    )
    return recorded


def _assert_scaffolding_guidance(system: str) -> None:
    assert "scaffolding" in system
    assert "dispatches" in system or "dispatcher" in system
    assert "implementation that does the defective work" in system
    assert "flip, default or reverse" in system


def test_the_conclusion_prompt_says_a_flag_and_its_dispatcher_are_not_the_defect(monkeypatch):
    recorded = _recorder(monkeypatch)

    llm_client.decide_defect("svc", "requests are slow", [])

    _assert_scaffolding_guidance(recorded[0]["messages"][0]["content"])


def test_the_investigation_prompt_points_the_search_at_the_implementation(monkeypatch):
    recorded = _recorder(monkeypatch)

    llm_client.decide_code_investigation_calls("svc", "requests are slow", [])

    _assert_scaffolding_guidance(recorded[0]["messages"][0]["content"])


def test_the_conclusion_prompt_keeps_its_existing_demands(monkeypatch):
    recorded = _recorder(monkeypatch)

    llm_client.decide_defect("svc", "requests are slow", [])

    system = recorded[0]["messages"][0]["content"]
    assert "the exact name of the one function whose body is defective" in system
    assert "set defect_found to false rather than guessing" in system
