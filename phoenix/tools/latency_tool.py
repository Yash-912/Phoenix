"""A measured latency, attached to the observer's own Prometheus read.

The scorer matches keywords in whatever a tool returns, so a bare read of
http_request_duration_seconds cannot say whether a service is slow: every service
exports the histogram, and its name is in the reading whether the p95 is 20 ms or
2 s. This module gives the question a numeric answer, computed here, in code,
from a real range read -- the model never calculates it and never states it. It
is the same shape as memory_tool's memory_trend, for the same reason.

When a query_prometheus call references the latency histogram for one job, the
response comes back with a ``latency_measure`` block added; nothing else about
the tool changes, and the memory_trend block memory_tool attaches is kept.

The threshold is the paging alert's own, not a new one: HighLatency fires on a p95
above 1 s for 30 s, so a service counts as slow here when its p95 is above 1 s on
two consecutive samples 30 s apart. A single spike is not a regression, and a
window with no traffic has no p95 at all (NaN), which is dropped rather than
read as fast or slow.

A slowdown is also anchored to the incident. A run of slow samples that was over
before the incident began -- by more than the lag between a symptom starting and
its alert opening the incident -- is real, but it is another incident's history,
and a thirty minute window is long enough to hold one. It is reported as
``elevated_before_onset_only`` and does not count. The same module answers a second
question, ``current_latency_state``: is the service slow right now, which is
what has to be true before anything is done about it.

Every string in the block is chosen so that it cannot satisfy a hypothesis
keyword: scoring reads a typed field, ``sustained_slow``, not the wording.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime

from phoenix.graph import correlation, scoring
from phoenix.tools.deploy_tool import incident_started_at
from phoenix.tools.memory_tool import query_prometheus_with_memory_trend
from phoenix.tools.prometheus_tool import query_prometheus_range

LATENCY_METRIC = "http_request_duration_seconds"
LATENCY_P95_THRESHOLD_SECONDS = 1.0  # HighLatency's own threshold
MIN_CONSECUTIVE_SAMPLES = 2  # HighLatency's `for: 30s` at a 30s step
WINDOW_MINUTES = 30
STEP_SECONDS = 30
MIN_SAMPLES = 2

# "Now" is the latest finite sample, and only if it is recent: a p95 over a one
# minute rate window has no value once traffic stops, so an old sample says what
# the service was doing, not what it is doing.
CURRENT_WINDOW_MINUTES = 10
CURRENT_MAX_AGE_SECONDS = 120

UNAVAILABLE = {"verdict": "unavailable", "sustained_slow": False}

_JOB = re.compile(r'job\s*=\s*"([A-Za-z0-9_.-]+)"')
_MENTIONS_METRIC = re.compile(rf"\b{LATENCY_METRIC}(?:_bucket|_count|_sum)?\b")
_FROM_TOOL = object()  # "ask the deployment tool when this incident began"


def _slow_runs(points: list[tuple[float, float]]) -> list[tuple[float, float, int]]:
    """Consecutive samples over the threshold, as (first timestamp, last timestamp, length)."""
    runs: list[tuple[float, float, int]] = []
    start = last = None
    length = 0
    for timestamp, value in points:
        if value > LATENCY_P95_THRESHOLD_SECONDS:
            start = timestamp if length == 0 else start
            last = timestamp
            length += 1
        elif length:
            runs.append((start, last, length))
            length = 0
    if length:
        runs.append((start, last, length))
    return runs


def summarize_latency(samples: list[tuple[float, float]], onset: datetime | None = None) -> dict:
    """Judge a series of (timestamp, p95 seconds) samples. Pure: no I/O, no clock.

    With `onset`, only a slow run that was still under way within the detection lag
    of the incident's start (or began after it) counts; without one the whole window
    is judged and the block says it was not anchored.
    """
    points = sorted(
        (float(t), float(v)) for t, v in samples if math.isfinite(float(t)) and math.isfinite(float(v))
    )
    if len(points) < MIN_SAMPLES:
        return {"verdict": "insufficient_data", "sustained_slow": False, "samples": len(points)}

    sustained_runs = [run for run in _slow_runs(points) if run[2] >= MIN_CONSECUTIVE_SAMPLES]
    if onset is not None:
        current_runs = [run for run in sustained_runs if not correlation.ended_before_onset(run[1], onset)]
    else:
        current_runs = sustained_runs
    sustained = bool(current_runs)
    historical_only = bool(sustained_runs) and not sustained

    if sustained:
        verdict = "elevated_ongoing"
    elif historical_only:
        verdict = "elevated_before_onset_only"
    else:
        verdict = "within_threshold"

    measure = {
        "verdict": verdict,
        "sustained_slow": sustained,
        "samples": len(points),
        "peak_p95_seconds": round(max(v for _, v in points), 3),
        "threshold_seconds": LATENCY_P95_THRESHOLD_SECONDS,
        "anchored": onset is not None,
    }
    if historical_only:
        measure["historical_only"] = True
    return measure


def _samples(series: dict) -> list[tuple[float, float]]:
    parsed = []
    for pair in series.get("values", []):
        try:
            parsed.append((float(pair[0]), float(pair[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return parsed


def _read_series(service_name: str, window_seconds: int, at: float | None):
    """One service's p95 series over the window ending at `at` (now by default).

    Returns (samples, None) on success, or (None, outcome) where outcome is
    "unavailable" (a failed or ambiguous read, or a job name outside the strict
    character set, which is never queried) or "empty" (no series at all).
    """
    if not _JOB.fullmatch(f'job="{service_name}"'):
        return None, "unavailable"
    end = int(at if at is not None else time.time())
    query = (
        f'histogram_quantile(0.95, sum by (le) '
        f'(rate({LATENCY_METRIC}_bucket{{job="{service_name}"}}[1m])))'
    )
    payload = query_prometheus_range(query, end - window_seconds, end, STEP_SECONDS)
    if not scoring._is_usable({"raw_data": payload}):
        return None, "unavailable"
    try:
        results = payload["data"]["result"]
    except (KeyError, TypeError):
        return None, "unavailable"
    if not isinstance(results, list) or len(results) > 1:
        return None, "unavailable"
    if not results:
        return None, "empty"
    return _samples(results[0]), None


def get_latency_measure(service_name: str, at: float | None = None, onset=_FROM_TOOL) -> dict:
    """The measured p95 latency of one service over the recent window, anchored to the incident.

    Read-only. A failed read, or a selector that matches more than one series, is
    "unavailable" and carries no error text: that text would be scored as evidence
    like any other returned value, and a failed measurement must never count for or
    against a hypothesis. `at` takes the measurement as of a past moment, and
    `onset` overrides the incident clock; both exist so a stored incident can be
    replayed, and neither is something the model supplies.
    """
    window = WINDOW_MINUTES * 60
    samples, outcome = _read_series(service_name, window, at)
    if outcome == "unavailable":
        return dict(UNAVAILABLE)
    if outcome == "empty":
        return {"verdict": "insufficient_data", "sustained_slow": False, "samples": 0, "window_seconds": window}

    if onset is _FROM_TOOL:
        onset = incident_started_at()
    measure = summarize_latency(samples, onset=onset)
    measure["window_seconds"] = window
    return measure


def current_latency_state(service_name: str, at: float | None = None) -> dict:
    """Is the service slow right now: present, absent, or unknown.

    Judged on the latest finite sample, and only if it is recent. A window with no
    traffic, a failed read, or a latest sample too old to speak for now is unknown:
    the absence of an observation is not an observation of absence, so it never
    reads as healthy.
    """
    samples, outcome = _read_series(service_name, CURRENT_WINDOW_MINUTES * 60, at)
    if outcome is not None:
        return {"state": "unknown"}
    finite = sorted((t, v) for t, v in samples if math.isfinite(t) and math.isfinite(v))
    if not finite:
        return {"state": "unknown"}
    timestamp, value = finite[-1]
    end = at if at is not None else time.time()
    age = end - timestamp
    if age > CURRENT_MAX_AGE_SECONDS:
        return {"state": "unknown", "sample_age_seconds": round(age)}
    return {
        "state": "present" if value > LATENCY_P95_THRESHOLD_SECONDS else "absent",
        "latest_p95_seconds": round(value, 3),
        "sample_age_seconds": round(age),
        "threshold_seconds": LATENCY_P95_THRESHOLD_SECONDS,
    }


def latency_read_job(promql: str) -> str | None:
    """The one job a query reads the latency histogram for, or None.

    None when the query does not mention the histogram, or names no job or more than
    one. Shared by the live wrapper below and by the replay of a stored incident, so
    the two cannot disagree about which reads get a measurement.
    """
    if not _MENTIONS_METRIC.search(promql):
        return None
    jobs = set(_JOB.findall(promql))
    return next(iter(jobs)) if len(jobs) == 1 else None


def query_prometheus_with_latency_measure(promql: str) -> dict:
    """query_prometheus (with its memory trend), plus the measured latency when the
    query reads the latency histogram for exactly one job.

    The job is taken from the selector in the query and checked against a strict
    character set, so the only thing a query can cause is a read-only range query
    for that one metric. A query that does not mention the metric, or that names no
    single job, is returned exactly as the inner read returned it.
    """
    result = query_prometheus_with_memory_trend(promql)
    if not isinstance(result, dict) or not scoring._is_usable({"raw_data": result}):
        return result
    job = latency_read_job(promql)
    if job is None:
        return result
    return {**result, "latency_measure": get_latency_measure(job)}
