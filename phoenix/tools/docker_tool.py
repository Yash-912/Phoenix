import os

import requests

DOCKER_PROXY_URL = os.environ.get("DOCKER_PROXY_URL", "http://localhost:2375")


def _without_env_values(inspect: dict) -> dict:
    """The inspect payload with Config.Env reduced to variable names.

    The payload is stored as evidence and shown to the model, and a container's
    environment is where its secrets live. Nothing reads the values -- verification
    and the policy read labels and State -- so the names stay, which is enough to
    see that a variable is set, and the values go before anything is stored.
    """
    config = inspect.get("Config") if isinstance(inspect, dict) else None
    env = config.get("Env") if isinstance(config, dict) else None
    if not isinstance(env, list):
        return inspect
    names = [entry.split("=", 1)[0] if isinstance(entry, str) else entry for entry in env]
    return {**inspect, "Config": {**config, "Env": names}}


def get_container_state(container_name: str) -> dict:
    """Inspect a container's current state via the docker-socket-proxy.

    Never touches /var/run/docker.sock directly — every request goes through
    the proxy on port 2375. Its allowlist (infra/docker-socket-proxy/
    haproxy.cfg.template, mounted by docker-compose.yml) permits GET on
    /containers/{name}/json, which is the one call made here; the only POSTs it
    lets through are restart, pause and unpause, used by remediation_tool.
    """
    try:
        response = requests.get(
            f"{DOCKER_PROXY_URL}/containers/{container_name}/json",
            timeout=5,
        )
        response.raise_for_status()
        return _without_env_values(response.json())
    except requests.RequestException as exc:
        return {"status": "error", "error": str(exc)}
