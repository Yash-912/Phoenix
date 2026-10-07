"""Exact-score unit tests for scoring.py — no network, no LLM."""

from phoenix.graph.schemas import Hypothesis
from phoenix.graph.scoring import score_all, score_hypothesis, top_confidence


def _ev(source: str, text: str, status_ok: bool = True) -> dict:
    raw = {"status": "success", "text": text} if status_ok else {"status": "error", "error": "boom"}
    return {"iteration": 1, "source": source, "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": f"{source}({text})", "raw_data": raw}


def test_no_evidence_scores_zero():
    h = Hypothesis(description="shop crashed", category="crash")
    score, bd = score_hypothesis([], h)
    assert score == 0.0
    assert bd["sources_supporting"] == 0
    assert bd["contradiction_penalty"] == 0.0  # nothing looked at yet -> no penalty


def test_single_prometheus_match():
    ev = [_ev("query_prometheus", "ServiceDown firing, up==0")]
    h = Hypothesis(description="shop is down", category="crash")
    score, bd = score_hypothesis(ev, h)
    assert score == 0.4
    assert bd["has_prometheus_signal"] == 1
    assert bd["agreement_bonus"] == 0.0


def test_two_source_agreement_gets_bonus():
    ev = [_ev("query_prometheus", "HighLatency p95 slow"),
          _ev("query_loki", "request slow latency timeout")]
    h = Hypothesis(description="overload", category="overload")
    score, bd = score_hypothesis(ev, h)
    # 0.4 (prom) + 0.3 (loki) + 0.2 (agreement) = 0.9
    assert score == 0.9
    assert bd["sources_supporting"] == 2


def test_all_three_sources_clamps_to_one():
    ev = [_ev("query_prometheus", "HighLatency p95 spike"),
          _ev("query_loki", "traceback request slow"),
          _ev("get_container_state", "container exit dead oom crash")]
    h_crash = Hypothesis(description="crashed", category="crash")
    # docker-only support for crash: 0.3, no agreement, no penalty (only 1 distinct... wait 3 distinct sources but 0... recalc below)
    s_crash, _ = score_hypothesis(ev, h_crash)
    assert s_crash == 0.3

    h_over = Hypothesis(description="overloaded", category="overload")
    s_over, bd = score_hypothesis(ev, h_over)
    # prom (0.4) + loki (0.3) = 0.7 + agreement 0.2 = 0.9
    assert s_over == 0.9
    assert bd["sources_supporting"] == 2


def test_contradiction_penalty_when_thorough_lookup_finds_nothing():
    ev = [_ev("query_prometheus", "all healthy, latency fine"),
          _ev("query_loki", "all healthy, no errors")]
    h = Hypothesis(description="bad deploy v18", category="deploy")
    score, bd = score_hypothesis(ev, h)
    assert bd["sources_supporting"] == 0
    assert bd["contradiction_penalty"] == 0.3
    assert score == 0.0  # 0 - 0.3 clamped to 0


def test_error_envelope_evidence_is_ignored():
    ev = [_ev("query_prometheus", "ServiceDown", status_ok=False),
          _ev("query_loki", "connection refused", status_ok=False)]
    h = Hypothesis(description="network blip", category="network")
    score, bd = score_hypothesis(ev, h)
    assert score == 0.0
    assert bd["sources_supporting"] == 0


def test_unknown_category_never_matches_and_never_penalized():
    ev = [_ev("query_prometheus", "ServiceDown"), _ev("query_loki", "crash loop")]
    h = Hypothesis(description="something weird", category="unknown")
    score, bd = score_hypothesis(ev, h)
    assert score == 0.0
    assert bd["contradiction_penalty"] == 0.0


def test_health_source_alone_scores_non_zero_for_matching_category():
    ev = [_ev("inspect_health", "readiness probe failing, container dead")]
    h = Hypothesis(description="shop crashed", category="crash")
    score, bd = score_hypothesis(ev, h)
    assert score == 0.15
    assert bd["has_health_signal"] == 1
    assert bd["has_deploy_signal"] == 0


