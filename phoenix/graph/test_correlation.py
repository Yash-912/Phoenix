"""Temporal correlation between an incident and a deployment.

The property that matters most here is direction. A deployment *after* an
incident is usually the response to it, and reading that as causation is how an
agent reverts the thing that was fixing the outage.
"""

from datetime import datetime, timedelta, timezone

import pytest

from phoenix.graph.correlation import (
    CORRELATION_WINDOW_SECONDS,
    correlate,
    correlate_all,
    parse_timestamp,
)

DEPLOYED_AT = datetime(2026, 10, 2, 8, 0, 0, tzinfo=timezone.utc)


def _marker(offset_seconds: float = 0) -> dict:
    when = (DEPLOYED_AT + timedelta(seconds=offset_seconds)).isoformat()
    return {"service": "checkout-service", "timestamp": when, "image_tag": "v18"}


def _incident(offset_seconds: float) -> datetime:
    return DEPLOYED_AT + timedelta(seconds=offset_seconds)


def test_a_deploy_just_before_an_incident_is_in_the_window():
    result = correlate(_marker(), _incident(60))

    assert result["correlation"] == "before_incident"
    assert result["in_window"] is True
    assert result["delta_seconds"] == pytest.approx(-60)


def test_a_deploy_at_the_same_instant_as_the_incident_counts_as_before_it():
    """Treated as a candidate rather than excluded on a knife edge: an incident
    raised from a deploy's own health check lands on the same second."""
    result = correlate(_marker(), _incident(0))

    assert result["in_window"] is True


def test_a_deploy_after_the_incident_is_not_causal():
    """The rollback itself lands here. Reading it as the cause would make the
    agent undo its own fix, and then undo that."""
    result = correlate(_marker(offset_seconds=300), _incident(0))

    assert result["correlation"] == "after_incident"
    assert result["in_window"] is False


def test_a_deploy_long_before_the_incident_is_outside_the_window():
    result = correlate(_marker(), _incident(CORRELATION_WINDOW_SECONDS * 4))

    assert result["correlation"] == "too_far_before"
    assert result["in_window"] is False


def test_the_window_edge_is_inclusive():
    result = correlate(_marker(), _incident(CORRELATION_WINDOW_SECONDS))

    assert result["in_window"] is True


def test_an_unknown_incident_time_is_a_gap_rather_than_a_negative_finding():
    """Reporting 'no correlation' for a missing clock would read as a conclusion."""
    result = correlate(_marker(), None)

    assert result["correlation"] == "incident_time_unknown"
    assert result["in_window"] is False
    assert result["delta_seconds"] is None


def test_an_unreadable_deploy_time_is_named_rather_than_silently_dropped():
    result = correlate({"timestamp": "not a time", "image_tag": "v18"}, _incident(0))

    assert result["correlation"] == "deploy_time_unreadable"
    assert result["in_window"] is False


def test_the_original_marker_survives_annotation():
    """The correlation is added to the deployment record, not substituted for it."""
    marker = _marker()
    result = correlate(marker, _incident(60))

    assert result["image_tag"] == "v18"
    assert result["timestamp"] == marker["timestamp"]


@pytest.mark.parametrize(
    "raw",
    [
        "2026-10-02T08:00:00Z",
        "2026-10-02T08:00:00.123456+00:00",
        "2026-10-02T10:00:00+02:00",
    ],
)
def test_both_timestamp_formats_in_the_system_are_read(raw):
    """Docker writes RFC3339 with Z; the marker writers use isoformat() with an
    offset. Both occur on the same comparison."""
    parsed = parse_timestamp(raw)

    assert parsed is not None
    assert parsed.tzinfo is not None


def test_offsets_are_normalised_rather_than_compared_as_text():
    """10:00+02:00 and 08:00Z are the same instant. As strings they are three
    hours apart, which would put the deployment outside any window."""
    assert parse_timestamp("2026-10-02T10:00:00+02:00") == parse_timestamp("2026-10-02T08:00:00Z")


def test_a_naive_timestamp_is_treated_as_utc_rather_than_refused():
    """Docstring and behaviour have to agree: markers are written by this repo
    with an aware clock, so a naive value can only come from a hand-edited file.
    Rejecting it would silently drop a real deployment."""
    parsed = parse_timestamp("2026-10-02T08:00:00")

    assert parsed is not None
    assert parsed == DEPLOYED_AT


@pytest.mark.parametrize("raw", [None, "", "yesterday", 17, {}])
def test_an_unparseable_timestamp_is_none_rather_than_an_exception(raw):
    assert parse_timestamp(raw) is None


def test_correlating_a_whole_history_annotates_each_entry_in_order():
    markers = [_marker(offset_seconds=300), _marker(), _marker(offset_seconds=-300)]

    annotated = correlate_all(markers, _incident(0))

    assert [m["correlation"] for m in annotated] == [
        "after_incident",
        "before_incident",
        "before_incident",
    ]


def test_correlation_never_decides_anything_by_itself():
    """A perfectly correlated deployment is still only evidence. Nothing in this
    module returns a verdict about the incident."""
    result = correlate(_marker(), _incident(30))

    assert set(result) == {"service", "timestamp", "image_tag", "correlation", "delta_seconds", "in_window"}
    assert "remediation" not in result and "verdict" not in result
