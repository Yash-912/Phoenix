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

# Tier 2, config rollback. Separate allowlists from the ones above because a
# service being deployment-rollback-eligible says nothing about which config
# keys on it are safe to flip -- the two are independent grants.
ALLOWED_CONFIG_SERVICES = {"auth-service"}
ALLOWED_CONFIG_KEYS = {"DB_POOL_SIZE"}
ALLOWED_CONFIG_VALUES = {"DB_POOL_SIZE": {"1", "10"}}


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


def rollback_config(
    service_name: str,
    key: str,
    target_value: str,
    rolled_back_from: str | None = None,
) -> dict:
    """Restore a known-good config value on a running service. Tier 2.

    Three arguments, three allowlists: service, key, and value are each
    re-validated here rather than trusted from the caller, the same discipline
    rollback_deployment applies to service and version. A config rollback is a
    narrower surface than a deployment rollback -- it changes an environment
    variable, not an artifact -- but it is still a mutating action reached from
    a diagnosis, so it gets the same independent re-check.

    Goes through chaos.lib.deployer.apply_config, which still shells out to
    compose rather than the socket proxy: the container is recreated so the new
    environment value actually reaches the process, and recreate is DELETE plus
    NETWORKS, which the proxy denies for the same reason given in the module
    docstring.
    """
    if service_name not in ALLOWED_CONFIG_SERVICES:
        return {
            "status": "error",
            "action": "rollback_config",
            "error": f"service '{service_name}' not config-rollback-eligible",
            "allowed": sorted(ALLOWED_CONFIG_SERVICES),
        }
    if key not in ALLOWED_CONFIG_KEYS:
        return {
            "status": "error",
            "action": "rollback_config",
            "error": f"key '{key}' not an allowed config key",
            "allowed": sorted(ALLOWED_CONFIG_KEYS),
        }
    allowed_values = ALLOWED_CONFIG_VALUES.get(key, set())
    if target_value not in allowed_values:
        return {
            "status": "error",
            "action": "rollback_config",
            "error": f"value '{target_value}' not a known state for {key}",
            "allowed": sorted(allowed_values),
        }

    from chaos.lib.deployer import DeploymentError, apply_config

    try:
        marker = apply_config(
            service=service_name,
            config={key: target_value},
            deployed_by="phoenix/remediation_tool.rollback_config",
            rolled_back_from=rolled_back_from,
        )
    except DeploymentError as exc:
        return {"status": "error", "action": "rollback_config", "error": str(exc)}

    return {
        "status": "ok",
        "action": "rollback_config",
        "service": service_name,
        "key": key,
        "rolled_back_from": rolled_back_from,
        "to_value": target_value,
        "timestamp": marker.get("timestamp"),
    }


def clear_approved_cache(cache_key: str) -> dict:
    """Clear a Redis key. Tier 1, low risk. Only single-key DEL, no FLUSHALL.

    The RESP length prefix is computed from the encoded bytes, not len(cache_key):
    a key with any non-ASCII character has more UTF-8 bytes than Python
    characters, and a prefix built from the character count would frame the
    command wrong, corrupting the protocol for every byte after it.

    DEL's reply is read and checked rather than discarded. Redis answers with
    an integer reply (":0" or ":1", both legitimate -- 0 means the key was
    already gone) or an error reply ("-ERR ..."). Returning "ok" without
    reading the reply would report success on a command Redis rejected.
    """
    if not cache_key or not isinstance(cache_key, str):
        return {"status": "error", "error": "cache_key must be a non-empty string"}
    if "*" in cache_key or cache_key.upper() in ("*", "FLUSHALL", "FLUSHDB"):
        return {"status": "error", "error": "wildcard/flush not allowed"}
    try:
        import socket

        redis_host = os.environ.get("REDIS_HOST", "localhost")
        key_bytes = cache_key.encode("utf-8")
        cmd = (
            b"*2\r\n$3\r\nDEL\r\n$" + str(len(key_bytes)).encode("ascii") + b"\r\n"
            + key_bytes + b"\r\n"
        )
        s = socket.create_connection((redis_host, 6379), timeout=5)
        try:
            s.sendall(cmd)
            reply = s.recv(1024)
        finally:
            s.close()
    except OSError as exc:
        return {"status": "error", "error": str(exc)}

    if reply.startswith(b":"):
        return {
            "status": "ok",
            "action": "clear_approved_cache",
            "key": cache_key,
            "deleted": reply.strip(b"\r\n") == b":1",
        }
    if reply.startswith(b"-"):
        return {
            "status": "error",
            "error": reply.strip(b"\r\n").decode("utf-8", "replace").lstrip("-"),
        }
    return {
        "status": "error",
        "error": f"unexpected reply from redis: {reply!r}",
    }
