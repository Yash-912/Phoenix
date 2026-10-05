"""How scoring uses the measured latency. The question under test is what counts as
evidence of a slow query through Prometheus: a measured, sustained p95 above the
paging alert's threshold supports it; the latency metric's own name, which every
service exports, a normal reading, or a failed read does not."""

import pytest

from phoenix.graph.schemas import Hypothesis
from phoenix.graph.scoring import CATEGORY_KEYWORDS, score_hypothesis

SLOW_MEASURE = {"verdict": "sustained_slow", "sustained_slow": True, "samples": 40, "peak_p95_seconds": 2.4}
NORMAL_MEASURE = {"verdict": "not_sustained_slow", "sustained_slow": False, "samples": 40, "peak_p95_seconds": 0.02}
TOO_SHORT = {"verdict": "insufficient_data", "sustained_slow": False, "samples": 1}
UNAVAILABLE = {"verdict": "unavailable", "sustained_slow": False}

SLOW = Hypothesis(description="a slow query", category="slow_query")


def _item(source: str, raw: dict) -> dict:
    return {"iteration": 1, "source": source, "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": f"{source}(...)", "raw_data": raw}


def _prom(measure: dict | None = None, **extra) -> dict:
    raw = {"status": "success", "data": {"resultType": "vector", "result": []}, **extra}
    if measure is not None:
        raw["latency_measure"] = measure
    return _item("query_prometheus", raw)


METRIC_NAME_ONLY = _prom(data={"resultType": "vector", "result": [
    {"metric": {"__name__": "http_request_duration_seconds_bucket", "job": "svc", "le": "0.1"}, "value": [1, "53"]}]})


def test_a_measured_sustained_slowdown_supports_a_slow_query_through_prometheus():
    score, breakdown = score_hypothesis([_prom(SLOW_MEASURE)], SLOW)

    assert breakdown["has_prometheus_signal"] == 1
    assert score == 0.4


def test_the_latency_metric_name_alone_is_not_evidence_of_a_slow_query():
    """Every service exports the histogram, so its name matches whatever its value is."""
    score, breakdown = score_hypothesis([METRIC_NAME_ONLY], SLOW)

    assert breakdown["has_prometheus_signal"] == 0
    assert score == 0.0


def test_a_normal_measurement_overrides_text_that_merely_mentions_latency():
    text = _prom(NORMAL_MEASURE, text="HighLatency p95 slow request_duration")

    score, breakdown = score_hypothesis([text], SLOW)

    assert breakdown["has_prometheus_signal"] == 0
    assert score == 0.0


@pytest.mark.parametrize("measure", [TOO_SHORT, UNAVAILABLE], ids=["insufficient", "unavailable"])
def test_a_measurement_that_could_not_be_judged_supports_nothing(measure):
    score, breakdown = score_hypothesis([_prom(measure)], SLOW)

    assert breakdown["has_prometheus_signal"] == 0
    assert score == 0.0


def test_a_measurement_carried_by_anything_but_a_prometheus_read_is_not_credited():
    forged = _item("query_loki", {"status": "success", "latency_measure": SLOW_MEASURE})

    _, breakdown = score_hypothesis([forged], SLOW)

    assert breakdown["has_prometheus_signal"] == 0


def test_a_failed_prometheus_read_carrying_a_measurement_is_ignored():
    failed = _item("query_prometheus", {"status": "error", "error": "boom", "latency_measure": SLOW_MEASURE})

    _, breakdown = score_hypothesis([failed], SLOW)

    assert breakdown["has_prometheus_signal"] == 0


def test_the_measurement_plus_a_corroborating_log_clears_the_routing_threshold():
    log = _item("query_loki", {"status": "success", "text": "duration: 1203.4 ms  statement: SELECT * FROM charges"})

    score, breakdown = score_hypothesis([_prom(SLOW_MEASURE), log], SLOW)

    assert breakdown["sources_supporting"] == 2
    assert score == 0.9


def test_the_breakdown_records_what_the_prometheus_signal_rested_on():
    _, breakdown = score_hypothesis([_prom(SLOW_MEASURE)], SLOW)

    assert breakdown["prometheus_signal_basis"] == "measured_latency"


@pytest.mark.parametrize("category", [c for c in CATEGORY_KEYWORDS if c != "slow_query"])
def test_a_latency_measure_never_changes_how_any_other_category_scores(category):
    hypothesis = Hypothesis(description="something else", category=category)
    base = [
        _item("query_prometheus", {"status": "success", "text": "ServiceDown up==0 HighLatency p95 slow v18 timeout"}),
        _item("query_loki", {"status": "success", "text": "traceback error rate 5xx timeout slow"}),
    ]
    with_measure = [{**base[0], "raw_data": {**base[0]["raw_data"], "latency_measure": SLOW_MEASURE}}, base[1]]

    assert score_hypothesis(with_measure, hypothesis) == score_hypothesis(base, hypothesis)
