"""A measured memory trend, attached to the observer's own Prometheus read.

The scorer matches keywords in whatever a tool returns, so a bare reading of
process_resident_memory_bytes cannot say whether memory is growing: a flat series
and a leaking one carry the same metric name. This module is where the question
gets a numeric answer, computed here, in code, from a real range read -- the
model never calculates it and never states it.

The observer's read surface stays the same five tools. When a query_prometheus
call references the resident-memory metric for one job, the response comes back
with a ``memory_trend`` block added; nothing else about the tool changes, and
verification's own use of query_prometheus is untouched.

Thresholds are the ones already in use, not new ones, so the observer, the
paging alert and the post-restart check cannot drift apart:

  * the window is the alert's own, deriv(...[30m]);
  * the slope tolerance is the alert's own 1024 B/s;
  * the noise floor is the 1 MiB growth budget the post-restart check uses to
    keep allocator wobble from reading as a leak.

What is new is only structure: a leak is growth that is still going on, spread
over many observations. So growth must also be recent (the latest half of the
window still climbs past the tolerance), must not end in a drop (that is a
restart or a release, not monotonic growth), and no single step may carry more
than half of it (that is one allocation, not a leak). A series too short to
judge is "insufficient_data", never growth.

Every string in the block is chosen so that it cannot satisfy a hypothesis
keyword: scoring reads a typed field, ``sustained_growth``, not the wording.
"""

from __future__ import annotations

import math
import re
import time

from phoenix.graph import scoring
from phoenix.graph.verification import MEMORY_METRIC, NOISE_FLOOR_BYTES, SLOPE_TOLERANCE
from phoenix.tools.prometheus_tool import query_prometheus, query_prometheus_range

TREND_WINDOW_MINUTES = 30
TREND_STEP_SECONDS = 60
# Two samples per half is the least a slope over each half can mean anything on.
MIN_TREND_SAMPLES = 6

UNAVAILABLE = {"verdict": "unavailable", "sustained_growth": False}

_JOB = re.compile(r'job\s*=\s*"([A-Za-z0-9_.-]+)"')


def _slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope in bytes per second, as deriv() computes it."""
    count = len(points)
    mean_t = sum(t for t, _ in points) / count
    mean_v = sum(v for _, v in points) / count
    spread = sum((t - mean_t) ** 2 for t, _ in points)
    if spread == 0:
        return 0.0
    return sum((t - mean_t) * (v - mean_v) for t, v in points) / spread


def summarize_memory_trend(samples: list[tuple[float, float]]) -> dict:
    """Judge a series of (timestamp, bytes) samples. Pure: no I/O, no clock."""
    points = sorted(
        (float(t), float(v)) for t, v in samples if math.isfinite(float(t)) and math.isfinite(float(v))
    )
    if len(points) < MIN_TREND_SAMPLES:
        return {"verdict": "insufficient_data", "sustained_growth": False, "samples": len(points)}

    values = [v for _, v in points]
    growth = values[-1] - values[0]
    steps = [later - earlier for earlier, later in zip(values, values[1:])]
    largest_drop = max((-step for step in steps if step < 0), default=0.0)
    largest_rise = max((step for step in steps if step > 0), default=0.0)
    slope = _slope(points)
    recent_slope = _slope(points[len(points) // 2:])

    sustained = (
        growth > NOISE_FLOOR_BYTES
        and slope > SLOPE_TOLERANCE
        and recent_slope > SLOPE_TOLERANCE
        and largest_drop <= NOISE_FLOOR_BYTES
        and largest_rise <= growth / 2
    )
    return {
        "verdict": "sustained_growth" if sustained else "no_sustained_growth",
        "sustained_growth": sustained,
        "samples": len(points),
        "bytes_start": round(values[0]),
        "bytes_now": round(values[-1]),
        "growth_bytes": round(growth),
        "slope_bytes_per_s": round(slope, 1),
        "recent_slope_bytes_per_s": round(recent_slope, 1),
        "largest_drop_bytes": round(largest_drop),
        "largest_rise_bytes": round(largest_rise),
    }


def _samples(series: dict) -> list[tuple[float, float]]:
    parsed = []
    for pair in series.get("values", []):
        try:
            parsed.append((float(pair[0]), float(pair[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return parsed


def get_memory_trend(service_name: str, at: float | None = None) -> dict:
    """The measured trend of one service's resident memory over the alert's window.

    Read-only. A failed read, or a selector that matches more than one series,
    is "unavailable" and carries no error text: that text would be scored as
    evidence like any other returned value, and a failed measurement must never
    count for or against a hypothesis.
    """
    if not _JOB.fullmatch(f'job="{service_name}"'):
        return dict(UNAVAILABLE)

    window = TREND_WINDOW_MINUTES * 60
    end = int(at if at is not None else time.time())
    payload = query_prometheus_range(
        f'{MEMORY_METRIC}{{job="{service_name}"}}', end - window, end, TREND_STEP_SECONDS
    )
    if not scoring._is_usable({"raw_data": payload}):
        return dict(UNAVAILABLE)
    try:
        results = payload["data"]["result"]
    except (KeyError, TypeError):
        return dict(UNAVAILABLE)
    if not isinstance(results, list) or len(results) > 1:
        return dict(UNAVAILABLE)
    if not results:
        return {"verdict": "insufficient_data", "sustained_growth": False, "samples": 0, "window_seconds": window}

    trend = summarize_memory_trend(_samples(results[0]))
    trend["window_seconds"] = window
    return trend


def current_memory_state(service_name: str, at: float | None = None) -> dict:
    """Is the service's memory still growing right now: present, absent, or unknown.

    Judged on the same window and the same slope tolerance as the trend, but on the
    recent half of it only: a leak that stopped, or was released by a restart, no
    longer has a recent climb even though the window as a whole still shows growth.
    A read that failed, or a series too short to judge, is unknown and never reads
    as a leak having stopped.
    """
    trend = get_memory_trend(service_name, at=at)
    recent = trend.get("recent_slope_bytes_per_s")
    if trend.get("verdict") in ("unavailable", "insufficient_data") or recent is None:
        return {"state": "unknown"}
    return {
        "state": "present" if recent > SLOPE_TOLERANCE else "absent",
        "recent_slope_bytes_per_s": recent,
        "tolerance_bytes_per_s": SLOPE_TOLERANCE,
    }


def query_prometheus_with_memory_trend(promql: str) -> dict:
    """query_prometheus, plus the measured trend when the query reads the
    resident-memory metric for exactly one job.

    The job is taken from the selector in the query and checked against a strict
    character set, so the only thing a query can cause is a read-only range
    query for that one metric. A query that does not mention the metric, or
    that names no single job, is returned exactly as query_prometheus returned
    it.
    """
    result = query_prometheus(promql)
    if not isinstance(result, dict) or not scoring._is_usable({"raw_data": result}):
        return result
    if not re.search(rf"\b{MEMORY_METRIC}\b", promql):
        return result
    jobs = set(_JOB.findall(promql))
    if len(jobs) != 1:
        return result
    return {**result, "memory_trend": get_memory_trend(next(iter(jobs)))}
