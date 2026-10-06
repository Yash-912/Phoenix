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

    assert result["verdict"] == "elevated_ongoing"
    assert result["sustained_slow"] is True
    assert result["peak_p95_seconds"] == 1.9


def test_a_single_spike_is_not_sustained():
    result = latency_tool.summarize_latency(_series(0.02, 3.0, 0.03, 0.02))

    assert result["sustained_slow"] is False
    assert result["verdict"] == "within_threshold"


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
        lambda job: measure or {"verdict": "elevated_ongoing", "sustained_slow": True, "job": job},
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


# ---- anchoring a measured slowdown to the incident's onset -----------------------------

from datetime import datetime, timezone

from phoenix.graph import correlation

T0 = 1_000_000.0


def _at(seconds: float) -> datetime:
    return datetime.fromtimestamp(T0 + seconds, tz=timezone.utc)


def _timed(*pairs: tuple[float, float]) -> list[tuple[float, float]]:
    """(seconds after T0, p95) pairs as samples."""
    return [(T0 + s, v) for s, v in pairs]


def _run(start: float, count: int, value: float = 2.0, step: float = 30.0) -> list[tuple[float, float]]:
    return [(T0 + start + step * i, value) for i in range(count)]


def _quiet(start: float, count: int, step: float = 30.0) -> list[tuple[float, float]]:
    return _run(start, count, 0.05, step)


def test_a_slow_run_that_ended_long_before_the_incident_is_history_not_a_cause():
    """The stale-evidence case: the previous scenario's slowdown ended 26 minutes before onset."""
    series = _run(0, 6) + _quiet(180, 60)
    onset = _at(180 + 60 * 30 - 60)

    result = latency_tool.summarize_latency(series, onset=onset)

    assert result["sustained_slow"] is False
    assert result["verdict"] == "elevated_before_onset_only"
    assert result["historical_only"] is True


def test_a_slow_run_that_overlaps_the_incident_onset_counts():
    series = _quiet(0, 10) + _run(300, 8)
    onset = _at(300 + 60)

    result = latency_tool.summarize_latency(series, onset=onset)

    assert result["sustained_slow"] is True
    assert result["anchored"] is True


def test_a_slow_run_that_begins_after_the_onset_counts():
    series = _quiet(0, 10) + _run(600, 4)

    assert latency_tool.summarize_latency(series, onset=_at(400))["sustained_slow"] is True


def test_a_slow_run_that_ended_within_the_detection_lag_before_onset_counts():
    """An alert needs for: 30s and a one minute rate window, so the incident opens a little after the symptom."""
    series = _run(0, 6) + _quiet(180, 40)
    run_end = 0 + 30 * 5
    onset = _at(run_end + correlation.ONSET_GRACE_SECONDS - 10)

    assert latency_tool.summarize_latency(series, onset=onset)["sustained_slow"] is True


def test_a_slow_run_that_ended_just_past_the_detection_lag_does_not_count():
    series = _run(0, 6) + _quiet(180, 60)
    run_end = 0 + 30 * 5
    onset = _at(run_end + correlation.ONSET_GRACE_SECONDS + 10)

    assert latency_tool.summarize_latency(series, onset=onset)["sustained_slow"] is False


def test_a_stale_run_does_not_hide_a_current_one():
    series = _run(0, 6) + _quiet(180, 20) + _run(800, 4)

    assert latency_tool.summarize_latency(series, onset=_at(820))["sustained_slow"] is True


def test_without_an_onset_the_whole_window_is_judged_and_says_it_was_not_anchored():
    series = _run(0, 6) + _quiet(180, 60)

    result = latency_tool.summarize_latency(series)

    assert result["sustained_slow"] is True
    assert result["anchored"] is False


def test_a_series_that_was_never_slow_is_not_called_historical():
    result = latency_tool.summarize_latency(_quiet(0, 20), onset=_at(300))

    assert result["verdict"] == "within_threshold"
    assert result.get("historical_only") is not True


def test_the_measure_anchors_to_the_incident_the_deployment_tool_was_told_about(monkeypatch):
    series = _run(0, 6) + _quiet(180, 60)
    values = [[t, str(v)] for t, v in series]
    payload = {"status": "success", "data": {"result": [{"metric": {}, "values": values}]}}
    monkeypatch.setattr(latency_tool, "query_prometheus_range", lambda *a: payload)
    monkeypatch.setattr(latency_tool, "incident_started_at", lambda: _at(180 + 59 * 30))

    result = latency_tool.get_latency_measure("svc")

    assert result["verdict"] == "elevated_before_onset_only"
    assert result["sustained_slow"] is False


