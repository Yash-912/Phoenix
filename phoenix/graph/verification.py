"""Whether the action Phoenix just took actually worked.

Two rules shape this module, and both come from the failure mode the whole
verification engine exists to avoid: an agent that reports recovery it did not
observe. The run that reports "recovered" on a container it could not check is
worse than the run that reports nothing, because a human stops looking.

**"Could not check" is a third answer, not a failed check.** Every path that
cannot obtain a signal returns INCONCLUSIVE, never pass. That is the same
direction scoring._is_usable errs in, and for the same reason: a failed
measurement making a guess look better-supported than no measurement at all is
the one direction an agent must never be wrong in. It is why the unusable check
below is shared with the scorer rather than reimplemented.

**The alert's window is the wrong window.** The memory-growth alert uses a 30m
deriv with a 10m for: on purpose, so one memory spike cannot trip it. Reusing
that for verification would mean waiting ~40 minutes to confirm a restart, which
is not a feature. A service that was just restarted is at baseline by definition,
so the question is not "is memory growing" but "did the resident set come down
from what it was seconds ago". That is a comparison against the snapshot
remediator_node took immediately before the action, over a short slope window to
catch a leak that starts again straight away.

Every PromQL here is authored by this module. Nothing in this file consults the
model, which is what keeps the observer's five read-only tools the whole read
surface: verification adds a new kind of question without adding a new tool.
"""

from phoenix.graph import scoring
from phoenix.tools.docker_tool import get_container_state
from phoenix.tools.health_tool import inspect_health
from phoenix.tools.prometheus_tool import query_prometheus

import math
from datetime import datetime, timezone

OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
OUTCOME_INCONCLUSIVE = "inconclusive"

# After a restart the working set must sit below this fraction of what it was
# before. Half is chosen to be a change no amount of ordinary traffic produces in
# the settling window, so a "pass" cannot be ordinary variance.
MEMORY_DROP_RATIO = 0.5

# Short window, unlike the alert's 30m: this is not deciding whether to page
# someone, it is asking whether the memory freed by the restart is staying freed.
# The threshold is the alert's own 1024 B/s, so a slope above it means the leak
# is back at the rate that would page a human, and the restart bought minutes
# rather than a fix.
SLOPE_WINDOW_MINUTES = 5
SLOPE_TOLERANCE = 1024


# The memory metric is process_resident_memory_bytes, not cAdvisor's
# container_memory_working_set_bytes. cAdvisor on Docker Desktop/WSL2 exposes only
# seven root-level cgroup series, labelled {__name__, id, instance, job}, with no
# name label on any of them -- so a name-scoped cAdvisor selector returns zero
# series here, and every overload check graded inconclusive regardless of what
# the container was doing. Measured, not assumed: this was the second place that
# inference went wrong, the first being the alert rule that reads the same signal.
#
# process_resident_memory_bytes is exported by prometheus_fastapi_instrumentator
# on each service's own /metrics, which Prometheus already scrapes. It is also a
# more honest measure for this check: the leak being verified is a growing Python
# dict in one process, so the process's resident set is the thing that grew.
#
# job= is the service name because prometheus.yml names each scrape job after
# its target. Scoping on it is load-bearing rather than incidental -- it is what
# makes "exactly one series" a real constraint instead of a formality, so the
# refusal in _single_sample keeps its meaning. Left unscoped, two services
# exporting the same metric name would match, and reading an arbitrary one of them
# is how a check ends up grading a restart on a series belonging to another service.
MEMORY_METRIC = "process_resident_memory_bytes"


def _memory_promql(service_name: str) -> str:
    return f'{MEMORY_METRIC}{{job="{service_name}"}}'


def _slope_promql(service_name: str) -> str:
    return f'deriv({MEMORY_METRIC}{{job="{service_name}"}}[{SLOPE_WINDOW_MINUTES}m])'


def _single_sample(payload) -> float | None:
    """The value of the one matching series, or None when there is not exactly one.

    The value arrives as a string because that is what Prometheus's JSON
    encodes, and a vector whose result list is empty has no sample at all --
    which is not the same as a sample of zero and must not be read as one.

    More than one matching series is refused rather than answered from the first.
    Reading result[0] would silently grade a restart on whichever series the
    query engine happened to order first, and a check that reports a confident
    verdict about an arbitrary series is worse than one that reports nothing.
    The selector is scoped by name and job precisely so this should not happen;
    if it does, something is wrong with the labels and saying so is the answer.
    """
    try:
        results = payload["data"]["result"]
    except (KeyError, TypeError):
        return None
    if not isinstance(results, list) or len(results) != 1:
        return None
    try:
        sample = results[0]["value"][1]
    except (KeyError, IndexError, TypeError):
        return None
    try:
        value = float(sample)
    except (TypeError, ValueError):
        return None
    # NaN is what Prometheus returns for a range query it cannot compute: deriv()
    # over fewer than two points, or a series with no value in range. A restart
    # empties the series, so this is the normal answer right after one. It is
    # not a number and must not be allowed to behave like one -- every
    # comparison against NaN is False, which would silently satisfy the
    # "is it holding steady" test and report a pass for an unmeasured slope.
    # +Inf and -Inf are different: they are real readings of a slope that is
    # genuinely unbounded, so they are kept and judged as the leak they are.
    if math.isnan(value):
        return None
    return value




