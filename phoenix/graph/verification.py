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

**A deploy is verified by identity and behaviour, never by liveness.** The
service's own /health endpoint reports "ok" in both artifacts, because a release
that rejects every order is still a perfectly live process. A check that trusted
it would grade the broken release healthy and the rollback as having changed
nothing. So the deploy check asks two things liveness cannot: is the running
container the artifact we meant to restore, and does the business endpoint work.

**Both are required, because either alone is forgeable by accident.** The
container label says which image is running; the HTTP response says the process
inside it behaves correctly. If only the label were checked, a redeploy that
started the right tag but wedged before serving would pass. If only behaviour
were checked, a healthy service that happened to recover on its own would pass
and be credited to the rollback.

Every PromQL here is authored by this module. Nothing in this file consults the
model, which is what keeps the observer's five read-only tools the whole read
surface: verification adds a new kind of question without adding a new tool.
"""

from phoenix.graph import scoring
from phoenix.tools.docker_tool import get_container_state
from phoenix.tools.health_tool import inspect_health
from phoenix.tools.prometheus_tool import query_prometheus

import math
import time
from datetime import datetime, timezone

import requests

# Categories whose Tier 1 action is verified by the resident-memory check. A
# memory_leak is a growing working set exactly as overload's memory case is, so
# the question after a restart is the same: did it come down and stay down.
# Keyed on what was wrong, not on which hypothesis family named it.
MEMORY_CATEGORIES = ("overload", "memory_leak")

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


def _slope_promql(service_name: str, window_seconds: int | None = None) -> str:
    window = f"{window_seconds}s" if window_seconds else f"{SLOPE_WINDOW_MINUTES}m"
    return f'deriv({MEMORY_METRIC}{{job="{service_name}"}}[{window}])'


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


def read_signal(category: str, service_name: str, window_seconds: int | None = None) -> dict:
    """One reading of whatever proves or disproves this category.

    Returns either a usable payload or the failure envelope, and never raises.
    A tool that raises is turned into that envelope rather than propagated: this
    is called after a real action has run, and an exception here would unwind
    past the final state print and lose the record that the action happened at
    all -- the one thing the operator cannot reconstruct.
    """
    try:
        if category in MEMORY_CATEGORIES:
            if window_seconds is None:
                return _read_memory(service_name)
            return _read_memory(service_name, window_seconds)
        if category == "crash":
            return {
                "state": get_container_state(service_name),
                "health": inspect_health(service_name),
            }
        if category == "deploy":
            return _read_deploy(service_name)
        if category == "config":
            return _read_config(service_name)
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    return {
        "status": "error",
        "error": f"no verification check for category {category!r}",
    }


def _read_memory(service_name: str, window_seconds: int | None = None) -> dict:
    """Working set now, and its short-window slope, or the failure envelope.

    Both are needed and they answer different questions. The level says the
    restart freed anything; the slope says the freed memory is staying freed. A
    level check alone passes on a container that dipped and is already climbing
    back, which is the leak scenario this phase is built to demonstrate.
    """
    level = query_prometheus(_memory_promql(service_name))
    if not scoring._is_usable({"raw_data": level}):
        return {"status": "error", "error": "the working-set query did not return data"}

    slope = query_prometheus(_slope_promql(service_name, window_seconds))
    if not scoring._is_usable({"raw_data": slope}):
        return {"status": "error", "error": "the working-set slope query did not return data"}

    bytes_now = _single_sample(level)
    slope_value = _single_sample(slope)

    if bytes_now is None:
        return {"status": "error", "error": "the working-set vector carried no sample"}
    if slope_value is None:
        return {"status": "error", "error": "the working-set slope vector carried no sample"}

    return {"bytes": bytes_now, "slope": slope_value, "window_seconds": window_seconds}


# Where each service's business endpoint lives. A fixed map rather than a
# convention, because "guess a port from the service name" would send traffic at
# whatever happens to be listening and read someone else's answer as this
# service's. Absence is treated as an unreadable signal, never as a pass.
BUSINESS_ENDPOINTS = {
    "checkout-service": "http://localhost:8001/checkout",
}

# A redeploy needs a moment before the new process answers. Bounded rather than
# retried indefinitely: an endpoint still failing after this is a real failure,
# not a slow start, and retrying longer would only delay saying so.
DEPLOY_SETTLE_SECONDS = 5
DEPLOY_SETTLE_ATTEMPTS = 12


def _probe_business_endpoint(service_name: str) -> dict:
    """Call the service's real endpoint and report what came back.

    A 500 is data, not an exception: it is precisely the failure being verified,
    so it is returned as an observation. Only an unreachable endpoint is an
    unreadable signal.
    """
    url = BUSINESS_ENDPOINTS.get(service_name)
    if not url:
        return {"status": "error", "error": f"no business endpoint known for {service_name!r}"}

    last_error = None
    for _ in range(DEPLOY_SETTLE_ATTEMPTS):
        try:
            response = requests.get(url, timeout=5)
            return {
                "status": "ok",
                "url": url,
                "http_status": response.status_code,
                "body": _truncate_body(response.text),
                "serving": response.status_code < 500,
            }
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(DEPLOY_SETTLE_SECONDS)

    return {"status": "error", "url": url, "error": str(last_error)}


def _truncate_body(text: str, limit: int = 300) -> str:
    """First `limit` characters of a response body, for the audit trail.

    An error page can be arbitrarily large and the trail does not need all of
    it; the first line is what identifies the failure.
    """
    return text[:limit]


def _read_deploy(service_name: str) -> dict:
    """Which artifact is running, and whether it actually serves, or the envelope."""
    state = get_container_state(service_name)
    if not isinstance(state, dict) or state.get("status") == "error":
        return {"status": "error", "error": "the container state could not be read"}

    labels = (state.get("Config") or {}).get("Labels") or {}
    running_version = labels.get("app.version")
    if not running_version:
        return {
            "status": "error",
            "error": "the running container carries no app.version label, so its identity is unknown",
        }

    return {
        "status": "ok",
        "running_version": running_version,
        "image_digest": state.get("Image"),
        "endpoint": _probe_business_endpoint(service_name),
    }


# Each configurable service's key name (as used across the policy/dispatch/tool
# boundary) and the field its own /health JSON reports that key under. Kept
# separate from remediation_policy.CONFIG_KEYS_BY_SERVICE /
# CONFIG_HEALTH_FIELDS -- that module reads the same field to decide whether a
# rollback is still needed, this one reads it to decide whether the rollback it
# took worked, and the two questions are allowed to diverge even though today's
# answer is the same field.
CONFIG_SIGNALS: dict[str, dict[str, str]] = {
    "auth-service": {"key": "DB_POOL_SIZE", "field": "db_pool_size"},
}


def _read_config(service_name: str) -> dict:
    """The live value of the one config key this service is verified on, or the
    failure envelope.

    Read from the service's own /health response, not from docker or from
    compose: a config value is a property of the running process, and only the
    process can say what it actually believes is configured. DB_POOL_SIZE in
    particular is too fragile to probe by opening a database connection
    ourselves here, so this reads the number the process reports of itself.
    """
    spec = CONFIG_SIGNALS.get(service_name)
    if spec is None:
        return {
            "status": "error",
            "error": f"no config signal known for {service_name!r}",
        }

    health = inspect_health(service_name)
    app = health.get("app") if isinstance(health, dict) else None
    if not isinstance(app, dict) or app.get("status") != "ok":
        return {"status": "error", "error": "the health probe did not return data"}

    value = app.get(spec["field"])
    if value is None:
        return {
            "status": "error",
            "error": f"the health probe carried no {spec['field']!r}",
        }

    return {"status": "ok", "key": spec["key"], "value": str(value)}


# A slope needs at least two scrapes, and the scrape interval is 15s, so the
# window it is measured over must hold two post-action samples.
MIN_POST_ACTION_SECONDS = 90
SLOPE_WINDOW_MARGIN_SECONDS = 5

# A freshly restarted process does not hold a perfectly flat working set: the
# allocator moves in steps of a few hundred KiB, which a regression over a
# window of a minute or two reads as tens of KB/s. SLOPE_TOLERANCE is the
# paging threshold for a 30m window and is orders of magnitude tighter than
# that noise, so applied to a post-action window it fails a restart that
# worked. What the check is really asking is whether the freed memory is
# coming back, i.e. whether the process grew by more than allocator noise over
# the window -- so the tolerance is the larger of the paging threshold and a
# 1 MiB growth budget spread across the window. A real leak (megabytes over the
# same window) is still well outside it.
NOISE_FLOOR_BYTES = 1024 * 1024


def _post_action_window(action_at: str | None) -> int | None:
    """Seconds of history after the action to measure the slope over, or None.

    The default slope window is 5m, which reaches back past the action: after a
    restart it still contains the leak's own ramp from before it, and a
    regression over a ramp-then-drop reads as a rising slope on a process that
    is in fact flat. The slope that answers "is the freed memory staying
    freed" can only come from samples taken after the action, so the window is
    the time elapsed since it (less a small margin so the pre-action sample
    cannot fall inside), capped at the default. If too little time has passed
    for two scrapes to exist, this waits rather than reading a window with one
    point in it. No usable action time means no basis for a window, so the
    caller falls back to the default rather than guessing.
    """
    moment = _as_instant(action_at)
    if moment is None:
        return None
    elapsed = (datetime.now(timezone.utc) - moment).total_seconds()
    if elapsed < MIN_POST_ACTION_SECONDS:
        time.sleep(MIN_POST_ACTION_SECONDS - elapsed)
        elapsed = MIN_POST_ACTION_SECONDS
    return int(min(elapsed - SLOPE_WINDOW_MARGIN_SECONDS, SLOPE_WINDOW_MINUTES * 60))


def run_check(
    category: str,
    service_name: str,
    before: dict,
    action_at: str | None,
    expect_version: str | None = None,
) -> tuple[str, dict]:
    """Judge the signal against the snapshot taken before the action.

    Returns (outcome, detail). outcome is one of pass, fail, or inconclusive,
    and inconclusive is the honest answer whenever the signal could not be read
    -- never a pass, and never a silent absence.
    """
    window_seconds = None
    if category in MEMORY_CATEGORIES:
        window_seconds = _post_action_window(action_at)
    if window_seconds is None:
        signal = read_signal(category, service_name)
    else:
        signal = read_signal(category, service_name, window_seconds)

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
        outcome, detail = _check_overload(signal, before or {})
        if outcome != OUTCOME_PASS:
            return outcome, detail
        return _confirm_overload_symptom(service_name, detail)
    if category in MEMORY_CATEGORIES:
        return _check_overload(signal, before or {})
    if category == "crash":
        return _check_crash(signal, action_at)
    if category == "deploy":
        return _check_deploy(signal, expect_version)
    if category == "config":
        return _check_config(signal, expect_version)

    return OUTCOME_INCONCLUSIVE, {
        "check": category,
        "reason": f"no verification check for category {category!r}",
    }


def _check_config(signal: dict, expect_value: str | None) -> tuple[str, dict]:
    """Does the service now report the value the rollback was supposed to set?

    Read-and-compare only, the same shape _check_deploy uses for identity: the
    signal already is the live answer, so there is nothing before/after to
    reconcile the way the memory check does. A config rollback either stuck or
    it did not, and the service's own report is the one source that can say so.
    """
    detail = {
        "check": "config",
        "key": signal.get("key"),
        "reported_value": signal.get("value"),
        "expected_value": expect_value,
    }

    if expect_value is None:
        return OUTCOME_INCONCLUSIVE, {
            **detail,
            "reason": (
                "no target value was recorded for this action, so there is "
                "nothing to confirm the service reports"
            ),
        }

    if signal.get("value") != expect_value:
        return OUTCOME_FAIL, {
            **detail,
            "reason": (
                f"the service reports {signal.get('key')}={signal.get('value')!r}, "
                f"not the {expect_value!r} the rollback set, so the config was not "
                f"restored"
            ),
        }

    return OUTCOME_PASS, {
        **detail,
        "reason": (
            f"the service reports {signal.get('key')}={expect_value!r}, matching "
            f"what the rollback set"
        ),
    }


def _check_deploy(signal: dict, expect_version: str | None) -> tuple[str, dict]:
    """Is the expected artifact running, and does it serve?

    Identity first: a redeploy that left the old image running has not rolled
    anything back, and reporting that as a pass would be the worst outcome in
    this module -- it would claim a recovery that did not happen. Behaviour
    second, since a correctly-tagged container that never came up serving is
    equally not a recovery.
    """
    running_version = signal.get("running_version")
    endpoint = signal.get("endpoint") or {}
    detail = {
        "check": "deploy",
        "running_version": running_version,
        "expected_version": expect_version,
        "image_digest": signal.get("image_digest"),
        "http_status": endpoint.get("http_status"),
        "endpoint_body": endpoint.get("body"),
    }

    if expect_version is None:
        return OUTCOME_INCONCLUSIVE, {
            **detail,
            "reason": (
                "no target version was recorded for this action, so there is no "
                "artifact to confirm is running"
            ),
        }

    if running_version != expect_version:
        return OUTCOME_FAIL, {
            **detail,
            "reason": (
                f"the running container reports {running_version!r}, not the "
                f"{expect_version!r} the rollback deployed, so the artifact was "
                f"not replaced"
            ),
        }

    if not endpoint.get("serving"):
        return OUTCOME_FAIL, {
            **detail,
            "reason": (
                f"{expect_version} is running but its business endpoint returned "
                f"HTTP {endpoint.get('http_status')}, so the service still rejects "
                f"traffic"
            ),
        }

    return OUTCOME_PASS, {
        **detail,
        "reason": (
            f"the running container reports {expect_version} and its business "
            f"endpoint answered HTTP {endpoint.get('http_status')}, so the bad "
            f"release was genuinely reverted"
        ),
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

    window = signal.get("window_seconds")
    tolerance = max(SLOPE_TOLERANCE, NOISE_FLOOR_BYTES / window) if window else SLOPE_TOLERANCE
    if signal["slope"] > tolerance:
        return OUTCOME_FAIL, {
            **detail,
            "reason": (
                f"working set did come down to {int(signal['bytes'])} but its "
                f"slope over the {window or SLOPE_WINDOW_MINUTES * 60}s after the action "
                f"is {signal['slope']:.0f} B/s, above the {tolerance:.0f} B/s this "
                f"window allows, so the restart bought time rather than a fix"
            ),
        }

    return OUTCOME_PASS, {
        **detail,
        "reason": (
            f"working set fell from {int(before_bytes)} to {int(signal['bytes'])} "
            f"and is not climbing"
        ),
    }


def _read_overload_symptom(service_name: str) -> dict:
    """The service's 5xx ratio and p95 latency as of now: present, absent or unknown.

    Imported here and not at the top: the tools behind it import this module for the
    memory constants, so a module-level import would be a cycle. Memory is left out on
    purpose; _check_overload has already judged it, and a restart empties it either way.
    """
    from phoenix.graph import symptom

    return symptom.overload_symptom(service_name, signals=("error_rate", "latency"))


def _confirm_overload_symptom(service_name: str, memory_detail: dict) -> tuple[str, dict]:
    """A restart that freed memory has proved the restart, not the recovery.

    Overload is the service failing requests or answering slowly. The working set comes
    down after any restart, so a pass on memory alone credits a restart with a fix it
    may not have made -- a service that is still returning 5xx was reported resolved.
    The check passes only when the 5xx ratio and the p95 latency are both observed gone.
    Either still present fails it, and a reading that cannot be had (no traffic, a failed
    read) is inconclusive: this module reports only a recovery it saw.
    """
    try:
        observed = _read_overload_symptom(service_name)
    except Exception as exc:  # noqa: BLE001 - an unreadable symptom is a gap, not a crash
        observed = {"state": "unknown", "signals": {}, "error": f"{type(exc).__name__}: {exc}"}
    detail = {**memory_detail, "symptom": observed}
    state = observed.get("state") if isinstance(observed, dict) else None

    if state == "present":
        return OUTCOME_FAIL, {
            **detail,
            "reason": (
                f"{memory_detail['reason']}; but the service is still showing the overload "
                f"symptom after the restart (5xx rate or p95 latency over its alert threshold), "
                f"so the restart did not fix it"
            ),
        }
    if state == "absent":
        return OUTCOME_PASS, {
            **detail,
            "reason": f"{memory_detail['reason']}, and the 5xx rate and p95 latency are both back under their thresholds",
        }
    return OUTCOME_INCONCLUSIVE, {
        **detail,
        "reason": (
            f"{memory_detail['reason']}, but the 5xx rate and p95 latency could not be observed "
            f"after the restart (no traffic or an unreadable read), so recovery cannot be confirmed"
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

