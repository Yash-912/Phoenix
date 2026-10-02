"""Change tools: read JSON deployment markers (read-only).

V1 uses local JSON files written by chaos scripts. No git required.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chaos.lib.deploy_tracker import get_recent_deployments as _get_recent
from phoenix.graph.correlation import correlate_all, parse_timestamp

# Set by the observer before each tool pass, from the incident being worked.
# Module-level rather than a parameter because the tool's signature is the
# model's contract -- adding an argument it has no reason to supply would make
# every call omit it.
_INCIDENT_STARTED_AT = None


def set_incident_started_at(value) -> None:
    """Tell the deployment tool when this incident began."""
    global _INCIDENT_STARTED_AT
    _INCIDENT_STARTED_AT = parse_timestamp(value)


def incident_started_at():
    return _INCIDENT_STARTED_AT


def _incident_time_text() -> str | None:
    return _INCIDENT_STARTED_AT.isoformat() if _INCIDENT_STARTED_AT else None


def get_recent_deployments(service_name: str, limit: int = 10) -> dict:
    """Return newest-first deployment markers for a service.

    Markers are annotated with how each one relates to the incident's onset
    (see correlation.py). The annotation is attached here rather than in the
    diagnoser's prompt because it is arithmetic on timestamps, not reasoning:
    doing it in code keeps the model from having to compare two ISO strings,
    which is the step it gets wrong most often and most confidently.
    """
    try:
        markers = _get_recent(service_name, limit=limit)
        annotated = correlate_all(markers, incident_started_at())
        return {
            "status": "ok",
            "service": service_name,
            "incident_started_at": _incident_time_text(),
            "deployments": annotated,
        }
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