def test_query_arguments_in_summary_do_not_count_as_evidence():
    # The LLM's own query (container_cpu_cfs_throttled_periods_total) contains
    # "cpu", an overload keyword -- but the metric actually came back clean.
    # Scoring must key off raw_data, not off what the LLM chose to ask for.
    ev = [{
        "iteration": 1,
        "source": "query_prometheus",
        "collected_at": "2026-01-01T00:00:00+00:00",
        "summary": "query_prometheus({'promql': 'container_cpu_cfs_throttled_periods_total'})",
        "raw_data": {"status": "success", "value": "0"},
    }]
    h = Hypothesis(description="overload", category="overload")
    score, bd = score_hypothesis(ev, h)
    assert score == 0.0
    assert bd["has_prometheus_signal"] == 0


def test_deployments_source_alone_scores_non_zero_for_deploy_hypothesis():
    ev = [_ev("get_recent_deployments", "deployed image shop:v18 at 09:14, rollout complete")]
    h = Hypothesis(description="bad deploy v18", category="deploy")
    score, bd = score_hypothesis(ev, h)
    assert score == 0.15
    assert bd["has_deploy_signal"] == 1
    assert bd["sources_supporting"] == 1
    assert bd["weights"]["deploy"] == 0.15


def test_contradiction_penalty_counts_secondary_sources():
    ev = [_ev("inspect_health", "all systems healthy"),
          _ev("get_recent_deployments", "no recent changes")]
    h = Hypothesis(description="shop crashed", category="crash")
    score, bd = score_hypothesis(ev, h)
    assert bd["sources_supporting"] == 0
    assert bd["contradiction_penalty"] == 0.3
    assert score == 0.0


def _deploy_ev(raw_data: dict) -> dict:
    return {"iteration": 1, "source": "get_recent_deployments",
            "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": "get_recent_deployments({'service_name': 'checkout-service', 'limit': 10})",
            "raw_data": raw_data}


def test_tool_name_alone_never_satisfies_a_keyword():
    categories = ("crash", "overload", "deploy", "config", "network")
    sources = ("query_prometheus", "query_loki", "get_container_state",
               "inspect_health", "get_recent_deployments")
    for source in sources:
        ev = [_ev(source, "")]
        for category in categories:
            score, bd = score_hypothesis(ev, Hypothesis(description="x", category=category))
            assert score == 0.0, f"empty {source} auto-matched {category}"
            assert bd["sources_supporting"] == 0, f"empty {source} auto-matched {category}"


def test_deployments_source_without_deploy_content_has_no_signal():
    ev = [_ev("get_recent_deployments", "")]
    h = Hypothesis(description="bad deploy v18", category="deploy")
    score, bd = score_hypothesis(ev, h)
    assert bd["has_deploy_signal"] == 0
    assert bd["sources_supporting"] == 0
    assert score == 0.0


def test_deployments_source_with_deploy_content_has_signal():
    ev = [_ev("get_recent_deployments", "image_tag checkout:v18, rollout complete")]
    h = Hypothesis(description="bad deploy v18", category="deploy")
    score, bd = score_hypothesis(ev, h)
    assert bd["has_deploy_signal"] == 1
    assert score == 0.15


def test_empty_deployment_marker_list_is_not_deploy_evidence():
    ev = [_deploy_ev({"status": "ok", "service": "checkout-service", "deployments": []})]
    h = Hypothesis(description="bad deploy v18", category="deploy")
    score, bd = score_hypothesis(ev, h)
    assert bd["has_deploy_signal"] == 0
    assert bd["sources_supporting"] == 0
    assert score == 0.0


def test_deployment_marker_content_is_deploy_evidence():
    ev = [_deploy_ev({"status": "ok", "service": "checkout-service",
                      "deployments": [{"service": "checkout-service", "timestamp": "2026-01-01T09:14:00+00:00",
                                       "git_commit": "abc123", "image_tag": "checkout:v18",
                                       "deployed_by": "chaos script"}]})]
    h = Hypothesis(description="bad deploy v18", category="deploy")
    score, bd = score_hypothesis(ev, h)
    assert bd["has_deploy_signal"] == 1
    assert score == 0.15


def test_score_all_sorts_best_first_and_confidence():
    ev = [_ev("query_prometheus", "HighLatency p95 slow")]
    h_wrong = Hypothesis(description="bad deploy", category="deploy")
    h_right = Hypothesis(description="slow overload", category="overload")
    ranked = score_all(ev, [h_wrong, h_right])
    assert ranked[0][0].category == "overload"
    assert ranked[0][1] == 0.4
    assert ranked[1][1] == 0.0
    assert top_confidence(ev, [h_wrong, h_right]) == 0.4
    assert top_confidence([], []) == 0.0


