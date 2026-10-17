"""Tier 1 actions refuse any container name outside the service allowlist,
before the proxy is called.

The allowlist is the application services health_tool already inspects. The
refusal cases are the ones that matter: a stateful or control container, and a
name that would change the URL path the proxy is asked for.
"""

from __future__ import annotations

import pytest

from phoenix.tools import remediation_tool


class _Response:
    def raise_for_status(self) -> None:
        return None


@pytest.fixture
def proxy(monkeypatch):
    """Record every POST to the proxy. A refused name must leave this empty."""
    calls: list[str] = []

    def fake_post(url, timeout):
        calls.append(url)
        return _Response()

    monkeypatch.setattr(remediation_tool.requests, "post", fake_post)
    return calls


@pytest.mark.parametrize(
    "name",
    [
        "postgres",
        "phoenix-api",
        "prometheus",
        "rabbitmq",
        "docker-socket-proxy",
        "../../containers/create",
        "checkout-service/../../postgres",
        "checkout-service/exec",
        "",
        "checkout-service ",
        None,
    ],
)
@pytest.mark.parametrize(
    "tool",
    [remediation_tool.restart_service, remediation_tool.pause_worker, remediation_tool.resume_worker],
)
def test_a_container_outside_the_allowlist_is_refused_without_calling_the_proxy(tool, name, proxy):
    result = tool(name)

    assert result["status"] == "error"
    assert "not in the service allowlist" in result["error"]
    assert proxy == []


def test_the_allowlist_is_the_services_health_tool_inspects():
    from phoenix.tools.health_tool import SERVICE_PORTS

    assert remediation_tool.ALLOWED_CONTAINER_NAMES == frozenset(SERVICE_PORTS)
    assert "postgres" not in remediation_tool.ALLOWED_CONTAINER_NAMES
    assert "phoenix-api" not in remediation_tool.ALLOWED_CONTAINER_NAMES


@pytest.mark.parametrize(
    ("tool", "verb"),
    [
        (remediation_tool.restart_service, "restart"),
        (remediation_tool.pause_worker, "pause"),
        (remediation_tool.resume_worker, "unpause"),
    ],
)
def test_a_valid_service_name_still_reaches_the_proxy(tool, verb, proxy):
    result = tool("checkout-service")

    assert result["status"] == "ok"
    assert proxy == [f"{remediation_tool.DOCKER_PROXY_URL}/containers/checkout-service/{verb}"]
