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

Every string in the block is chosen so that it cannot satisfy a hypothesis
keyword: scoring reads a typed field, ``sustained_slow``, not the wording.
"""

from __future__ import annotations

import math
import re
import time

from phoenix.graph import scoring
from phoenix.tools.memory_tool import query_prometheus_with_memory_trend
from phoenix.tools.prometheus_tool import query_prometheus_range

LATENCY_METRIC = "http_request_duration_seconds"
LATENCY_P95_THRESHOLD_SECONDS = 1.0  # HighLatency's own threshold
MIN_CONSECUTIVE_SAMPLES = 2  # HighLatency's `for: 30s` at a 30s step
WINDOW_MINUTES = 30
STEP_SECONDS = 30
MIN_SAMPLES = 2

UNAVAILABLE = {"verdict": "unavailable", "sustained_slow": False}

_JOB = re.compile(r'job\s*=\s*"([A-Za-z0-9_.-]+)"')
_MENTIONS_METRIC = re.compile(rf"\b{LATENCY_METRIC}(?:_bucket|_count|_sum)?\b")


def summarize_latency(samples: list[tuple[float, float]]) -> dict:
    """Judge a series of (timestamp, p95 seconds) samples. Pure: no I/O, no clock."""
    points = sorted(
        (float(t), float(v)) for t, v in samples if math.isfinite(float(t)) and math.isfinite(float(v))
    )
    if len(points) < MIN_SAMPLES:
        return {"verdict": "insufficient_data", "sustained_slow": False, "samples": len(points)}

    run = longest = 0
    for _, value in points:
        run = run + 1 if value > LATENCY_P95_THRESHOLD_SECONDS else 0
        longest = max(longest, run)
    sustained = longest >= MIN_CONSECUTIVE_SAMPLES

    return {
        "verdict": "sustained_slow" if sustained else "not_sustained_slow",
        "sustained_slow": sustained,
        "samples": len(points),
        "peak_p95_seconds": round(max(v for _, v in points), 3),
        "threshold_seconds": LATENCY_P95_THRESHOLD_SECONDS,
    }


def _samples(series: dict) -> list[tuple[float, float]]:
    parsed = []
    for pair in series.get("values", []):
        try:
            parsed.append((float(pair[0]), float(pair[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return parsed


def get_latency_measure(service_name: str) -> dict:
    """The measured p95 latency of one service over the recent window.

    Read-only. A failed read, or a selector that matches more than one series, is
    "unavailable" and carries no error text: that text would be scored as evidence
    like any other returned value, and a failed measurement must never count for or
    against a hypothesis.
    """
    if not _JOB.fullmatch(f'job="{service_name}"'):
        return dict(UNAVAILABLE)

    window = WINDOW_MINUTES * 60
    end = int(time.time())
    query = (
        f'histogram_quantile(0.95, sum by (le) '
        f'(rate({LATENCY_METRIC}_bucket{{job="{service_name}"}}[1m])))'
    )
    payload = query_prometheus_range(query, end - window, end, STEP_SECONDS)
    if not scoring._is_usable({"raw_data": payload}):
        return dict(UNAVAILABLE)
    try:
        results = payload["data"]["result"]
    except (KeyError, TypeError):
        return dict(UNAVAILABLE)
    if not isinstance(results, list) or len(results) > 1:
        return dict(UNAVAILABLE)
    if not results:
        return {"verdict": "insufficient_data", "sustained_slow": False, "samples": 0, "window_seconds": window}

    measure = summarize_latency(_samples(results[0]))
    measure["window_seconds"] = window
    return measure


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
    if not _MENTIONS_METRIC.search(promql):
        return result
    jobs = set(_JOB.findall(promql))
    if len(jobs) != 1:
        return result
    return {**result, "latency_measure": get_latency_measure(next(iter(jobs)))}
