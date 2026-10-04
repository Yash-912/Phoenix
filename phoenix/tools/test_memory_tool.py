"""The measured memory trend: genuine growth is told apart from allocator noise,
a one-off allocation, a restart and a series too short to judge. Nothing here is
specific to one service or one defect -- samples are plain (timestamp, bytes)
pairs, and the thresholds are the ones the paging alert and the post-restart
check already use."""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

import phoenix.tools.memory_tool as memory_tool
from phoenix.graph import scoring, verification

MIB = 1024 * 1024
STEP = 60
START = 1_700_000_000


def _series(values: list[float]) -> list[tuple[float, float]]:
    return [(START + i * STEP, v) for i, v in enumerate(values)]


def _ramp(count: int, per_step: float, base: float = 50 * MIB) -> list[float]:
    return [base + i * per_step for i in range(count)]


def test_the_thresholds_are_the_alerts_and_the_verifiers_not_new_ones():
    assert memory_tool.SLOPE_TOLERANCE == verification.SLOPE_TOLERANCE == 1024
    assert memory_tool.NOISE_FLOOR_BYTES == verification.NOISE_FLOOR_BYTES == MIB
    assert memory_tool.TREND_WINDOW_MINUTES == 30


def test_a_steady_leak_across_the_window_is_sustained_growth():
    trend = memory_tool.summarize_memory_trend(_series(_ramp(31, 2.4 * MIB)))

    assert trend["sustained_growth"] is True
    assert trend["verdict"] == "sustained_growth"
    assert trend["growth_bytes"] > 60 * MIB
    assert trend["slope_bytes_per_s"] > 1024
    assert trend["recent_slope_bytes_per_s"] > 1024


def test_a_leak_that_started_midway_through_the_window_is_still_growth():
    """The alert fires some minutes after a leak begins, so the 30m window holds
    a flat stretch and then the ramp. That must read as growth, not as noise."""
    values = [50 * MIB] * 15 + _ramp(16, 2.4 * MIB, base=50 * MIB)

    trend = memory_tool.summarize_memory_trend(_series(values))

    assert trend["sustained_growth"] is True


def test_allocator_wobble_on_a_flat_process_is_not_growth():
    wobble = [50 * MIB + ((-1) ** i) * 300 * 1024 for i in range(31)]

    trend = memory_tool.summarize_memory_trend(_series(wobble))

    assert trend["sustained_growth"] is False
    assert trend["verdict"] == "no_sustained_growth"


def test_a_slow_drift_below_the_paging_threshold_is_not_growth():
    """512 B/s sustained for 30 minutes is under the alert's own 1024 B/s."""
    trend = memory_tool.summarize_memory_trend(_series(_ramp(31, 512 * STEP)))

    assert trend["slope_bytes_per_s"] < 1024
    assert trend["sustained_growth"] is False


def test_a_one_off_allocation_is_not_a_leak():
    values = [50 * MIB] * 20 + [110 * MIB] * 11

    trend = memory_tool.summarize_memory_trend(_series(values))

    assert trend["growth_bytes"] > MIB
    assert trend["sustained_growth"] is False


def test_a_step_late_in_the_window_is_not_a_leak_either():
    values = [50 * MIB] * 28 + [110 * MIB] * 3

    assert memory_tool.summarize_memory_trend(_series(values))["sustained_growth"] is False


def test_growth_that_ends_in_a_drop_is_not_sustained():
    """A restart or a release part-way through is not monotonic growth."""
    values = _ramp(20, 2.4 * MIB) + _ramp(11, 0, base=50 * MIB)

    trend = memory_tool.summarize_memory_trend(_series(values))

    assert trend["largest_drop_bytes"] > MIB
    assert trend["sustained_growth"] is False


def test_a_shrinking_process_is_not_growth():
    trend = memory_tool.summarize_memory_trend(_series(list(reversed(_ramp(31, 2.4 * MIB)))))

    assert trend["sustained_growth"] is False


def test_too_few_samples_is_insufficient_data_never_growth():
    trend = memory_tool.summarize_memory_trend(_series(_ramp(4, 5 * MIB)))

    assert trend["verdict"] == "insufficient_data"
    assert trend["sustained_growth"] is False


def test_non_finite_samples_are_dropped_rather_than_trusted():
    values = _ramp(31, 2.4 * MIB)
    samples = _series(values)
    samples[10] = (samples[10][0], float("nan"))
    samples[11] = (samples[11][0], float("inf"))

    trend = memory_tool.summarize_memory_trend(samples)

    assert trend["samples"] == 29
    assert trend["sustained_growth"] is True


def test_no_string_in_the_trend_can_satisfy_a_hypothesis_keyword():
    """scoring matches keywords against returned values, so the block's own
    wording must never be mistaken for evidence of any category."""
    blocks = [
        memory_tool.summarize_memory_trend(_series(_ramp(31, 2.4 * MIB))),
        memory_tool.summarize_memory_trend(_series([50 * MIB] * 31)),
        memory_tool.summarize_memory_trend(_series(_ramp(3, 1))),
        memory_tool.UNAVAILABLE,
    ]
    strings = []
    for block in blocks:
        for value in block.values():
            if isinstance(value, str):
                strings.append(value.lower())
            elif isinstance(value, bool):
                strings.append(str(value).lower())

    for category, keywords in scoring.CATEGORY_KEYWORDS.items():
        for text in strings:
            assert not any(keyword in text for keyword in keywords), (category, text)


# ---- the read itself ------------------------------------------------------


def _range_payload(values: list[float], name: str = "process_resident_memory_bytes", job: str = "svc") -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{
                "metric": {"__name__": name, "job": job},
                "values": [[START + i * STEP, str(v)] for i, v in enumerate(values)],
            }],
        },
    }


