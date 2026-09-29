"""Change tools: read JSON deployment markers (read-only).

V1 uses local JSON files written by chaos scripts. No git required.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chaos.lib.deploy_tracker import get_recent_deployments as _get_recent


def get_recent_deployments(service_name: str, limit: int = 10) -> dict:
    """Return newest-first deployment markers for a service."""
    try:
        markers = _get_recent(service_name, limit=limit)
        return {"status": "ok", "service": service_name, "deployments": markers}
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