def test_the_measure_can_be_taken_as_of_a_past_moment_for_replay(monkeypatch):
    seen = {}
    monkeypatch.setattr(latency_tool, "query_prometheus_range",
                        lambda promql, start, end, step: seen.update(start=start, end=end) or _matrix("0.02", "0.03"))
    monkeypatch.setattr(latency_tool, "incident_started_at", lambda: None)

    latency_tool.get_latency_measure("svc", at=5_000.0)

    assert seen["end"] == 5_000
    assert seen["start"] == 5_000 - latency_tool.WINDOW_MINUTES * 60


# ---- is the slowdown there now? ----------------------------------------------------------

NOW = 2_000_000.0


def _payload(*points: tuple[float, str]) -> dict:
    return {"status": "success", "data": {"result": [{"metric": {}, "values": [[t, v] for t, v in points]}]}}


def _state(monkeypatch, payload: dict) -> dict:
    monkeypatch.setattr(latency_tool, "query_prometheus_range", lambda *a: payload)
    return latency_tool.current_latency_state("svc", at=NOW)


def test_a_slow_latest_sample_means_the_slowdown_is_present(monkeypatch):
    result = _state(monkeypatch, _payload((NOW - 90, "0.02"), (NOW - 60, "1.5"), (NOW - 30, "2.0")))

    assert result["state"] == "present"
    assert result["latest_p95_seconds"] == 2.0


def test_a_healthy_latest_sample_means_the_slowdown_is_gone(monkeypatch):
    """Slow a few minutes ago, normal in the latest sample: the symptom self-cleared."""
    result = _state(monkeypatch, _payload((NOW - 300, "3.0"), (NOW - 270, "3.2"), (NOW - 60, "0.04"), (NOW - 30, "0.05")))

    assert result["state"] == "absent"
    assert result["latest_p95_seconds"] == 0.05


def test_a_latest_sample_too_old_to_speak_for_now_is_unknown(monkeypatch):
    result = _state(monkeypatch, _payload((NOW - 600, "3.0"), (NOW - 570, "0.04")))

    assert result["state"] == "unknown"


def test_a_window_with_no_traffic_is_unknown_not_healthy(monkeypatch):
    result = _state(monkeypatch, _payload((NOW - 60, "NaN"), (NOW - 30, "NaN")))

    assert result["state"] == "unknown"


def test_a_nan_latest_sample_falls_back_to_the_last_finite_one(monkeypatch):
    result = _state(monkeypatch, _payload((NOW - 60, "2.5"), (NOW - 30, "NaN")))

    assert result["state"] == "present"


def test_a_failed_read_is_unknown(monkeypatch):
    assert _state(monkeypatch, {"status": "error", "error": "boom"})["state"] == "unknown"


def test_a_selector_matching_two_series_is_unknown(monkeypatch):
    two = {"status": "success", "data": {"result": [{"values": [[NOW, "1"]]}, {"values": [[NOW, "1"]]}]}}

    assert _state(monkeypatch, two)["state"] == "unknown"


def test_a_job_name_outside_the_character_set_is_unknown_and_never_queried(monkeypatch):
    monkeypatch.setattr(latency_tool, "query_prometheus_range", lambda *a: (_ for _ in ()).throw(AssertionError("queried")))

    assert latency_tool.current_latency_state('svc"} or vector(1)', at=NOW)["state"] == "unknown"


# ---- the block's own words must not be evidence --------------------------------------------


def test_no_verdict_the_measure_can_emit_contains_a_keyword_of_any_category():
    """The scorer reads values, so a verdict spelled 'elevated_before_onset_only' satisfied the
    overload keyword 'slow' on every latency read, whatever the reading was."""
    from phoenix.graph.scoring import CATEGORY_KEYWORDS

    emitted = {
        latency_tool.summarize_latency(_run(0, 6) + _quiet(180, 40), onset=_at(60 + 30 * 8))["verdict"],       # ongoing
        latency_tool.summarize_latency(_run(0, 6) + _quiet(180, 60), onset=_at(180 + 59 * 30))["verdict"],     # before onset
        latency_tool.summarize_latency(_quiet(0, 20), onset=_at(300))["verdict"],                              # normal
        latency_tool.summarize_latency([])["verdict"],                                                         # too short
        latency_tool.UNAVAILABLE["verdict"],
    }
    keywords = {kw for words in CATEGORY_KEYWORDS.values() for kw in words}

    leaks = {(verdict, kw) for verdict in emitted for kw in keywords if kw in verdict}

    assert len(emitted) == 5
    assert leaks == set()
