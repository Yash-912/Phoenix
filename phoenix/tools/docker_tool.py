import os

import requests

DOCKER_PROXY_URL = os.environ.get("DOCKER_PROXY_URL", "http://localhost:2375")


def get_container_state(container_name: str) -> dict:
    """Inspect a container's current state via the docker-socket-proxy.

    Never touches /var/run/docker.sock directly — every request goes through
    the proxy on port 2375, which only permits GET/HEAD on /containers/*
    (see docker-compose.yml's docker-socket-proxy service, CONTAINERS=1,
    POST left at its default of 0/denied).
    """
    try:
        response = requests.get(
            f"{DOCKER_PROXY_URL}/containers/{container_name}/json",
            timeout=5,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        return {"status": "error", "error": str(exc)}
