"""Real evidence is not current evidence; current evidence is not causal evidence.

The ladder, in the order the code applies it:

    historical evidence
    -> was it superseded before the incident began?        (correlation)
    -> does the measurement overlap the incident's onset?  (latency_tool)
    -> does the symptom exist now?                         (symptom, at remediation)
    -> only then may it support remediation.

These tests compose the real pieces -- the marker logic, the latency measure and
the scorer -- over a service that was genuinely slow, once for the incident that
slowness caused and once for an unrelated incident that began after it was fixed.
Nothing here names a scenario in the code under test."""

from datetime import datetime, timedelta, timezone

from phoenix.graph import correlation, scoring
from phoenix.graph.schemas import Hypothesis
from phoenix.tools import latency_tool

SLOW = Hypothesis(description="a slow query", category="slow_query")
ONSET = datetime(2026, 10, 5, 19, 9, 25, tzinfo=timezone.utc)
STEP = 30

SLOW_ON = {"image_tag": "slow-query", "git_commit": "slow-join", "config": {"slow_query": True}}
HEALTHY = {"image_tag": "healthy", "git_commit": "indexed", "config": {"slow_query": False}}
STALE_SLOW_STATEMENT = "duration: 1203.4 ms  statement: SELECT order_id, amount, status FROM charges"


def _marker(minutes_before_onset: float, **fields) -> dict:
    when = ONSET - timedelta(minutes=minutes_before_onset)
    return {"service": "payment-service", "timestamp": when.isoformat(), "rolled_back_from": None, **fields}


def _item(source: str, raw: dict) -> dict:
    return {"iteration": 1, "source": source, "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": f"{source}(...)", "raw_data": raw}


def _deployments(*oldest_first: dict) -> dict:
    annotated = correlation.correlate_all(list(reversed(oldest_first)), ONSET)
    return _item("get_recent_deployments", {"status": "ok", "service": "payment-service", "deployments": annotated})


def _p95(slow_from_minutes: float | None, slow_until_minutes: float | None, window_minutes: int = 30) -> list:
    """A 30 minute series ending 2 minutes after onset, slow between the two offsets (minutes before onset)."""
    end = ONSET + timedelta(minutes=2)
    samples = []
    t = end - timedelta(minutes=window_minutes)
    while t <= end:
        minutes_before = (ONSET - t).total_seconds() / 60
        slow = (
            slow_from_minutes is not None
            and slow_until_minutes is not None
            and slow_until_minutes <= minutes_before <= slow_from_minutes
        )
        samples.append((t.timestamp(), 2.4 if slow else 0.04))
        t += timedelta(seconds=STEP)
    return samples


def _latency(series: list) -> dict:
    return _item("query_prometheus", {"status": "success", "latency_measure": latency_tool.summarize_latency(series, onset=ONSET)})


def _loki(text: str) -> dict:
    return _item("query_loki", {"status": "success", "text": text})


def _score(*evidence: dict):
    return scoring.score_hypothesis(list(evidence), SLOW)


# 1. genuine slow query -----------------------------------------------------------------


def test_a_genuine_slow_query_under_way_at_onset_is_still_detected():
    """The fault was injected a minute and a half before the incident and is still going."""
    score, breakdown = _score(
        _deployments(_marker(1.5, **SLOW_ON)),
        _latency(_p95(slow_from_minutes=1.5, slow_until_minutes=-2)),
        _loki(STALE_SLOW_STATEMENT),
    )

    assert breakdown["has_deploy_signal"] == 1
    assert breakdown["has_prometheus_signal"] == 1
    assert score >= 0.75


# 2. stale evidence from before an unrelated incident --------------------------------------


def test_a_slowdown_that_was_fixed_before_an_unrelated_incident_is_not_a_cause_of_it():
    """Slow 28 to 24 minutes before onset, reverted 20 minutes before, and a slow-statement
    line from it still sitting in the logs. The incident that follows is a different one."""
    score, breakdown = _score(
        _deployments(_marker(28, **SLOW_ON), _marker(20, **HEALTHY)),
        _latency(_p95(slow_from_minutes=28, slow_until_minutes=24)),
        _loki(STALE_SLOW_STATEMENT),
    )

    assert breakdown["has_deploy_signal"] == 0
    assert breakdown["has_prometheus_signal"] == 0
    assert score < 0.75
    assert score == 0.3  # only the stale log line, which is one source and below the bar


def test_the_stale_marker_alone_cannot_carry_a_diagnosis_even_with_no_revert_recorded():
    """Without the superseding marker the deployment is inside the 1 hour window, but the
    measurement is what shows the symptom was over, so the pair still falls short."""
    score, breakdown = _score(
        _deployments(_marker(28, **SLOW_ON)),
        _latency(_p95(slow_from_minutes=28, slow_until_minutes=24)),
    )

    assert breakdown["has_prometheus_signal"] == 0
    assert score < 0.75


# 3. evidence overlapping the onset -------------------------------------------------------------


def test_a_slowdown_that_overlaps_the_onset_is_eligible():
    _, breakdown = _score(_latency(_p95(slow_from_minutes=6, slow_until_minutes=-1)))

    assert breakdown["has_prometheus_signal"] == 1


def test_a_slowdown_that_ended_within_the_detection_lag_before_onset_is_eligible():
    _, breakdown = _score(_latency(_p95(slow_from_minutes=12, slow_until_minutes=3)))

    assert breakdown["has_prometheus_signal"] == 1


# 4. evidence entirely preceding the onset -------------------------------------------------------


def test_a_slowdown_that_ended_well_before_the_onset_is_rejected():
    _, breakdown = _score(_latency(_p95(slow_from_minutes=20, slow_until_minutes=12)))

    assert breakdown["has_prometheus_signal"] == 0


def test_the_rejected_measurement_says_why():
    measure = latency_tool.summarize_latency(_p95(slow_from_minutes=20, slow_until_minutes=12), onset=ONSET)

    assert measure["verdict"] == "elevated_before_onset_only"
    assert measure["historical_only"] is True
    assert measure["sustained_slow"] is False


# the same history, read for the incident it did cause --------------------------------------------


def test_the_same_history_supports_the_incident_that_began_while_the_fault_was_on():
    """One history, two incidents: the fault is a cause of the incident that began during it
    and history for the one that began after the revert."""
    during = datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc)
    after = datetime(2026, 10, 5, 19, 30, tzinfo=timezone.utc)
    markers = [
        {"service": "s", "timestamp": "2026-10-05T18:55:00+00:00", "rolled_back_from": None, **SLOW_ON},
        {"service": "s", "timestamp": "2026-10-05T19:10:00+00:00", "rolled_back_from": None, **HEALTHY},
    ]

    during_verdicts = [m["correlation"] for m in correlation.correlate_all(list(reversed(markers)), during)]
    after_verdicts = [m["correlation"] for m in correlation.correlate_all(list(reversed(markers)), after)]

    assert during_verdicts == ["after_incident", "before_incident"]
    assert after_verdicts == ["before_incident", "superseded"]
