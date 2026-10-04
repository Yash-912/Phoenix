"""decide_patch hands the model a structured target, asks it to declare which
function it changed before it writes the file, and parses that declaration.
The client is a recorder: no request ever leaves the process, and nothing here
is specific to any one defect."""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from types import SimpleNamespace

import phoenix.graph.llm_client as llm_client
from phoenix.graph.schemas import PatchTarget

TARGET = PatchTarget(
    target_file="app/store.py",
    target_function="evict_oldest",
    defect_summary="the cache never evicts, so it grows without bound",
    required_change="drop the oldest entry once the cap is reached",
    forbidden_areas=["every function other than the target function, including any dispatcher", "feature flags and chaos toggles"],
)

OLD_CONTENT = "def evict_oldest(cache):\n    return cache\n"


def _stub(monkeypatch, content: str) -> list[dict]:
    recorded: list[dict] = []

    def fake_create(**kwargs):
        recorded.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=1, total_tokens=5),
        )

    monkeypatch.setattr(
        llm_client, "client", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    )
    return recorded


def _reply(target: str | None = "evict_oldest", patched: str = "def evict_oldest(cache):\n    return cache[1:]\n") -> str:
    parts = []
    if target is not None:
        parts.append(f"{llm_client._TARGET_MARK_START}\n{target}\n{llm_client._TARGET_MARK_END}")
    parts.append(f"{llm_client._PATCH_MARK_START}\n{patched}{llm_client._PATCH_MARK_END}")
    parts.append(f"{llm_client._RATIONALE_MARK_START}\nbound the cache\n{llm_client._RATIONALE_MARK_END}")
    return "\n".join(parts)


def test_the_user_prompt_carries_the_structured_target_and_the_current_file(monkeypatch):
    recorded = _stub(monkeypatch, _reply())

    llm_client.decide_patch(OLD_CONTENT, TARGET)

    user = recorded[0]["messages"][1]["content"]
    assert '"target_file": "app/store.py"' in user
    assert '"target_function": "evict_oldest"' in user
    assert "the cache never evicts" in user
    assert "drop the oldest entry" in user
    assert "any dispatcher" in user
    assert OLD_CONTENT in user


def test_the_system_prompt_forbids_dispatcher_toggle_and_hard_wired_fixes_and_asks_for_the_target_first(monkeypatch):
    recorded = _stub(monkeypatch, _reply())

    llm_client.decide_patch(OLD_CONTENT, TARGET)

    system = recorded[0]["messages"][0]["content"]
    assert "dispatcher" in system
    assert "toggles" in system
    assert "hard-wire" in system
    assert "Do not edit tests" in system
    assert system.index(llm_client._TARGET_MARK_START) < system.index(llm_client._PATCH_MARK_START)


def test_the_declared_target_content_and_rationale_are_parsed(monkeypatch):
    _stub(monkeypatch, _reply(target="evict_oldest", patched="def evict_oldest(cache):\n    return cache[1:]\n"))

    decision = llm_client.decide_patch(OLD_CONTENT, TARGET)

    assert decision.output is not None
    assert decision.output.target_function == "evict_oldest"
    assert decision.output.new_content == "def evict_oldest(cache):\n    return cache[1:]\n"
    assert decision.output.rationale == "bound the cache"
    assert decision.tokens == 5


def test_a_reply_without_a_target_block_still_parses_with_an_empty_declaration(monkeypatch):
    _stub(monkeypatch, _reply(target=None))

    decision = llm_client.decide_patch(OLD_CONTENT, TARGET)

    assert decision.output is not None
    assert decision.output.target_function == ""


def test_a_reply_without_the_patch_markers_is_discarded(monkeypatch):
    _stub(monkeypatch, "here is a fix, trust me")

    decision = llm_client.decide_patch(OLD_CONTENT, TARGET)

    assert decision.output is None


def test_rejection_feedback_is_repeated_with_the_target_to_fix(monkeypatch):
    recorded = _stub(monkeypatch, _reply())

    llm_client.decide_patch(OLD_CONTENT, TARGET, feedback="patch modifies a chaos toggle (changed lines: `x = 1`)")

    user = recorded[0]["messages"][1]["content"]
    assert "rejected by the safety gate" in user
    assert "changed lines: `x = 1`" in user
    assert "must stay exactly as it is" in user
    assert "`evict_oldest`" in user
