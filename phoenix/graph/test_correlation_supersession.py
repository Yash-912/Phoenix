"""A marker describes the state a deployment put the service in. Once a later
marker replaced that state -- before the incident began -- the earlier marker is
history, not a cause: it is not what the service was running at onset.

Real evidence is not current evidence. The reset script reverts a fault without
touching the old marker, so a stale 'slow_query: true' marker kept looking like
the active configuration for an hour afterwards."""

from datetime import datetime, timedelta, timezone

from phoenix.graph import correlation

ONSET = datetime(2026, 10, 5, 19, 9, 25, tzinfo=timezone.utc)


def _marker(minutes_before_onset: float, image_tag: str = "same", config: dict | None = None, **fields) -> dict:
    when = ONSET - timedelta(minutes=minutes_before_onset)
    return {"service": "payment-service", "timestamp": when.isoformat(), "image_tag": image_tag,
            "config": config or {}, "rolled_back_from": None, "git_commit": "x", **fields}


def _verdicts(*oldest_first: dict, onset=ONSET) -> list[str]:
    """The correlation verdict of each marker, oldest first, given newest-first input."""
    annotated = correlation.correlate_all(list(reversed(oldest_first)), onset)
    return [m["correlation"] for m in reversed(annotated)]


SLOW_ON = {"image_tag": "slow-query", "config": {"slow_query": True}}
HEALTHY = {"image_tag": "healthy", "config": {"slow_query": False}}


def test_a_fault_marker_followed_by_its_revert_before_onset_is_superseded():
    assert _verdicts(_marker(26, **SLOW_ON), _marker(20, **HEALTHY)) == ["superseded", "before_incident"]


def test_a_fault_marker_with_no_revert_is_still_the_state_in_effect():
    assert _verdicts(_marker(26, **SLOW_ON)) == ["before_incident"]


def test_a_revert_written_after_onset_does_not_supersede_the_cause():
    """The fault was in effect when the incident began; undoing it later is the fix, not history."""
    assert _verdicts(_marker(10, **SLOW_ON), _marker(-5, **HEALTHY)) == ["before_incident", "after_incident"]


def test_a_config_value_replaced_by_a_later_value_is_superseded():
    pool_down = _marker(30, config={"DB_POOL_SIZE": "1"})
    pool_up = _marker(20, config={"DB_POOL_SIZE": "10"})

    assert _verdicts(pool_down, pool_up) == ["superseded", "before_incident"]


def test_a_deploy_reverted_by_a_rollback_is_superseded():
    bad = _marker(30, image_tag="v18")
    good = _marker(20, image_tag="v17", rolled_back_from="bad.json")

    assert _verdicts(bad, good) == ["superseded", "before_incident"]


def test_a_later_marker_that_changes_something_else_does_not_supersede():
    """Replacing one setting leaves the other in effect."""
    slow = _marker(30, **SLOW_ON)
    unrelated = _marker(20, config={"DB_POOL_SIZE": "1"})

    assert _verdicts(slow, unrelated) == ["before_incident", "before_incident"]


def test_a_marker_that_still_sets_one_live_key_is_not_superseded():
    both = _marker(30, image_tag="v18", config={"FLAG": "on"})
    only_image = _marker(20, image_tag="v19")

    assert _verdicts(both, only_image) == ["before_incident", "before_incident"]


def test_a_marker_that_sets_nothing_is_never_called_superseded():
    noop = _marker(30)
    later = _marker(20, **HEALTHY)

    assert _verdicts(noop, later) == ["before_incident", "before_incident"]


def test_an_unknown_incident_time_gives_no_supersession():
    verdicts = _verdicts(_marker(26, **SLOW_ON), _marker(20, **HEALTHY), onset=None)

    assert verdicts == ["incident_time_unknown", "incident_time_unknown"]


def test_a_marker_already_too_far_before_keeps_that_verdict():
    assert _verdicts(_marker(300, **SLOW_ON), _marker(20, **HEALTHY)) == ["too_far_before", "before_incident"]


def test_a_superseded_marker_is_out_of_the_correlation_window():
    annotated = correlation.correlate_all(
        [_marker(20, **HEALTHY), _marker(26, **SLOW_ON)], ONSET
    )

    assert annotated[1]["correlation"] == "superseded"
    assert annotated[1]["in_window"] is False
    assert annotated[0]["in_window"] is True


def test_the_marker_that_superseded_is_named():
    annotated = correlation.correlate_all([_marker(20, **HEALTHY), _marker(26, **SLOW_ON)], ONSET)

    assert annotated[1]["superseded_by"] == annotated[0]["timestamp"]


def test_a_redeploy_of_the_same_image_does_not_erase_the_marker_that_introduced_it():
    """The regression was introduced by the first v18 marker; deploying v18 again changed nothing."""
    first = _marker(30, image_tag="v18")
    again = _marker(20, image_tag="v18")

    assert _verdicts(first, again) == ["before_incident", "before_incident"]


def test_a_fault_applied_again_after_being_reverted_is_current_and_the_first_is_history():
    first = _marker(40, **SLOW_ON)
    revert = _marker(30, **HEALTHY)
    again = _marker(20, **SLOW_ON)

    assert _verdicts(first, revert, again) == ["superseded", "superseded", "before_incident"]


def test_the_marker_that_replaced_it_is_named_not_one_that_merely_repeated_it():
    first = _marker(40, **SLOW_ON)
    repeat = _marker(30, **SLOW_ON)
    revert = _marker(20, **HEALTHY)

    annotated = correlation.correlate_all([revert, repeat, first], ONSET)

    assert annotated[2]["superseded_by"] == revert["timestamp"]
