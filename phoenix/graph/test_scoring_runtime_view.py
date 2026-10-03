"""Static docker configuration must not be scored as runtime evidence."""

from phoenix.graph import scoring
from phoenix.graph.schemas import Hypothesis

DOCKER_INSPECT = {
    "State": {"Status": "running", "ExitCode": 0},
    "HostConfig": {"MaskedPaths": ["/proc/latency_stats", "/proc/timer_stats"]},
    "Config": {"Env": ["CHAOS_ENABLED=true"]},
}


def test_masked_paths_do_not_satisfy_a_latency_keyword():
    blob = scoring._blob({"raw_data": DOCKER_INSPECT})

    assert "latency" not in blob
    assert "running" in blob


def test_the_reduction_applies_inside_inspect_health_nesting():
    blob = scoring._blob({"raw_data": {"container": DOCKER_INSPECT, "app": {"status": "ok"}}})

    assert "latency" not in blob
    assert "ok" in blob


def test_a_healthy_container_does_not_support_overload():
    evidence = [{"source": "get_container_state", "raw_data": DOCKER_INSPECT}]
    hypothesis = Hypothesis(description="overloaded", category="overload")

    score, breakdown = scoring.score_hypothesis(evidence, hypothesis)

    assert breakdown["has_docker_signal"] == 0


def test_a_payload_that_is_not_docker_inspect_is_unchanged():
    payload = {"status": "success", "text": "p95 latency high"}

    assert scoring._runtime_view(payload) == payload
