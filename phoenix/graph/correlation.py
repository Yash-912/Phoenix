"""Does this incident line up with a deployment, in time?

The PRD asks for the discriminating question rather than a guess: given several
candidate causes, find the evidence that separates them. For a deploy cause
that evidence is temporal -- an incident that began right after a release is
much more likely to be that release than an incident that predates it by days.
This module answers that question from two facts Phoenix already has: when the
incident was first seen, and when each deployment happened.

**Direction matters more than proximity.** A deployment *before* the incident is
the only one that can explain it. A deployment after an incident that was
already firing is almost certainly a response to it, and treating that as
causation is how an agent rolls back the wrong thing during an outage -- the
human or pipeline fixing the incident gets reverted and the original cause
stays. So a deployment later than the incident is reported as `after_incident`
rather than quietly excluded, and only `before_incident` counts as support.

**Correlation is evidence, never permission.** Nothing here decides to roll
anything back. It annotates a deployment marker with how it relates to the
incident, and the diagnosis still has to be proposed, scored, routed, and
verified on its own. A deployment that correlates perfectly is still only worth
acting on if the rest of the evidence agrees.
"""

from __future__ import annotations

from datetime import datetime, timezone

# How long before an incident a deployment still counts as a plausible cause.
# Generous on purpose: a release that lands in a pipeline can take a while to
# reach every instance, and an incident firing well after the deploy is still
# that deploy's fault. Being too tight here would silently discard the true
# cause, which is the worse error -- the agent would then have evidence for a
# cause it cannot confirm and no way to say why.
CORRELATION_WINDOW_SECONDS = 3600


def parse_timestamp(value) -> datetime | None:
    """An aware datetime, or None when the value cannot be read as one.

    Accepts the two forms in play: docker's RFC3339 with a Z, and isoformat()
    output with an offset. A naive timestamp is refused rather than assumed UTC
    -- the same reasoning verification._as_instant uses, and for the same
    reason: a guessed offset puts the correlation window in the wrong place,
    which is indistinguishable from no correlation at all.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def correlate(marker: dict, incident_started_at: datetime | None) -> dict:
    """How one deployment relates to the incident's onset, as a verdict dict.

    Returns the marker annotated with `correlation`, `delta_seconds`, and
    `in_window`. A missing or unreadable incident time yields
    `incident_time_unknown` rather than a negative answer: not knowing when the
    incident began is a gap in the evidence, and reporting it as "no
    correlation" would read as a finding.
    """
    deployed_at = parse_timestamp(marker.get("timestamp"))

    if incident_started_at is None:
        return {
            **marker,
            "correlation": "incident_time_unknown",
            "delta_seconds": None,
            "in_window": False,
        }
    if deployed_at is None:
        return {
            **marker,
            "correlation": "deploy_time_unreadable",
            "delta_seconds": None,
            "in_window": False,
        }

    # Positive means the deployment came after the incident started.
    delta = (deployed_at - incident_started_at).total_seconds()

    if delta > 0:
        verdict = "after_incident"
        in_window = False
    elif abs(delta) <= CORRELATION_WINDOW_SECONDS:
        verdict = "before_incident"
        in_window = True
    else:
        verdict = "too_far_before"
        in_window = False

    return {
        **marker,
        "correlation": verdict,
        "delta_seconds": delta,
        "in_window": in_window,
    }


def correlate_all(
    markers: list[dict],
    incident_started_at: datetime | None,
) -> list[dict]:
    """Newest-first markers, each annotated with its correlation verdict."""
    return [correlate(marker, incident_started_at) for marker in markers]
