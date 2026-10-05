"""A URL path in what a tool returned is the caller's input, not an observation.

An HTTP access-log line says that a request was made; the path in it is whatever
the caller asked for. The reset script and the chaos scripts call endpoints named
/chaos/slow/disable and /chaos/leak/start, so scoring that path text as evidence
let the harness's own requests satisfy the slow_query and memory_leak keywords.
The same goes for the handler label on a request metric. The status code and the
rest of the line are still evidence, and so is any log message that is not a
request line."""

from phoenix.graph import scoring
from phoenix.graph.schemas import Hypothesis
from phoenix.graph.scoring import score_hypothesis

SLOW = Hypothesis(description="a slow query", category="slow_query")
LEAK = Hypothesis(description="memory keeps growing", category="memory_leak")
OVERLOAD = Hypothesis(description="the service is overloaded", category="overload")


def _item(source: str, raw: dict) -> dict:
    return {"iteration": 1, "source": source, "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": f"{source}(...)", "raw_data": raw}


def _loki(*lines: str) -> dict:
    return _item("query_loki", {"status": "success", "data": {"result": [{"values": [["1", line] for line in lines]}]}})


def test_a_chaos_endpoint_in_an_access_log_line_does_not_support_a_slow_query():
    line = 'INFO:     172.18.0.1:56726 - "POST /chaos/slow/enable HTTP/1.1" 200 OK'

    _, breakdown = score_hypothesis([_loki(line)], SLOW)

    assert breakdown["has_loki_signal"] == 0


def test_a_chaos_endpoint_in_an_access_log_line_does_not_support_a_leak():
    line = 'INFO:     172.18.0.1:37448 - "POST /chaos/leak/start HTTP/1.1" 200 OK'

    _, breakdown = score_hypothesis([_loki(line)], LEAK)

    assert breakdown["has_loki_signal"] == 0


def test_a_log_message_that_is_not_a_request_line_still_counts():
    _, breakdown = score_hypothesis([_loki("payment lookup is slow: took 2.1s per request")], SLOW)

    assert breakdown["has_loki_signal"] == 1


def test_the_method_and_status_of_a_request_line_are_kept():
    line = 'INFO:     172.18.0.1:56726 - "GET /charge HTTP/1.1" 503 Service Unavailable'

    blob = scoring._blob(_loki(line))

    assert "get" in blob and "http/1.1" in blob and "503 service unavailable" in blob
    assert "/charge" not in blob


def test_every_request_line_in_a_batch_is_stripped():
    lines = [f'INFO:     10.0.0.{i}:1 - "POST /chaos/slow/disable HTTP/1.1" 200 OK' for i in range(5)]

    blob = scoring._blob(_loki(*lines))

    assert "slow" not in blob and "chaos" not in blob


def test_a_request_metrics_handler_label_does_not_support_a_category():
    series = {"metric": {"handler": "/chaos/slow/disable", "job": "svc", "status": "2xx"}, "value": [1, "3"]}
    prom = _item("query_prometheus", {"status": "success", "data": {"resultType": "vector", "result": [series]}})

    _, breakdown = score_hypothesis([prom], OVERLOAD)

    assert breakdown["has_prometheus_signal"] == 0


def test_a_metrics_own_name_is_what_was_asked_for_not_something_observed():
    """process_resident_memory_bytes carries 'memory' in its name for every service,
    whatever the reading, so the name alone must not satisfy the overload keyword."""
    series = {"metric": {"__name__": "process_resident_memory_bytes", "job": "svc"}, "value": [1, "143536128"]}
    prom = _item("query_prometheus", {"status": "success", "data": {"resultType": "vector", "result": [series]}})

    _, breakdown = score_hypothesis([prom], OVERLOAD)

    assert breakdown["has_prometheus_signal"] == 0


def test_a_firing_alert_name_in_a_label_value_still_counts():
    """ALERTS{alertname="HighLatency"} is an observation: the alert is firing."""
    series = {"metric": {"__name__": "ALERTS", "alertname": "HighLatency", "alertstate": "firing"}, "value": [1, "1"]}
    prom = _item("query_prometheus", {"status": "success", "data": {"resultType": "vector", "result": [series]}})

    _, breakdown = score_hypothesis([prom], OVERLOAD)

    assert breakdown["has_prometheus_signal"] == 1


def test_other_labels_and_values_of_a_metric_still_count():
    series = {"metric": {"handler": "/x", "job": "svc"}, "value": [1, "high latency"]}
    prom = _item("query_prometheus", {"status": "success", "data": {"resultType": "vector", "result": [series]}})

    _, breakdown = score_hypothesis([prom], OVERLOAD)

    assert breakdown["has_prometheus_signal"] == 1