def _as_instant(moment: str | None) -> datetime | None:
    """A timestamp as an aware datetime, or None when it cannot be read as one.

    Accepts what both producers actually write: Docker's RFC3339 with a Z, and
    isoformat() output with a numeric offset and microseconds. A timestamp with
    no timezone is refused rather than assumed to be UTC -- guessing the offset
    would reintroduce exactly the class of wrong answer this function exists to
    prevent, one layer further from where it is visible.
    """
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def read_signal(category: str, service_name: str) -> dict:
    """One reading of whatever proves or disproves this category.

    Returns either a usable payload or the failure envelope, and never raises.
    A tool that raises is turned into that envelope rather than propagated: this
    is called after a real action has run, and an exception here would unwind
    past the final state print and lose the record that the action happened at
    all -- the one thing the operator cannot reconstruct.
    """
    try:
        if category == "overload":
            return _read_memory(service_name)
        if category == "crash":
            return {
                "state": get_container_state(service_name),
                "health": inspect_health(service_name),
            }
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    return {
        "status": "error",
        "error": f"no verification check for category {category!r}",
    }


def _read_memory(service_name: str) -> dict:
    """Working set now, and its short-window slope, or the failure envelope.

    Both are needed and they answer different questions. The level says the
    restart freed anything; the slope says the freed memory is staying freed. A
    level check alone passes on a container that dipped and is already climbing
    back, which is the leak scenario this phase is built to demonstrate.
    """
    level = query_prometheus(_memory_promql(service_name))
    if not scoring._is_usable({"raw_data": level}):
        return {"status": "error", "error": "the working-set query did not return data"}

    slope = query_prometheus(_slope_promql(service_name))
    if not scoring._is_usable({"raw_data": slope}):
        return {"status": "error", "error": "the working-set slope query did not return data"}

    bytes_now = _single_sample(level)
    slope_value = _single_sample(slope)

    if bytes_now is None:
        return {"status": "error", "error": "the working-set vector carried no sample"}
    if slope_value is None:
        return {"status": "error", "error": "the working-set slope vector carried no sample"}

    return {"bytes": bytes_now, "slope": slope_value}


def run_check(
    category: str,
    service_name: str,
    before: dict,
    action_at: str | None,
) -> tuple[str, dict]:
    """Judge the signal against the snapshot taken before the action.

    Returns (outcome, detail). outcome is one of pass, fail, or inconclusive,
    and inconclusive is the honest answer whenever the signal could not be read
    -- never a pass, and never a silent absence.
    """
    signal = read_signal(category, service_name)

    if not scoring._is_usable({"raw_data": signal}):
        return OUTCOME_INCONCLUSIVE, {
            "check": category,
            "reason": (
                f"the signal for {category} could not be read, so whether the "
                f"action worked is unknown: {signal.get('error', 'no data')}"
            ),
            "signal": signal,
        }

    if category == "overload":
        return _check_overload(signal, before or {})
    if category == "crash":
        return _check_crash(signal, action_at)

    return OUTCOME_INCONCLUSIVE, {
        "check": category,
        "reason": f"no verification check for category {category!r}",
    }


def _check_overload(signal: dict, before: dict) -> tuple[str, dict]:
    """Did the working set come down, and is it staying down?"""
    detail = {
        "check": "overload",
        "before": before.get("bytes"),
        "after": signal.get("bytes"),
        "slope": signal.get("slope"),
    }

    before_bytes = before.get("bytes")
    if before_bytes is None:
        return OUTCOME_INCONCLUSIVE, {
            **detail,
            "reason": (
                "no pre-action working set was captured, so there is nothing to "
                "compare the current reading against"
            ),
        }

    if signal["bytes"] >= before_bytes * MEMORY_DROP_RATIO:
        return OUTCOME_FAIL, {
            **detail,
            "reason": (
                f"working set is {int(signal['bytes'])} against a pre-action "
                f"{int(before_bytes)}, which is not the drop a restart should "
                f"produce"
            ),
        }

    if signal["slope"] > SLOPE_TOLERANCE:
        return OUTCOME_FAIL, {
            **detail,
            "reason": (
                f"working set did come down to {int(signal['bytes'])} but its "
                f"slope over {SLOPE_WINDOW_MINUTES}m is {signal['slope']:.0f} B/s, "
                f"above the {SLOPE_TOLERANCE} B/s the alert would page on, so the "
                f"restart bought time rather than a fix"
            ),
        }

    return OUTCOME_PASS, {
        **detail,
        "reason": (
            f"working set fell from {int(before_bytes)} to {int(signal['bytes'])} "
            f"and is not climbing"
        ),
    }