def test_get_memory_trend_reads_one_series_and_summarises_it(monkeypatch):
    seen = {}

    def fake_range(promql, start, end, step):
        seen.update(promql=promql, start=start, end=end, step=step)
        return _range_payload(_ramp(31, 2.4 * MIB))

    monkeypatch.setattr(memory_tool, "query_prometheus_range", fake_range)

    trend = memory_tool.get_memory_trend("svc")

    assert seen["promql"] == 'process_resident_memory_bytes{job="svc"}'
    assert seen["end"] - seen["start"] == 30 * 60
    assert trend["sustained_growth"] is True
    assert trend["window_seconds"] == 30 * 60


def test_a_service_that_exports_no_such_series_is_insufficient_data(monkeypatch):
    monkeypatch.setattr(
        memory_tool, "query_prometheus_range",
        lambda *a: {"status": "success", "data": {"resultType": "matrix", "result": []}},
    )

    assert memory_tool.get_memory_trend("svc")["verdict"] == "insufficient_data"


def test_more_than_one_series_is_refused_not_guessed_from(monkeypatch):
    payload = _range_payload(_ramp(31, 2.4 * MIB))
    payload["data"]["result"].append(payload["data"]["result"][0])
    monkeypatch.setattr(memory_tool, "query_prometheus_range", lambda *a: payload)

    trend = memory_tool.get_memory_trend("svc")

    assert trend == memory_tool.UNAVAILABLE


def test_a_failed_read_is_unavailable_and_carries_no_error_text(monkeypatch):
    monkeypatch.setattr(
        memory_tool, "query_prometheus_range",
        lambda *a: {"status": "error", "error": "Connection refused: timeout"},
    )

    trend = memory_tool.get_memory_trend("svc")

    assert trend == memory_tool.UNAVAILABLE
    assert "refused" not in str(trend) and "timeout" not in str(trend)


# ---- attached to the observer's Prometheus read ---------------------------


def _instant(name="process_resident_memory_bytes", job="svc"):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"__name__": name, "job": job}, "value": [START, "52113408"]}]}}


def test_a_memory_query_for_one_job_comes_back_with_the_measured_trend(monkeypatch):
    monkeypatch.setattr(memory_tool, "query_prometheus", lambda promql: _instant())
    monkeypatch.setattr(memory_tool, "query_prometheus_range", lambda *a: _range_payload(_ramp(31, 2.4 * MIB)))

    result = memory_tool.query_prometheus_with_memory_trend('process_resident_memory_bytes{job="svc"}')

    assert result["data"]["result"][0]["value"][1] == "52113408"
    assert result["memory_trend"]["sustained_growth"] is True


def test_a_slope_query_on_the_memory_metric_is_measured_too(monkeypatch):
    monkeypatch.setattr(memory_tool, "query_prometheus", lambda promql: _instant())
    monkeypatch.setattr(memory_tool, "query_prometheus_range", lambda *a: _range_payload(_ramp(31, 2.4 * MIB)))

    result = memory_tool.query_prometheus_with_memory_trend('deriv(process_resident_memory_bytes{job="svc"}[30m])')

    assert result["memory_trend"]["sustained_growth"] is True


def test_the_job_measured_is_the_one_in_the_query(monkeypatch):
    seen = []
    monkeypatch.setattr(memory_tool, "query_prometheus", lambda promql: _instant(job="other"))
    monkeypatch.setattr(
        memory_tool, "query_prometheus_range",
        lambda promql, *a: (seen.append(promql), _range_payload([50 * MIB] * 31))[1],
    )

    memory_tool.query_prometheus_with_memory_trend('process_resident_memory_bytes{job="other"}')

    assert seen == ['process_resident_memory_bytes{job="other"}']


def test_queries_that_do_not_read_the_memory_metric_are_returned_untouched(monkeypatch):
    payload = {"status": "success", "data": {"resultType": "vector", "result": []}}
    monkeypatch.setattr(memory_tool, "query_prometheus", lambda promql: payload)
    monkeypatch.setattr(
        memory_tool, "query_prometheus_range",
        lambda *a: (_ for _ in ()).throw(AssertionError("no range read expected")),
    )

    assert memory_tool.query_prometheus_with_memory_trend('up{job="svc"}') is payload
    assert "memory_trend" not in memory_tool.query_prometheus_with_memory_trend('rate(http_requests_total{job="svc"}[5m])')


def test_a_memory_query_without_a_job_selector_is_not_measured(monkeypatch):
    monkeypatch.setattr(memory_tool, "query_prometheus", lambda promql: _instant())
    monkeypatch.setattr(
        memory_tool, "query_prometheus_range",
        lambda *a: (_ for _ in ()).throw(AssertionError("no range read expected")),
    )

    assert "memory_trend" not in memory_tool.query_prometheus_with_memory_trend("process_resident_memory_bytes")


def test_a_failed_prometheus_read_is_returned_as_is_without_a_trend(monkeypatch):
    failure = {"status": "error", "error": "boom"}
    monkeypatch.setattr(memory_tool, "query_prometheus", lambda promql: failure)

    result = memory_tool.query_prometheus_with_memory_trend('process_resident_memory_bytes{job="svc"}')

    assert result == failure


def test_an_unreadable_trend_leaves_the_original_read_intact(monkeypatch):
    monkeypatch.setattr(memory_tool, "query_prometheus", lambda promql: _instant())
    monkeypatch.setattr(memory_tool, "query_prometheus_range", lambda *a: {"status": "error", "error": "down"})

    result = memory_tool.query_prometheus_with_memory_trend('process_resident_memory_bytes{job="svc"}')

    assert result["data"]["result"][0]["value"][1] == "52113408"
    assert result["memory_trend"] == memory_tool.UNAVAILABLE
