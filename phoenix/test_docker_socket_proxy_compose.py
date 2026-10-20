"""What the docker-socket-proxy lets through.

The socket behind the proxy is host-root equivalent once container create or
exec is reachable, so the proxy must grant only the verbs remediation_tool
calls. The image's own template refuses every non-GET before it reaches the
per-verb ALLOW_* flags unless POST=1, and POST=1 opens create and exec, so the
allowlist lives in infra/docker-socket-proxy/haproxy.cfg.template.

These tests evaluate that template's allow rules against requests (no YAML
dependency, no docker) and check docker-compose.yml wires it in. The live check
is the same matrix sent to a running proxy.
"""

import re
from pathlib import Path

import pytest

from phoenix.tools import remediation_tool

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
TEMPLATE = ROOT / "infra" / "docker-socket-proxy" / "haproxy.cfg.template"

RULE = re.compile(
    r"^\s*http-request allow if METH_(?P<method>[A-Z]+) "
    r"\{ path,url_dec -m reg -i (?P<regex>\S+) \}\s*$"
)


def _template_lines() -> list[str]:
    return TEMPLATE.read_text(encoding="utf-8").splitlines()


def _allow_rules() -> list[tuple[str, re.Pattern]]:
    return [
        (m["method"], re.compile(m["regex"], re.IGNORECASE))
        for m in map(RULE.match, _template_lines())
        if m
    ]


def proxy_allows(method: str, path: str) -> bool:
    return any(method == m and rx.search(path) for m, rx in _allow_rules())


def _service_block() -> list[str]:
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index("  docker-socket-proxy:")
    block = []
    for line in lines[start + 1:]:
        if re.match(r"^  \S", line):
            break
        block.append(line)
    return block


def test_the_template_ends_in_a_deny_and_forwards_what_it_allows():
    rules = [line.strip() for line in _template_lines() if line.strip().startswith("http-request")]
    assert rules[-1] == "http-request deny"
    assert not any(line.strip().startswith("http-request deny unless") for line in _template_lines())
    assert any(line.strip() == "default_backend dockerbackend" for line in _template_lines())


def test_each_tier_1_action_is_allowed_through_the_proxy():
    for action, spec in remediation_tool.ALLOWED_DOCKER_ACTIONS.items():
        path = spec["path"].format(name="worker-service")
        assert proxy_allows(spec["method"], path), f"{action} would be denied by the proxy"
        assert proxy_allows(spec["method"], "/v1.43" + path)


def test_container_inspection_is_allowed():
    assert proxy_allows("GET", "/containers/worker-service/json")


@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/containers/create"),
        ("POST", "/containers/worker-service/exec"),
        ("POST", "/containers/restart/exec"),
        ("POST", "/containers/worker-service/stop"),
        ("POST", "/containers/worker-service/kill"),
        ("POST", "/containers/worker-service/start"),
        ("POST", "/containers/worker-service/update"),
        ("POST", "/containers/worker-service/rename"),
        ("POST", "/containers/worker-service/restart/x"),
        ("POST", "/images/create"),
        ("POST", "/build"),
        ("POST", "/networks/create"),
        ("DELETE", "/containers/worker-service"),
        ("PUT", "/containers/worker-service/restart"),
        ("GET", "/containers/worker-service/restart"),
        ("GET", "/containers/json"),
        ("GET", "/containers/worker-service/logs"),
        ("GET", "/events"),
        ("GET", "/images/json"),
        ("GET", "/info"),
        ("GET", "/volumes"),
    ],
)
def test_everything_else_is_denied(method, path):
    assert not proxy_allows(method, path), f"{method} {path} must be denied by the proxy"


def test_compose_mounts_the_template_and_enables_no_proxy_flags():
    block = "\n".join(_service_block())
    assert "./infra/docker-socket-proxy/haproxy.cfg.template:/usr/local/etc/haproxy/haproxy.cfg.template:ro" in block
    assert "environment:" not in block, "access is decided by the template, not env flags"


def test_the_proxy_image_is_pinned_by_digest():
    image = next(line for line in _service_block() if line.strip().startswith("image:"))
    assert re.search(r"@sha256:[0-9a-f]{64}$", image.strip())
