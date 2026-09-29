"""Tier 1 remediation via docker-socket-proxy with tool-level allowlist.

Proxy has POST=1 globally, but this module is the enforcement point:
only actions in ALLOWED_DOCKER_ACTIONS can ever execute. No raw exec,
no image build, no network/volume management.
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
