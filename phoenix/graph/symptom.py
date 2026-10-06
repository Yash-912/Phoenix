"""Is the symptom a finding describes still there now?

Real evidence is not current evidence, and current evidence is not causal
evidence; this module is only the middle question. Historical evidence can
support a diagnosis -- a slowdown that really happened, memory that really grew
-- for a service that has since stopped being slow or stopped growing. Acting on
it then repairs nothing and, for a patch, opens a PR for a problem nobody can
observe.

The verifier refuses to report a recovery it did not observe. This is the
inverse: the remediator refuses to act on a symptom that is no longer observed.
Each category that can be probed has a probe that answers present, absent or
unknown. Only a clear absence blocks an action; unknown (no traffic, a failed
read, a sample too old to speak for now) does not, because the absence of an
observation is not an observation of absence, and the causal evidence has already
been required to overlap the incident's onset. A category with no probe is not
checked and behaves exactly as it did.
"""

from __future__ import annotations

from phoenix.tools import error_rate_tool, latency_tool, memory_tool

STATES = ("present", "absent", "unknown")

NOT_CHECKED = {"state": "not_checked"}

# Looked up through the module at call time, so the probes are the tools' own
# current functions and nothing is copied here.
def _overload_probe(service: str, at: float | None = None) -> dict:
    """Is any of overload's signals -- 5xx, slowness, memory growth -- there now?

    An overload finding can rest on any one of them, so the finding is stale only when
    all of them are clearly gone. One present signal is enough to act on; a signal that
    cannot be read leaves the answer unknown rather than absent, because a gone error
    spike next to an unreadable latency read is not evidence that the load has cleared.
    """
    readers = {
        "error_rate": lambda: error_rate_tool.current_error_rate_state(service)
        if at is None else error_rate_tool.current_error_rate_state(service, at=at),
        "latency": lambda: latency_tool.current_latency_state(service)
        if at is None else latency_tool.current_latency_state(service, at=at),
        "memory": lambda: memory_tool.current_memory_state(service)
        if at is None else memory_tool.current_memory_state(service, at=at),
    }
    signals = {}
    for name, read in readers.items():
        try:
            result = read()
        except Exception:  # noqa: BLE001 - an unreadable signal is unknown, as for any probe
            result = None
        state = result.get("state") if isinstance(result, dict) else None
        signals[name] = result if state in STATES else {"state": "unknown"}
    states = {signal["state"] for signal in signals.values()}
    if "present" in states:
        state = "present"
    elif "unknown" in states:
        state = "unknown"
    else:
        state = "absent"
    return {"state": state, "signals": signals}


PROBES = {
    "slow_query": lambda service, at=None: (
        latency_tool.current_latency_state(service) if at is None else latency_tool.current_latency_state(service, at=at)
    ),
    "memory_leak": lambda service, at=None: (
        memory_tool.current_memory_state(service) if at is None else memory_tool.current_memory_state(service, at=at)
    ),
    "overload": _overload_probe,
}


def current_symptom(category: str | None, service_name: str, at: float | None = None) -> dict:
    """The present/absent/unknown state of the category's symptom on this service.

    `at` asks as of a past moment, for replaying a stored incident; a live run never
    passes it. A probe that raises, or returns anything but a dict carrying a known
    state, is unknown: an unreadable probe is a gap in what can be observed, not a
    verdict, and it must never take a run down.
    """
    probe = PROBES.get(category)
    if probe is None:
        return dict(NOT_CHECKED)
    try:
        result = probe(service_name) if at is None else probe(service_name, at)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return {"state": "unknown", "detail": f"probe raised {type(exc).__name__}"}
    if not isinstance(result, dict) or result.get("state") not in STATES:
        return {"state": "unknown", "detail": "probe returned an unusable result"}
    return result


def blocks_action(observed: dict) -> bool:
    """Only a symptom that is clearly gone stands in the way of acting on a finding."""
    return isinstance(observed, dict) and observed.get("state") == "absent"
