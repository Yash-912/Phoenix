"""The measured latency attached to the observer's own Prometheus read.

summarize_latency is pure. The reads are patched at the module's own boundary, so
nothing here touches Prometheus."""

import math

import pytest

from phoenix.tools import latency_tool


def _series(*p95: float) -> list[tuple[float, float]]:
    return [(1000.0 + 30 * i, value) for i, value in enumerate(p95)]


def test_two_consecutive_samples_over_the_alert_threshold_are_a_sustained_slowdown():
    result = latency_tool.summarize_latency(_series(0.02, 0.03, 1.4, 1.9, 0.04))

    assert result["verdict"] == "sustained_slow"
    assert result["sustained_slow"] is True
    assert result["peak_p95_seconds"] == 1.9


def test_a_single_spike_is_not_sustained():
    result = latency_tool.summarize_latency(_series(0.02, 3.0, 0.03, 0.02))

    assert result["sustained_slow"] is False
    assert result["verdict"] == "not_sustained_slow"


def test_a_series_that_never_reaches_the_threshold_is_not_slow():
    result = latency_tool.summarize_latency(_series(*([0.02] * 20)))

    assert result["sustained_slow"] is False
    assert result["peak_p95_seconds"] == 0.02


def test_two_separate_spikes_do_not_add_up_to_a_sustained_run():
    assert latency_tool.summarize_latency(_series(2.0, 0.1, 2.0, 0.1))["sustained_slow"] is False


def test_nan_samples_from_a_window_with_no_traffic_are_dropped_not_read_as_zero_or_slow():
    result = latency_tool.summarize_latency(_series(math.nan, 1.5, 0.1, 1.6, 0.1))

    assert result["samples"] == 4
    assert result["sustained_slow"] is False


def test_the_samples_that_remain_after_dropping_nan_are_what_is_judged():
    result = latency_tool.summarize_latency(_series(math.nan, math.nan, 1.6, 1.7))

    assert result["sustained_slow"] is True


@pytest.mark.parametrize("series", [[], _series(2.0)], ids=["empty", "one-sample"])
def test_too_few_samples_to_judge_is_insufficient_data_never_slow(series):
    result = latency_tool.summarize_latency(series)

    assert result["verdict"] == "insufficient_data"
    assert result["sustained_slow"] is False


def _matrix(*values: str) -> dict:
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {}, "values": [[1000 + 30 * i, v] for i, v in enumerate(values)]}]}}


def test_the_measure_reads_one_jobs_p95_over_the_window(monkeypatch):
    seen = {}

    def fake_range(promql, start, end, step):
        seen.update(promql=promql, start=start, end=end, step=step)
        return _matrix("0.02", "1.5", "1.8")

    monkeypatch.setattr(latency_tool, "query_prometheus_range", fake_range)

    result = latency_tool.get_latency_measure("payment-service")

    assert result["sustained_slow"] is True
    assert 'job="payment-service"' in seen["promql"] and "histogram_quantile(0.95" in seen["promql"]
    assert seen["end"] - seen["start"] == result["window_seconds"]


def test_a_failed_read_is_unavailable_and_carries_no_error_text(monkeypatch):
    monkeypatch.setattr(latency_tool, "query_prometheus_range", lambda *a: {"status": "error", "error": "boom"})

    assert latency_tool.get_latency_measure("svc") == {"verdict": "unavailable", "sustained_slow": False}


def test_a_selector_matching_more_than_one_series_is_unavailable(monkeypatch):
    two = {"status": "success", "data": {"result": [{"values": [[1, "1"]]}, {"values": [[1, "1"]]}]}}
    monkeypatch.setattr(latency_tool, "query_prometheus_range", lambda *a: two)

    assert latency_tool.get_latency_measure("svc")["verdict"] == "unavailable"


@pytest.mark.parametrize("name", ['svc"} or vector(1) #', "a b", "", 'x{y="z"}'])
def test_a_job_name_outside_the_strict_character_set_is_never_queried(monkeypatch, name):
    def boom(*a):
        raise AssertionError("must not query")

    monkeypatch.setattr(latency_tool, "query_prometheus_range", boom)

    assert latency_tool.get_latency_measure(name)["verdict"] == "unavailable"


# ---- the wrapper around the observer's query_prometheus ----------------------------


def _wire(monkeypatch, instant: dict, measure: dict | None = None):
    monkeypatch.setattr(latency_tool, "query_prometheus_with_memory_trend", lambda promql: instant)
    monkeypatch.setattr(
        latency_tool, "get_latency_measure",
        lambda job: measure or {"verdict": "sustained_slow", "sustained_slow": True, "job": job},
    )


GOOD = {"status": "success", "data": {"result": []}}


def test_a_query_of_the_latency_histogram_for_one_job_comes_back_with_the_measure(monkeypatch):
    _wire(monkeypatch, GOOD)

    result = latency_tool.query_prometheus_with_latency_measure('http_request_duration_seconds_bucket{job="svc"}')

    assert result["latency_measure"]["job"] == "svc"
    assert result["status"] == "success"


def test_the_count_and_sum_series_of_the_histogram_also_trigger_it(monkeypatch):
    _wire(monkeypatch, GOOD)

    for name in ("http_request_duration_seconds_count", "http_request_duration_seconds_sum"):
        assert "latency_measure" in latency_tool.query_prometheus_with_latency_measure(f'{name}{{job="svc"}}')


def test_a_query_that_does_not_read_the_latency_metric_is_returned_unchanged(monkeypatch):
    _wire(monkeypatch, GOOD)

    assert latency_tool.query_prometheus_with_latency_measure('up{job="svc"}') is GOOD


def test_a_query_naming_more_than_one_job_or_none_is_returned_unchanged(monkeypatch):
    _wire(monkeypatch, GOOD)

    assert latency_tool.query_prometheus_with_latency_measure(
        'http_request_duration_seconds_bucket{job=~"a|b"}') is GOOD
    assert latency_tool.query_prometheus_with_latency_measure(
        'http_request_duration_seconds_bucket{job="a"} + http_request_duration_seconds_bucket{job="b"}') is GOOD


def test_a_failed_instant_read_is_returned_unchanged(monkeypatch):
    failed = {"status": "error", "error": "boom"}
    _wire(monkeypatch, failed)

    assert latency_tool.query_prometheus_with_latency_measure('http_request_duration_seconds_bucket{job="svc"}') is failed


def test_the_memory_trend_the_inner_wrapper_attached_is_kept(monkeypatch):
    _wire(monkeypatch, {**GOOD, "memory_trend": {"verdict": "sustained_growth", "sustained_growth": True}})

    result = latency_tool.query_prometheus_with_latency_measure('http_request_duration_seconds_bucket{job="svc"}')

    assert result["memory_trend"]["sustained_growth"] is True
    assert "latency_measure" in result
