"""Is the symptom the evidence describes still there now?

Real evidence is not current evidence. A probe answers one question about one
category: present, absent, or unknown (it could not be observed). A category
with no probe is not checked, and nothing about it changes."""

import pytest

from phoenix.graph import symptom
from phoenix.tools import error_rate_tool, latency_tool, memory_tool


def test_a_slow_query_is_checked_against_the_services_current_latency(monkeypatch):
    monkeypatch.setattr(latency_tool, "current_latency_state", lambda service: {"state": "absent", "service": service})

    assert symptom.current_symptom("slow_query", "payment-service") == {"state": "absent", "service": "payment-service"}


def test_a_memory_leak_is_checked_against_the_services_current_memory_growth(monkeypatch):
    monkeypatch.setattr(memory_tool, "current_memory_state", lambda service: {"state": "present"})

    assert symptom.current_symptom("memory_leak", "worker-service") == {"state": "present"}


def _overload(monkeypatch, errors: str, latency: str, memory: str) -> dict:
    monkeypatch.setattr(error_rate_tool, "current_error_rate_state", lambda service: {"state": errors})
    monkeypatch.setattr(latency_tool, "current_latency_state", lambda service: {"state": latency})
    monkeypatch.setattr(memory_tool, "current_memory_state", lambda service: {"state": memory})
    return symptom.current_symptom("overload", "svc")


@pytest.mark.parametrize(
    "errors, latency, memory",
    [("present", "absent", "absent"), ("absent", "present", "absent"), ("absent", "absent", "present"),
     ("present", "unknown", "unknown")],
)
def test_overload_is_present_when_any_one_of_its_signals_is(monkeypatch, errors, latency, memory):
    assert _overload(monkeypatch, errors, latency, memory)["state"] == "present"


def test_overload_is_absent_only_when_every_signal_is_clearly_absent(monkeypatch):
    assert _overload(monkeypatch, "absent", "absent", "absent")["state"] == "absent"


@pytest.mark.parametrize(
    "errors, latency, memory",
    [("unknown", "absent", "absent"), ("absent", "unknown", "absent"), ("absent", "absent", "unknown"),
     ("unknown", "unknown", "unknown")],
)
def test_overload_with_no_signal_present_and_one_unreadable_is_unknown_not_absent(monkeypatch, errors, latency, memory):
    assert _overload(monkeypatch, errors, latency, memory)["state"] == "unknown"


def test_overload_reports_what_each_signal_read(monkeypatch):
    result = _overload(monkeypatch, "absent", "absent", "absent")

    assert set(result["signals"]) == {"error_rate", "latency", "memory"}


def test_overload_with_a_signal_that_raises_is_unknown_not_a_crashed_run(monkeypatch):
    def boom(service):
        raise RuntimeError("prometheus exploded")

    monkeypatch.setattr(error_rate_tool, "current_error_rate_state", boom)
    monkeypatch.setattr(latency_tool, "current_latency_state", lambda service: {"state": "absent"})
    monkeypatch.setattr(memory_tool, "current_memory_state", lambda service: {"state": "absent"})

    assert symptom.current_symptom("overload", "svc")["state"] == "unknown"


def test_overload_present_beats_a_signal_that_raises(monkeypatch):
    def boom(service):
        raise RuntimeError("prometheus exploded")

    monkeypatch.setattr(error_rate_tool, "current_error_rate_state", boom)
    monkeypatch.setattr(latency_tool, "current_latency_state", lambda service: {"state": "present"})
    monkeypatch.setattr(memory_tool, "current_memory_state", lambda service: {"state": "absent"})

    assert symptom.current_symptom("overload", "svc")["state"] == "present"


@pytest.mark.parametrize("category", ["crash", "deploy", "config", "network", "unknown", "nonsense", None])
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
