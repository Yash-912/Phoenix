"""Deployment history as files on disk, one marker per real deployment.

A marker records intent: what was deployed, when, and what it replaced. It is
written by whoever performs the deployment -- the chaos script for a bad
release, Phoenix for a rollback -- and it is evidence of *intent*, never proof
of what is currently running. Only the running container can answer that, which
is why `rollback_deployment` re-reads container metadata after it acts.

**Why the root is overridable.** The default sits under the repository root, but
that makes history a property of *which checkout you happen to be in*: two
worktrees disagree about what was deployed, and a `git clean -xdf` silently
erases the record Phoenix needs to justify a rollback. PHOENIX_DEPLOYMENTS_ROOT
lets the lab point one shared history at both the app and the API container,
and lets tests use a temporary directory without writing into the repository.

**Why reads do not create directories.** Creating a directory as a side effect of
asking "what do you know?" means a read-only observer leaves state behind, and an
empty answer becomes indistinguishable from a service that was never deployed.
`_service_dir` is for writers; `get_recent_deployments` and `get_latest_marker`
resolve paths without touching the filesystem.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DEPLOYMENTS_ROOT = Path(__file__).resolve().parents[2] / "deployments"


def deployments_root() -> Path:
    override = os.environ.get("PHOENIX_DEPLOYMENTS_ROOT")
    return Path(override) if override else DEFAULT_DEPLOYMENTS_ROOT


def _service_dir(service: str) -> Path:
    """Writable service directory. Callers that write markers rely on this."""
    d = deployments_root() / service
    d.mkdir(parents=True, exist_ok=True)
    return d


def _service_path(service: str) -> Path:
    """Non-creating path, for readers."""
    return deployments_root() / service


def config_hash(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def get_latest_marker(service: str) -> str | None:
    """Filename of the newest marker, or None when the service has no history."""
    d = _service_path(service)
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.json"))
    return files[-1].name if files else None


def write_deployment_marker(
    service: str,
    image_tag: str,
    git_commit: str = "unknown",
    config: dict | None = None,
    deployed_by: str = "chaos script",
    image_digest: str | None = None,
    rolled_back_from: str | None = None,
) -> dict:
    """Write one marker, return its dict.

    `image_digest` is the resolved digest of the image that was actually started,
    recorded by whoever deployed it. It is what lets a later reader distinguish
    "we intended v17" from "v17 is running" -- the marker can be wrong, a running
    container cannot.

    `rolled_back_from` names the marker this deployment reverted, so the
    current/previous relationship is explicit in both directions rather than
    only reachable by walking the chain forward.
    """
    config = config or {}
    now = datetime.now(timezone.utc).isoformat()
    previous = get_latest_marker(service)
    marker = {
        "service": service,
        "timestamp": now,
        "git_commit": git_commit,
        "image_tag": image_tag,
        "config_hash": config_hash(config),
        "config": config,
        "deployed_by": deployed_by,
        "previous_marker": previous,
        "rolled_back_from": rolled_back_from,
        # None until someone resolves it against the live container.
        "image_digest": image_digest,
    }
    safe_ts = now.replace(":", "-")
    (_service_dir(service) / f"{safe_ts}.json").write_text(json.dumps(marker, indent=2))
    return marker


def get_recent_deployments(service: str, limit: int = 10) -> list[dict]:
    """Read newest-first, up to limit. Pure read: creates nothing."""
    d = _service_path(service)
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.json"), reverse=True)[:limit]
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text()))
        except (ValueError, OSError):
            continue
    return out
