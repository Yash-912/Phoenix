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

# How long before an incident's recorded start a symptom can have been under way
# and still be that incident's. The incident opens when its alert fires, and the
# alert needs a `for:` hold plus a rate window of its own, so the symptom starts
# a little before the incident does. A symptom that ended longer ago than this
# was over before the incident began: real evidence, but not this incident's.
ONSET_GRACE_SECONDS = 300

# Image tags that say "this marker did not change the image".
NEUTRAL_IMAGE_TAGS = frozenset({"", "same", "unknown"})


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


def ended_before_onset(last_seen: float, incident_started_at: datetime) -> bool:
    """Whether something last observed at `last_seen` (epoch seconds) was over before
    the incident began, allowing for the lag between a symptom starting and its alert
    opening the incident. The one place that answers it, so a measurement and a marker
    cannot disagree about what 'before the incident' means."""
    return last_seen < incident_started_at.timestamp() - ONSET_GRACE_SECONDS


def _state_keys(marker: dict) -> dict:
    """The pieces of service state a marker set: the image, and each config key.

    A marker is a record of one deployment, and what a deployment sets is exactly
    what it replaces -- so 'what is the service running' is the latest setter of
    each of these, not 'the latest marker'. A marker that did not change the image
    sets no image key, and a marker with no config sets no config keys.
    """
    keys: dict = {}
    tag = marker.get("image_tag")
    if isinstance(tag, str) and tag not in NEUTRAL_IMAGE_TAGS:
        keys[("image",)] = tag
    config = marker.get("config")
    if isinstance(config, dict):
        for name, value in config.items():
            keys[("config", name)] = value
    return keys


def _mark_superseded(annotated: list[dict], incident_started_at: datetime) -> None:
    """Re-label, in place, the in-window markers whose state a later marker replaced
    before the incident began.

    Only markers at or before the onset can have been the state in effect at onset,
    so only they decide what was. A key a marker set is replaced when a later marker
    set it to a *different* value; setting the same value again re-asserts it and
    replaces nothing, so a redeploy of the same image does not erase the marker that
    introduced it. A marker is superseded when every key it set was replaced: its
    fault, if it had one, was reverted before this incident started. One that still
    stands for at least one key is still in effect. A revert written after the onset
    is not considered; it is the fix, and the fault was in effect when the incident
    began.
    """
    dated = []
    for marker in annotated:
        deployed_at = parse_timestamp(marker.get("timestamp"))
        if deployed_at is not None and deployed_at <= incident_started_at:
            dated.append((deployed_at, marker))
    dated.sort(key=lambda pair: pair[0])

    for index, (_, marker) in enumerate(dated):
        keys = _state_keys(marker)
        if not keys or marker.get("correlation") != "before_incident":
            continue
        replacers = []
        for key, value in keys.items():
            replacer = next(
                (later for _, later in dated[index + 1:] if key in _state_keys(later) and _state_keys(later)[key] != value),
                None,
            )
            if replacer is None:
                break
            replacers.append(replacer)
        else:
            marker["correlation"] = "superseded"
            marker["in_window"] = False
            marker["superseded_by"] = max(replacers, key=lambda m: parse_timestamp(m["timestamp"]))["timestamp"]


def correlate_all(
    markers: list[dict],
    incident_started_at: datetime | None,
) -> list[dict]:
    """Newest-first markers, each annotated with its correlation verdict.

    Time alone does not make a marker a candidate cause. Besides its place relative
    to the onset, a marker is marked `superseded` when a later marker replaced
    everything it set before the incident began. With no known onset nothing is
    superseded: there is no moment to ask what was in effect at.
    """
    annotated = [correlate(marker, incident_started_at) for marker in markers]
    if incident_started_at is not None:
        _mark_superseded(annotated, incident_started_at)
    return annotated
