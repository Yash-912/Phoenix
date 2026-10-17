"""The docker-socket-proxy grants in docker-compose.yml.

The socket behind the proxy is host-root equivalent once container create or
exec is reachable, so the proxy must grant only the verbs remediation_tool
calls. These tests read the compose file's own environment block (no YAML
dependency; the block is a flat list of KEY: value lines) and check it against
the tool's allowlist. They do not start docker.
"""

import re
from pathlib import Path

import pytest

from phoenix.tools import remediation_tool

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"

# What the proxy must grant, and no more: GET on containers for inspection,
# plus the three per-verb container flags the Tier 1 actions use.
EXPECTED_GRANTS = {"CONTAINERS", "ALLOW_RESTARTS", "ALLOW_PAUSE", "ALLOW_UNPAUSE"}

# Sections and verbs that would open container create, exec, images, networks,
# volumes, or the API's other write and control planes. None may be enabled.
DANGEROUS_FLAGS = {
    "POST", "EXEC", "IMAGES", "NETWORKS", "VOLUMES", "BUILD", "COMMIT",
    "CONFIGS", "AUTH", "SECRETS", "SYSTEM", "SWARM", "SERVICES", "TASKS",
    "PLUGINS", "NODES", "DISTRIBUTION", "SESSION", "INFO", "ALLOW_START", "ALLOW_STOP",
}


def _proxy_environment() -> dict[str, str]:
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index("  docker-socket-proxy:")
    env: dict[str, str] = {}
    in_env = False
    for line in lines[start + 1:]:
        if re.match(r"^  \S", line):
            break
        if line == "    environment:":
            in_env = True
            continue
        if not in_env:
            continue
        match = re.match(r"^      ([A-Z_]+):\s*(\S+?)\s*(#.*)?$", line)
        if match:
            env[match.group(1)] = match.group(2).strip("'\"")
        elif line.startswith("    ") and not line.startswith("      "):
            in_env = False
    return env


def _enabled(env: dict[str, str]) -> set[str]:
    return {name for name, value in env.items() if value == "1"}


def test_the_proxy_grants_exactly_the_container_verbs_the_remediation_tools_use():
    assert _enabled(_proxy_environment()) == EXPECTED_GRANTS


@pytest.mark.parametrize("flag", sorted(DANGEROUS_FLAGS))
def test_a_dangerous_proxy_flag_is_not_enabled(flag):
    assert _proxy_environment().get(flag, "0") != "1", f"{flag} must stay off on the socket proxy"


def test_each_tier_1_action_has_its_verb_granted():
    """restart_service, pause_worker and resume_worker each need the flag that
    covers the endpoint they POST to; without it the proxy denies them."""
    env = _proxy_environment()
    verb_to_flag = {"restart": "ALLOW_RESTARTS", "pause": "ALLOW_PAUSE", "unpause": "ALLOW_UNPAUSE"}
    for action, spec in remediation_tool.ALLOWED_DOCKER_ACTIONS.items():
        verb = spec["path"].rsplit("/", 1)[-1]
        assert env.get(verb_to_flag[verb]) == "1", f"{action} needs {verb_to_flag[verb]} on the proxy"
