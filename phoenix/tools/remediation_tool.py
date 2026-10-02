"""Remediation via docker-socket-proxy with tool-level allowlist.

Proxy has POST=1 globally, but this module is the enforcement point:
only actions in ALLOWED_DOCKER_ACTIONS can ever execute. No raw exec,
no image build, no network/volume management.

**Why rollback is here and not in the proxy.** Tier 1 acts on a *running*
container: restart, pause, unpause are single POST verbs. Tier 2 replaces one,
which needs the old container removed and the new one attached to the compose
network -- DELETE and NETWORKS, both denied by the proxy. The proxy was left
untouched on purpose (see chaos/lib/deployer.py for why widening it is worse),
so `rollback_deployment` is the one action here that leaves the Engine API.

**What keeps that from being arbitrary command execution.** The caller supplies
exactly two things: a service name and a version, both checked against
allowlists before anything runs, and both drawn from deployment history rather
than from the model. The argv, the compose file, and the project name are
constants. There is no path from a hypothesis to a shell string.
"""

from __future__ import annotations

import os

import requests

DOCKER_PROXY_URL = os.environ.get("DOCKER_PROXY_URL", "http://localhost:2375")

ALLOWED_DOCKER_ACTIONS = {
    "restart_service": {"method": "POST", "path": "/containers/{name}/restart"},
    "pause_worker": {"method": "POST", "path": "/containers/{name}/pause"},
    "resume_worker": {"method": "POST", "path": "/containers/{name}/unpause"},
}

# Tier 2. Not reachable through _call_proxy -- see module docstring.
ALLOWED_ROLLBACK_SERVICES = {"checkout-service"}
ALLOWED_ROLLBACK_VERSIONS = {"v17", "v18"}


def _call_proxy(action: str, container_name: str) -> dict:
    spec = ALLOWED_DOCKER_ACTIONS.get(action)
    if spec is None:
        return {"status": "error", "error": f"action '{action}' not in allowlist"}
    url = f"{DOCKER_PROXY_URL}{spec['path'].format(name=container_name)}"
    try:
        response = requests.post(url, timeout=10)
        response.raise_for_status()
        return {"status": "ok", "action": action, "container": container_name}
    except requests.RequestException as exc:
        return {"status": "error", "error": str(exc)}


def restart_service(container_name: str) -> dict:
    """Restart a container (new process, not resumed). Tier 1, low risk."""
    return _call_proxy("restart_service", container_name)


def pause_worker(container_name: str) -> dict:
    """Pause a worker container. Tier 1, low risk."""
    return _call_proxy("pause_worker", container_name)


def resume_worker(container_name: str) -> dict:
    """Resume a paused worker container. Tier 1, low risk."""
    return _call_proxy("resume_worker", container_name)


def rollback_deployment(
    service_name: str,
    target_version: str,
    rolled_back_from: str | None = None,
) -> dict:
    """Redeploy a known-good artifact over the running one. Tier 2.

    `target_version` must come from deployment history, not from the caller
    inventing it: a rollback to a version that was never deployed, or never
    healthy, replaces one bad release with an unknown one. Callers get it from
    the previous marker; this function re-validates it rather than trusting
    them, because policy code is the wrong place to be the only gate.

    Both arguments are validated against allowlists and passed to
    chaos.lib.deployer.apply_deployment as data. Nothing from the model reaches
    a shell.

    The target is restored, not rebuilt: v17 and v18 are one source tree with
    different compile-time values, so rebuilding v17 from the current checkout
    could put back something that was never the verified-good v17. The image tag
    is what preserves it.
    """
    if service_name not in ALLOWED_ROLLBACK_SERVICES:
        return {
            "status": "error",
            "action": "rollback_deployment",
            "error": f"service '{service_name}' not rollback-eligible",
            "allowed": sorted(ALLOWED_ROLLBACK_SERVICES),
        }
    if target_version not in ALLOWED_ROLLBACK_VERSIONS:
        return {
            "status": "error",
            "action": "rollback_deployment",
            "error": f"version '{target_version}' not a known-good artifact",
            "allowed": sorted(ALLOWED_ROLLBACK_VERSIONS),
        }

    from chaos.lib.deployer import DeploymentError, apply_deployment

    try:
        marker = apply_deployment(
            service=service_name,
            version=target_version,
            deployed_by="phoenix/remediation_tool.rollback_deployment",
            rolled_back_from=rolled_back_from,
            build=False,
        )
    except DeploymentError as exc:
        return {"status": "error", "action": "rollback_deployment", "error": str(exc)}

    return {
        "status": "ok",
        "action": "rollback_deployment",
        "service": service_name,
        "rolled_back_from": rolled_back_from,
        "to_version": target_version,
        "timestamp": marker.get("timestamp"),
        "image_digest": marker.get("image_digest"),
        "observed_label_version": marker.get("observed_label_version"),
    }


def clear_approved_cache(cache_key: str) -> dict:
    """Clear a Redis key. Tier 1, low risk. Only single-key DEL, no FLUSHALL."""
    if not cache_key or not isinstance(cache_key, str):
        return {"status": "error", "error": "cache_key must be a non-empty string"}
    if "*" in cache_key or cache_key.upper() in ("*", "FLUSHALL", "FLUSHDB"):
        return {"status": "error", "error": "wildcard/flush not allowed"}
    try:
        import socket

        redis_host = os.environ.get("REDIS_HOST", "localhost")
        s = socket.create_connection((redis_host, 6379), timeout=5)
        cmd = f"*2\r\n$3\r\nDEL\r\n${len(cache_key)}\r\n{cache_key}\r\n".encode()
        s.sendall(cmd)
        s.recv(1024)
        s.close()
        return {"status": "ok", "action": "clear_approved_cache", "key": cache_key}
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