def _check_crash(signal: dict, action_at: str | None) -> tuple[str, dict]:
    """Is it running, did THIS action restart it, and is it answering?

    The start time is what separates a restart that worked from a container that
    happened to be up already. Without it a check would pass on a service that
    was never touched, and the agent would credit itself with a fix it did not
make.
    """
    state = signal.get("state", {})
    health = signal.get("health", {})
    container_state = state.get("State", {}) if isinstance(state, dict) else {}
    started_at = container_state.get("StartedAt")
    detail = {
        "check": "crash",
        "status": container_state.get("Status"),
        "started_at": started_at,
    }

    if not started_at:
        return OUTCOME_INCONCLUSIVE, {
            **detail,
            "reason": (
                "the container's start time could not be read, so there is no way "
                "to tell whether this action restarted it"
            ),
        }

    # Both sides are parsed into datetimes rather than compared as strings.
    # Docker writes StartedAt as RFC3339 with trailing zeros trimmed
    # ("2026-09-30T10:00:00.123Z") and the remediator writes action_at through
    # isoformat(), which keeps the microseconds and an offset
    # ("2026-09-30T10:00:00.123456+00:00"). As strings those two are not
    # comparable at all: 'Z' is 0x5A and '+' is 0x2B, so a container that
    # started microseconds BEFORE the action sorts as though it started after
    # it, and this check passes on a container the action never touched.
    # A naive timestamp would be worse than either: it would compare wall-clock
    # labels across offsets, so it is treated as unreadable instead.
    started_moment = _as_instant(started_at)
    action_moment = _as_instant(action_at)
    if started_moment is None:
        return OUTCOME_INCONCLUSIVE, {
            **detail,
            "reason": (
                f"the container's start time {started_at!r} could not be read as a "
                f"timestamp, so there is no way to tell whether this action "
                f"restarted it"
            ),
        }
    if action_moment is None:
        return OUTCOME_INCONCLUSIVE, {
            **detail,
            "reason": (
                f"the action time {action_at!r} could not be read as a timestamp, so "
                f"there is no way to tell whether this action restarted the container"
            ),
        }

    if started_moment < action_moment:
        return OUTCOME_FAIL, {
            **detail,
            "reason": (
                f"the container started at {started_at}, before the action was "
                f"taken at {action_at}, so it was already running and the action "
                f"is not what made it healthy"
            ),
        }

    if container_state.get("Status") != "running":
        return OUTCOME_FAIL, {
            **detail,
            "reason": f"the container is {container_state.get('Status')!r}, not running",
        }
  
    app = health.get("app", {}) if isinstance(health, dict) else {}
    app_status = app.get("status")
    if app_status != "ok":
        # A probe that answered "I am unhealthy" and a probe that could not be
        # reached are both non-ok, but only one of them is evidence about the
        # service. inspect_health writes {"status": "error", "error": ...} when
        # the request raises, and reading that as an unhealthy service would
        # grade a restart as failed because an HTTP call timed out -- then loop
        # back and spend another attempt on a check that never ran. The design
        # puts "/health timed out" under signal unobtainable, not under a failed
        # check, so the error envelope is inconclusive like every other
        # unreadable signal.
        if app_status == "error":
            return OUTCOME_INCONCLUSIVE, {
                **detail,
                "reason": (
                    f"the container restarted at {started_at} and is running, but "
                    f"its health probe could not be reached "
                    f"({app.get('error', 'no detail')}), so whether the service is "
                    f"serving is unknown"
                ),
            }
        return OUTCOME_FAIL, {
            **detail,
            "reason": (
                f"the container is running but its health probe reports "
                f"{app_status!r}, so the service is not serving"
            ),
        }

    return OUTCOME_PASS, {
        **detail,
        "reason": (
            f"the container restarted at {started_at}, after the action, and is "
            f"running with a healthy app probe"
        ),
    }

