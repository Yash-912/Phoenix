"""JSON deployment tracker: write/read markers in /deployments/<service>/.

V1 uses local JSON files (per user decision). Each deploy writes one marker;
get_recent_deployments reads the directory sorted by timestamp.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

DEPLOYMENTS_ROOT = Path(__file__).resolve().parents[2] / "deployments"


def _service_dir(service: str) -> Path:
    d = DEPLOYMENTS_ROOT / service
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_hash(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def get_latest_marker(service: str) -> str | None:
    d = _service_dir(service)
    files = sorted(d.glob("*.json"))
    return files[-1].name if files else None


def write_deployment_marker(
    service: str,
    image_tag: str,
    git_commit: str = "unknown",
    config: dict | None = None,
    deployed_by: str = "chaos script",
) -> dict:
    """Write one marker, return its dict. Idempotent per call (new timestamp each time)."""
    config = config or {}
    now = datetime.now(timezone.utc).isoformat()
    marker = {
        "service": service,
        "timestamp": now,
        "git_commit": git_commit,
        "image_tag": image_tag,
        "config_hash": config_hash(config),
        "config": config,
        "deployed_by": deployed_by,
        "previous_marker": get_latest_marker(service),
    }
    safe_ts = now.replace(":", "-")
    (_service_dir(service) / f"{safe_ts}.json").write_text(json.dumps(marker, indent=2))
    return marker


def get_recent_deployments(service: str, limit: int = 10) -> list[dict]:
    """Read newest-first, up to limit."""
    d = _service_dir(service)
    files = sorted(d.glob("*.json"), reverse=True)[:limit]
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text()))
        except (ValueError, OSError):
            continue
    return out
