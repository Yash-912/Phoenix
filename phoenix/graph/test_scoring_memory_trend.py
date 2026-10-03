"""How scoring uses the measured memory trend. The question under test is what
counts as evidence of a memory leak, not which incident produces which answer:
a measured, sustained growth supports the hypothesis; a chaos endpoint's name in
a log line or a metric label, a flat reading, or a failed read does not."""

import pytest

from phoenix.graph.schemas import Hypothesis
from phoenix.graph.scoring import CATEGORY_KEYWORDS, score_hypothesis

GROWTH = {"verdict": "sustained_growth", "sustained_growth": True, "samples": 31, "growth_bytes": 72000000}
FLAT = {"verdict": "no_sustained_growth", "sustained_growth": False, "samples": 31, "growth_bytes": 0}
TOO_SHORT = {"verdict": "insufficient_data", "sustained_growth": False, "samples": 3}
UNAVAILABLE = {"verdict": "unavailable", "sustained_growth": False}

LEAK = Hypothesis(description="resident memory keeps growing", category="memory_leak")


def _item(source: str, raw: dict) -> dict:
    return {"iteration": 1, "source": source, "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": f"{source}(...)", "raw_data": raw}


def _prom(trend: dict | None = None, **extra) -> dict:
    raw = {"status": "success", "data": {"resultType": "vector", "result": []}, **extra}
    if trend is not None:
        raw["memory_trend"] = trend
    return _item("query_prometheus", raw)


def _loki(line: str) -> dict:
    return _item("query_loki", {"status": "success", "data": {"result": [{"values": [["1", line]]}]}})


CHAOS_LOG = _loki('INFO:     172.18.0.1:37448 - "POST /chaos/leak/start HTTP/1.1" 200 OK')
CHAOS_LABELS = _prom(data={"resultType": "vector", "result": [
    {"metric": {"handler": "/chaos/leak/start", "job": "svc"}, "value": [1, "1"]}]})


def test_measured_sustained_growth_supports_a_leak_through_the_prometheus_signal():
    score, breakdown = score_hypothesis([_prom(GROWTH)], LEAK)

    assert breakdown["has_prometheus_signal"] == 1
    assert score == 0.4


def test_measured_growth_plus_a_corroborating_source_clears_the_routing_threshold():
    score, breakdown = score_hypothesis([_prom(GROWTH), CHAOS_LOG], LEAK)

    assert breakdown["sources_supporting"] == 2
    assert score == 0.9


def test_a_chaos_endpoints_name_in_logs_and_labels_is_not_evidence_of_a_leak():
    """Both signals matched only on the word 'leak' in a URL. Neither measured
    memory, so together they stay below the routing threshold."""
    score, breakdown = score_hypothesis([CHAOS_LABELS, CHAOS_LOG], LEAK)

    assert breakdown["has_prometheus_signal"] == 0
    assert breakdown["has_loki_signal"] == 1
    assert score == 0.3
    assert score < 0.75


def test_measured_flat_memory_overrides_a_log_line_that_merely_mentions_a_leak():
    score, breakdown = score_hypothesis([_prom(FLAT), CHAOS_LOG], LEAK)

    assert breakdown["has_prometheus_signal"] == 0
    assert score == 0.3 < 0.75


def test_a_flat_reading_of_the_memory_metric_is_not_support_for_a_leak():
    """The metric's own name is a returned value, so a keyword match on it would
    pass any service's memory reading. Only a measurement can."""
    flat_reading = _prom(data={"resultType": "vector", "result": [
        {"metric": {"__name__": "process_resident_memory_bytes", "job": "svc"}, "value": [1, "52113408"]}]})

    score, breakdown = score_hypothesis([flat_reading], LEAK)

    assert breakdown["has_prometheus_signal"] == 0
    assert score == 0.0


@pytest.mark.parametrize("trend", [TOO_SHORT, UNAVAILABLE], ids=["insufficient", "unavailable"])
def test_a_trend_that_could_not_be_judged_supports_nothing(trend):
    score, breakdown = score_hypothesis([_prom(trend)], LEAK)

    assert breakdown["has_prometheus_signal"] == 0
    assert score == 0.0


def test_a_trend_carried_by_anything_but_a_prometheus_read_is_not_credited():
    forged = _item("query_loki", {"status": "success", "memory_trend": GROWTH})

    score, breakdown = score_hypothesis([forged], LEAK)

    assert breakdown["has_prometheus_signal"] == 0
    assert score == 0.0


def test_a_failed_prometheus_read_carrying_a_trend_is_ignored():
    failed = _item("query_prometheus", {"status": "error", "error": "boom", "memory_trend": GROWTH})

    score, breakdown = score_hypothesis([failed], LEAK)

    assert breakdown["has_prometheus_signal"] == 0
    assert score == 0.0


def test_the_breakdown_records_what_the_prometheus_signal_rested_on():
    _, breakdown = score_hypothesis([_prom(GROWTH)], LEAK)

    assert breakdown["prometheus_signal_basis"] == "measured_memory_trend"


@pytest.mark.parametrize("category", [c for c in CATEGORY_KEYWORDS if c != "memory_leak"])
def test_a_memory_trend_block_never_changes_how_any_other_category_scores(category):
    """The block is for the leak hypothesis only: its numbers and verdict strings
    must not satisfy, or count against, any other category."""
    hypothesis = Hypothesis(description="something else", category=category)
    base = [
        _item("query_prometheus", {"status": "success", "text": "ServiceDown up==0 HighLatency p95 slow v18 timeout"}),
        _item("query_loki", {"status": "success", "text": "traceback error rate 5xx timeout slow"}),
    ]
    with_trend = [{**base[0], "raw_data": {**base[0]["raw_data"], "memory_trend": GROWTH}}, base[1]]
    with_flat = [{**base[0], "raw_data": {**base[0]["raw_data"], "memory_trend": FLAT}}, base[1]]

    expected = score_hypothesis(base, hypothesis)

    assert score_hypothesis(with_trend, hypothesis) == expected
    assert score_hypothesis(with_flat, hypothesis) == expected
