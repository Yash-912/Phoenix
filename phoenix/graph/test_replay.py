"""Offline replay: re-score a stored incident with the current evidence-validity logic.

The replay takes what the run stored -- the evidence rows, the hypotheses it
proposed -- and asks what today's code would conclude from the same evidence.
Everything it reads from the world (the deployment history, the latency series,
the live symptom) comes in through injected functions, so the core is a pure
function of stored rows and needs no database or Prometheus."""

from datetime import datetime, timedelta, timezone

from phoenix.graph import replay

ONSET = datetime(2026, 10, 5, 19, 9, 25, tzinfo=timezone.utc)


def _incident(**fields) -> dict:
    return {"id": 94, "service_name": "payment-service", "alertname": "HighErrorRate",
            "first_seen_at": ONSET, **fields}


def _row(source: str, summary: str, raw: dict, minutes_after_onset: float = 1.0) -> dict:
    return {"source": source, "summary": summary, "raw_data": raw, "iteration": 1,
            "collected_at": ONSET + timedelta(minutes=minutes_after_onset)}


def _marker(minutes_before_onset: float, **fields) -> dict:
    when = ONSET - timedelta(minutes=minutes_before_onset)
    return {"service": "payment-service", "timestamp": when.isoformat(), "rolled_back_from": None,
            "git_commit": "x", **fields}


SLOW_ON = {"image_tag": "slow-query", "git_commit": "slow-join", "config": {"slow_query": True}}
HEALTHY = {"image_tag": "healthy", "git_commit": "indexed", "config": {"slow_query": False}}

# What the run stored for the deployment read: an annotation computed by the code of the day.
STORED_DEPLOYMENTS = _row("get_recent_deployments", "get_recent_deployments({'service_name': 'payment-service'})", {
    "status": "ok", "service": "payment-service",
    "deployments": [{**_marker(26, **SLOW_ON), "correlation": "before_incident", "in_window": True}],
})
LATENCY_READ = _row("query_prometheus", "query_prometheus({'promql': 'http_request_duration_seconds_bucket{job=\"payment-service\"}'})",
                    {"status": "success", "data": {"result": []}})
HYPOTHESES = [{"description": "a slow query", "score": 0.75, "created_at": ONSET + timedelta(minutes=2),
               "score_breakdown": {"category": "slow_query"}}]

SLOW_MEASURE = {"verdict": "elevated_ongoing", "sustained_slow": True}
STALE_MEASURE = {"verdict": "elevated_before_onset_only", "sustained_slow": False, "historical_only": True}


def _latency(measure):
    return lambda service, at, onset: measure


def _symptom(state):
    return lambda category, service, at: {"state": state}


def _rescore(evidence, history=(), measure=STALE_MEASURE, symptom="unknown", hypotheses=HYPOTHESES):
    return replay.rescore_incident(
        _incident(), evidence, hypotheses, list(history),
        latency_fn=_latency(measure), symptom_fn=_symptom(symptom),
    )


def test_a_stored_marker_is_re_read_against_the_history_that_superseded_it():
    history = [_marker(20, **HEALTHY), _marker(26, **SLOW_ON)]  # newest first

    result = _rescore([STORED_DEPLOYMENTS], history)

    assert result["hypotheses"][0]["breakdown"]["has_deploy_signal"] == 0
    assert any(r["why"] == "superseded" for r in result["rejections"])


def test_the_same_marker_with_no_revert_in_the_history_still_counts():
    result = _rescore([STORED_DEPLOYMENTS], [_marker(26, **SLOW_ON)])

    assert result["hypotheses"][0]["breakdown"]["has_deploy_signal"] == 1
    assert result["rejections"] == []


def test_a_marker_written_after_the_evidence_was_collected_is_not_part_of_what_the_run_could_see():
    """The history on disk today includes markers from later runs; the replay must not read them."""
    later_revert = _marker(-30, **HEALTHY)  # 30 minutes after onset, after the evidence was collected
    result = _rescore([STORED_DEPLOYMENTS], [later_revert, _marker(26, **SLOW_ON)])

    assert result["hypotheses"][0]["breakdown"]["has_deploy_signal"] == 1


def test_a_latency_read_is_given_the_measure_anchored_to_this_incident():
    result = _rescore([LATENCY_READ], measure=STALE_MEASURE)

    assert result["hypotheses"][0]["breakdown"]["has_prometheus_signal"] == 0
    assert any(r["why"] == "elevated_before_onset_only" for r in result["rejections"])


def test_a_current_slowdown_measure_is_credited():
    result = _rescore([LATENCY_READ], measure=SLOW_MEASURE)

    assert result["hypotheses"][0]["breakdown"]["has_prometheus_signal"] == 1


def test_a_stale_measure_the_run_stored_is_replaced_not_trusted():
    stored = _row("query_prometheus", LATENCY_READ["summary"],
                  {"status": "success", "data": {"result": []}, "latency_measure": SLOW_MEASURE})

    result = _rescore([stored], measure=STALE_MEASURE)

    assert result["hypotheses"][0]["breakdown"]["has_prometheus_signal"] == 0


