"""Is the service returning 5xx right now: present, absent, or unknown.

The same freshness rules as the latency probe: the latest finite sample speaks for now
only if it is recent, and a window with no traffic is unknown, never healthy."""

from phoenix.tools import error_rate_tool

NOW = 2_000_000.0


def _payload(*points: tuple[float, str]) -> dict:
    return {"status": "success", "data": {"result": [{"metric": {}, "values": [[t, v] for t, v in points]}]}}


def _state(monkeypatch, payload: dict) -> dict:
    monkeypatch.setattr(error_rate_tool, "query_prometheus_range", lambda *a: payload)
    return error_rate_tool.current_error_rate_state("svc", at=NOW)


def test_an_error_ratio_over_the_alert_threshold_is_present(monkeypatch):
    result = _state(monkeypatch, _payload((NOW - 60, "0.01"), (NOW - 30, "0.40")))

    assert result["state"] == "present"
    assert result["latest_error_ratio"] == 0.4


def test_a_latest_ratio_back_under_the_threshold_is_absent(monkeypatch):
    result = _state(monkeypatch, _payload((NOW - 120, "0.60"), (NOW - 60, "0.0"), (NOW - 30, "0.0")))

    assert result["state"] == "absent"


def test_a_ratio_exactly_at_the_threshold_is_not_over_it(monkeypatch):
    assert _state(monkeypatch, _payload((NOW - 30, "0.05")))["state"] == "absent"


def test_a_latest_sample_too_old_to_speak_for_now_is_unknown(monkeypatch):
    assert _state(monkeypatch, _payload((NOW - 600, "0.5"), (NOW - 570, "0.0")))["state"] == "unknown"


def test_a_window_with_no_traffic_is_unknown_not_healthy(monkeypatch):
    assert _state(monkeypatch, _payload((NOW - 60, "NaN"), (NOW - 30, "NaN")))["state"] == "unknown"


def test_a_window_with_no_series_is_unknown(monkeypatch):
    assert _state(monkeypatch, {"status": "success", "data": {"result": []}})["state"] == "unknown"


def test_a_failed_read_is_unknown(monkeypatch):
    assert _state(monkeypatch, {"status": "error", "error": "boom"})["state"] == "unknown"


def test_a_job_name_outside_the_character_set_is_unknown_and_never_queried(monkeypatch):
    def boom(*a):
        raise AssertionError("queried")

    monkeypatch.setattr(error_rate_tool, "query_prometheus_range", boom)

    assert error_rate_tool.current_error_rate_state('svc"} or vector(1)', at=NOW)["state"] == "unknown"


def test_the_query_is_the_alerts_ratio_scoped_to_one_job_and_zero_without_5xx_series(monkeypatch):
    seen = {}

    def fake(promql, start, end, step):
        seen.update(promql=promql, end=end)
        return _payload((NOW - 30, "0"))

    monkeypatch.setattr(error_rate_tool, "query_prometheus_range", fake)

    error_rate_tool.current_error_rate_state("svc", at=NOW)

    assert 'job="svc"' in seen["promql"]
    assert 'status=~"5.."' in seen["promql"]
    assert "or vector(0)" in seen["promql"]
    assert seen["end"] == int(NOW)
