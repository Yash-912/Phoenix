"""Is the symptom the evidence describes still there now?

Real evidence is not current evidence. A probe answers one question about one
category: present, absent, or unknown (it could not be observed). A category
with no probe is not checked, and nothing about it changes."""

import pytest

from phoenix.graph import symptom
from phoenix.tools import latency_tool, memory_tool


def test_a_slow_query_is_checked_against_the_services_current_latency(monkeypatch):
    monkeypatch.setattr(latency_tool, "current_latency_state", lambda service: {"state": "absent", "service": service})

    assert symptom.current_symptom("slow_query", "payment-service") == {"state": "absent", "service": "payment-service"}


def test_a_memory_leak_is_checked_against_the_services_current_memory_growth(monkeypatch):
    monkeypatch.setattr(memory_tool, "current_memory_state", lambda service: {"state": "present"})

    assert symptom.current_symptom("memory_leak", "worker-service") == {"state": "present"}


@pytest.mark.parametrize("category", ["crash", "overload", "deploy", "config", "network", "unknown", "nonsense", None])
def test_a_category_without_a_probe_is_not_checked(category):
    assert symptom.current_symptom(category, "svc") == {"state": "not_checked"}


def test_a_probe_that_raises_is_unknown_not_a_crashed_run(monkeypatch):
    def boom(service):
        raise RuntimeError("prometheus exploded")

    monkeypatch.setattr(latency_tool, "current_latency_state", boom)

    result = symptom.current_symptom("slow_query", "svc")

    assert result["state"] == "unknown"


def test_a_probe_that_returns_something_unreadable_is_unknown(monkeypatch):
    monkeypatch.setattr(latency_tool, "current_latency_state", lambda service: {"state": "maybe"})

    assert symptom.current_symptom("slow_query", "svc")["state"] == "unknown"


def test_a_probe_that_returns_a_non_dict_is_unknown(monkeypatch):
    monkeypatch.setattr(latency_tool, "current_latency_state", lambda service: None)

    assert symptom.current_symptom("slow_query", "svc")["state"] == "unknown"


def test_only_a_clear_absence_blocks_an_action():
    assert symptom.blocks_action({"state": "absent"}) is True
    for state in ("present", "unknown", "not_checked"):
        assert symptom.blocks_action({"state": state}) is False