def test_a_prometheus_read_of_something_else_is_left_exactly_as_stored():
    other = _row("query_prometheus", "query_prometheus({'promql': 'up{job=\"payment-service\"}'})",
                 {"status": "success", "data": {"result": []}})
    asked = []

    replay.rescore_incident(_incident(), [other], HYPOTHESES, [],
                            latency_fn=lambda *a: asked.append(a) or SLOW_MEASURE, symptom_fn=_symptom("unknown"))

    assert asked == []


def test_the_result_names_the_leading_hypothesis_and_whether_it_would_be_acted_on():
    strong = [_row("query_prometheus", LATENCY_READ["summary"],
                   {"status": "success", "data": {"result": []}}),
              _row("query_loki", "query_loki(...)", {"status": "success", "text": "duration: 1203 ms  statement: SELECT"})]

    result = _rescore(strong, [_marker(1, **SLOW_ON)], measure=SLOW_MEASURE, symptom="present",
                      hypotheses=HYPOTHESES)

    assert result["predicted_category"] == "slow_query"
    assert result["score"] >= 0.75
    assert result["would_remediate"] is True
    assert result["blocked_by_symptom"] is False


def test_a_symptom_that_is_gone_blocks_the_action_even_when_the_score_clears_the_bar():
    strong = [_row("query_prometheus", LATENCY_READ["summary"], {"status": "success", "data": {"result": []}}),
              _row("query_loki", "query_loki(...)", {"status": "success", "text": "duration: 1203 ms  statement: SELECT"})]

    result = _rescore(strong, [_marker(1, **SLOW_ON)], measure=SLOW_MEASURE, symptom="absent")

    assert result["score"] >= 0.75
    assert result["blocked_by_symptom"] is True
    assert result["would_remediate"] is False


def test_a_category_with_no_probe_is_not_asked_about_its_symptom():
    asked = []
    crash = [{"description": "crash", "score": 0.4, "created_at": ONSET, "score_breakdown": {"category": "crash"}}]

    result = replay.rescore_incident(_incident(), [], crash, [], latency_fn=_latency(SLOW_MEASURE),
                                     symptom_fn=lambda *a: asked.append(a) or {"state": "absent"})

    assert result["symptom"] is None
    assert result["blocked_by_symptom"] is False


def test_an_incident_with_no_hypotheses_predicts_nothing():
    result = _rescore([], hypotheses=[])

    assert result["predicted_category"] is None
    assert result["score"] == 0.0
    assert result["would_remediate"] is False


# ---- judging against a label ---------------------------------------------------------------------


def _result(category="slow_query", score=0.9, would=True, blocked=False) -> dict:
    return {"predicted_category": category, "score": score, "would_remediate": would, "blocked_by_symptom": blocked}


def test_an_ambiguous_incident_passes_when_nothing_would_be_remediated():
    verdict = replay.judge(_result(score=0.4, would=False), {"expect": "ambiguous"})

    assert verdict["passed"] is True
    assert verdict["margin"] == round(0.75 - 0.4, 3)


def test_an_ambiguous_incident_fails_when_it_would_be_remediated():
    verdict = replay.judge(_result(score=0.75, would=True), {"expect": "ambiguous"})

    assert verdict["passed"] is False
    assert verdict["margin"] == 0.0


def test_an_ambiguous_incident_blocked_by_the_symptom_check_passes_on_a_high_score():
    verdict = replay.judge(_result(score=0.9, would=False, blocked=True), {"expect": "ambiguous"})

    assert verdict["passed"] is True
    assert verdict["protected_by"] == "symptom_check"


def test_a_true_incident_passes_when_the_right_category_clears_the_bar():
    verdict = replay.judge(_result("slow_query", 0.9, True), {"expect": "slow_query"})

    assert verdict["passed"] is True
    assert verdict["margin"] == round(0.9 - 0.75, 3)


def test_a_true_incident_fails_on_the_wrong_category_or_a_score_below_the_bar():
    assert replay.judge(_result("overload", 0.9, True), {"expect": "slow_query"})["passed"] is False
    assert replay.judge(_result("slow_query", 0.6, False), {"expect": "slow_query"})["passed"] is False


def test_a_thin_margin_is_flagged_even_when_the_case_passes():
    """0.74 for a false positive and 0.76 for a true one both pass and both deserve a second look."""
    assert replay.judge(_result(score=0.74, would=False), {"expect": "ambiguous"})["suspicious"] is True
    assert replay.judge(_result("slow_query", 0.76, True), {"expect": "slow_query"})["suspicious"] is True
    assert replay.judge(_result("slow_query", 0.9, True), {"expect": "slow_query"})["suspicious"] is False


def test_an_unlabelled_incident_is_reported_not_judged():
    verdict = replay.judge(_result(), None)

    assert verdict["passed"] is None
