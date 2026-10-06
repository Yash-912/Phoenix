"""Is the service returning 5xx right now: present, absent, or unknown.

The probe for the 5xx half of "overload", read the way latency's is: the latest finite
sample of the alert's own ratio, and only if it is recent. The threshold is the
HighErrorRate alert's, so the probe, the alert and the finding cannot disagree about
what counts as an error spike.

A window with no traffic is unknown, never healthy. The numerator falls back to zero
when there is no 5xx series at all, so a service that is serving traffic without errors
reads as absent, while one serving nothing leaves the ratio undefined.
"""

from __future__ import annotations

import math
import re
import time

from phoenix.tools.prometheus_tool import query_prometheus_range

ERROR_RATIO_THRESHOLD = 0.05  # HighErrorRate's own threshold
CURRENT_WINDOW_MINUTES = 10
CURRENT_MAX_AGE_SECONDS = 120
STEP_SECONDS = 30

_JOB = re.compile(r'job\s*=\s*"([A-Za-z0-9_.-]+)"')


def current_error_rate_state(service_name: str, at: float | None = None) -> dict:
    """Is the 5xx ratio over the alert threshold now: present, absent, or unknown."""
    if not _JOB.fullmatch(f'job="{service_name}"'):
        return {"state": "unknown"}
    end = int(at if at is not None else time.time())
    query = (
        f'(sum(rate(http_requests_total{{job="{service_name}", status=~"5.."}}[1m])) or vector(0))'
        f' / sum(rate(http_requests_total{{job="{service_name}"}}[1m]))'
    )
    payload = query_prometheus_range(query, end - CURRENT_WINDOW_MINUTES * 60, end, STEP_SECONDS)
    try:
        results = payload["data"]["result"]
        if payload.get("status") != "success" or not isinstance(results, list) or len(results) != 1:
            return {"state": "unknown"}
        samples = [(float(t), float(v)) for t, v in results[0]["values"]]
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"state": "unknown"}
    finite = sorted((t, v) for t, v in samples if math.isfinite(t) and math.isfinite(v))
    if not finite:
        return {"state": "unknown"}
    timestamp, value = finite[-1]
    age = (at if at is not None else time.time()) - timestamp
    if age > CURRENT_MAX_AGE_SECONDS:
        return {"state": "unknown", "sample_age_seconds": round(age)}
    return {
        "state": "present" if value > ERROR_RATIO_THRESHOLD else "absent",
        "latest_error_ratio": round(value, 3),
        "sample_age_seconds": round(age),
        "threshold_ratio": ERROR_RATIO_THRESHOLD,
    }