FAILED_HEALTH = {
    "container": {"status": "error", "error": "Connection refused by the docker-socket-proxy"},
    "app": {"status": "error", "error": "HTTPConnectionPool(host='localhost'): Read timed out."},
}

DEGRADED_HEALTH = {
    "container": {"status": "error", "error": "Connection refused by the docker-socket-proxy"},
    "app": {"status": "degraded", "detail": "upstream connection refused by payment-service"},
}

DEAD_CONTAINER = {
    "container": {"State": {"Status": "exited", "ExitCode": 1}},
    "app": {"status": "error", "error": "HTTPConnectionPool(host='localhost'): Read timed out."},
}


def _health_ev(raw_data: dict) -> dict:
    """An inspect_health item carrying what the tool really returns.

    The payload is quoted from phoenix/tools/health_tool.py: four of the five
    read-only tools write their failure envelope flat, and inspect_health is the
    one that asks two systems a question, so a read that reached neither arrives
    as an envelope under container and the same envelope again under app.
    """
    return {"iteration": 1, "source": "inspect_health",
            "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": "inspect_health({'service_name': 'checkout-service'})",
            "raw_data": raw_data}


def _prom_ev(raw_data: dict) -> dict:
    return {"iteration": 1, "source": "query_prometheus",
            "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": "query_prometheus({'promql': 'up'})",
            "raw_data": raw_data}


def test_a_health_read_that_reached_nothing_is_not_evidence():
    h = Hypothesis(description="the network dropped", category="network")

    score, bd = score_hypothesis([_health_ev(FAILED_HEALTH)], h)

    assert bd["has_health_signal"] == 0
    assert bd["sources_supporting"] == 0
    assert score == 0.0


def test_a_failed_read_scores_below_a_read_that_answered_and_reported_the_same_words():
    h = Hypothesis(description="the network dropped", category="network")

    failed, _ = score_hypothesis([_health_ev(FAILED_HEALTH)], h)
    answered, bd = score_hypothesis([_health_ev(DEGRADED_HEALTH)], h)

    assert bd["sources_supporting"] == 1
    assert failed < answered
    assert answered == 0.15


def test_a_failed_read_does_not_suppress_the_contradiction_penalty():
    h = Hypothesis(description="the network dropped", category="network")
    ev = [_ev("query_prometheus", "all healthy, latency fine"),
          _ev("query_loki", "all healthy, no errors"),
          _health_ev(FAILED_HEALTH)]

    score, bd = score_hypothesis(ev, h)

    assert bd["sources_supporting"] == 0
    assert bd["contradiction_penalty"] == 0.3
    assert score == 0.0


def test_a_health_read_that_answered_on_one_channel_is_still_evidence():
    h = Hypothesis(description="the container is dead", category="crash")

    score, bd = score_hypothesis([_health_ev(DEAD_CONTAINER)], h)

    assert bd["has_health_signal"] == 1
    assert score == 0.15


def test_a_successful_prometheus_response_stays_evidence_though_a_metric_is_labelled_status_error():
    labelled = _prom_ev({"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"__name__": "probe", "status": "error"},
         "value": [1767225600, "0"]}]}})
    h = Hypothesis(description="the v18 rollout broke it", category="deploy")

    score, bd = score_hypothesis([labelled, _ev("query_loki", "all healthy, no errors")], h)

    assert bd["sources_supporting"] == 0
    assert bd["contradiction_penalty"] == 0.3
    assert score == 0.0


# ---- deployments the tool already ruled out by time ------------------------------


def _marker(correlation: str, **fields) -> dict:
    return {"service": "payment-service", "image_tag": "slow-query", "git_commit": "slow-join",
            "config": {"slow_query": True}, "correlation": correlation,
            "in_window": correlation == "before_incident", **fields}


def _deployments(*markers: dict) -> dict:
    return _deploy_ev({"status": "ok", "service": "payment-service", "deployments": list(markers)})


def test_a_deployment_far_before_the_incident_is_not_evidence_for_it():
    ev = [_deployments(_marker("too_far_before"))]

    score, bd = score_hypothesis(ev, Hypothesis(description="a slow query", category="slow_query"))

    assert bd["has_deploy_signal"] == 0
    assert score == 0.0


def test_a_deployment_after_the_incident_began_is_not_evidence_for_it():
    ev = [_deployments(_marker("after_incident"))]

    _, bd = score_hypothesis(ev, Hypothesis(description="a slow query", category="slow_query"))

    assert bd["has_deploy_signal"] == 0


def test_a_deployment_shortly_before_the_incident_still_counts():
    ev = [_deployments(_marker("before_incident"))]

    _, bd = score_hypothesis(ev, Hypothesis(description="a slow query", category="slow_query"))

    assert bd["has_deploy_signal"] == 1


def test_one_in_window_deployment_counts_when_older_ones_are_ruled_out():
    ev = [_deployments(_marker("too_far_before"), _marker("before_incident"))]

    _, bd = score_hypothesis(ev, Hypothesis(description="a slow query", category="slow_query"))

    assert bd["has_deploy_signal"] == 1


def test_an_unknown_incident_time_is_a_gap_not_an_exclusion():
    """The tool could not say whether the deployment was related; that is not a ruling out."""
    ev = [_deployments(_marker("incident_time_unknown"))]

    _, bd = score_hypothesis(ev, Hypothesis(description="a slow query", category="slow_query"))

    assert bd["has_deploy_signal"] == 1


def test_a_superseded_deployment_is_not_evidence_for_the_incident():
    """A later marker replaced that state before the incident began, so it is history."""
    ev = [_deployments(_marker("superseded"))]

    score, bd = score_hypothesis(ev, Hypothesis(description="a slow query", category="slow_query"))

    assert bd["has_deploy_signal"] == 0
    assert score == 0.0


def test_the_marker_that_superseded_it_is_still_read_as_the_state_in_effect():
    ev = [_deployments(_marker("superseded"), _marker("before_incident", image_tag="healthy", git_commit="indexed", config={"slow_query": False}))]

    _, bd = score_hypothesis(ev, Hypothesis(description="a slow query", category="slow_query"))

    # The healthy marker carries no slow-query keyword, and the superseded one is gone.
    assert bd["has_deploy_signal"] == 0


# ---- overload is not evidenced by the symptoms every fault shares ----------------------------

OVERLOAD_HYPOTHESIS = Hypothesis(description="the service is overloaded", category="overload")

# The shape of a real http_requests_total read: the status class is a label on the
# counter, so "5xx" is in the payload whenever the series exists, errors or none.
PROM_REQUEST_COUNTER = {
    "status": "success",
    "data": {"resultType": "vector", "result": [
        {"metric": {"handler": "/charge", "method": "post", "status": "2xx", "job": "payment-service"}, "value": [1791305993.1, "174"]},
        {"metric": {"handler": "/charge", "method": "get", "status": "5xx", "job": "payment-service"}, "value": [1791305993.1, "0"]},
    ]},
}


def _item(source: str, raw: dict) -> dict:
    return {"iteration": 1, "source": source, "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": f"{source}()", "raw_data": raw}


def test_a_5xx_status_label_on_a_request_counter_is_not_overload_evidence():
    score, bd = score_hypothesis([_item("query_prometheus", PROM_REQUEST_COUNTER)], OVERLOAD_HYPOTHESIS)

    assert bd["has_prometheus_signal"] == 0
    assert score == 0.0


def test_error_symptom_words_are_not_overload_evidence_from_any_source():
    for text in ("HighErrorRate firing", "traceback error rate 5xx", "5xx spike on /charge"):
        for source in ("query_prometheus", "query_loki"):
            _, bd = score_hypothesis([_ev(source, text)], OVERLOAD_HYPOTHESIS)
            assert bd["sources_supporting"] == 0, (source, text)


def test_error_symptoms_that_every_fault_shares_do_not_reach_the_threshold_for_overload():
    ev = [_ev("query_prometheus", "HighErrorRate 5xx spike"), _ev("query_loki", "traceback error rate 5xx")]

    score, bd = score_hypothesis(ev, OVERLOAD_HYPOTHESIS)

    assert bd["sources_supporting"] == 0
    assert score < 0.75


def test_latency_and_resource_pressure_still_support_overload():
    for text in ("HighLatency p95", "cpu saturated", "memory pressure", "overload shedding requests"):
        _, bd = score_hypothesis([_ev("query_prometheus", text)], OVERLOAD_HYPOTHESIS)
        assert bd["has_prometheus_signal"] == 1, text
