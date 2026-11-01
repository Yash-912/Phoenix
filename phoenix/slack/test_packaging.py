"""How the bridge ships: its image, its compose service and the environment template.

These read docker-compose.yml, the Dockerfile and .env.example as text, like
phoenix/test_compose_env.py does for the API: no YAML dependency and no docker. The facts they
hold are the ones that keep the bridge safe to run. It is opt-in, publishes no port, runs as
an ordinary user, takes every secret from the host environment, and keeps the database
password out of the connection URL (a password with a URL-special character would break it).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
DOCKERFILE = ROOT / "phoenix" / "slack" / "Dockerfile"
DOCKERIGNORE = ROOT / "phoenix" / ".dockerignore"
REQUIREMENTS = ROOT / "phoenix" / "slack" / "requirements.txt"

FROM_HOST = (
    "SLACK_APP_TOKEN",
    "SLACK_BOT_TOKEN",
    "PHOENIX_SLACK_CHANNEL",
    "PHOENIX_SLACK_APPROVERS",
    "PHOENIX_APPROVAL_TOKEN",
    "PHOENIX_DASHBOARD_URL",
)
SECRETS = ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "PHOENIX_APPROVAL_TOKEN", "SLACK_BRIDGE_PASSWORD")


def _service(name: str) -> list[str]:
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {name}:")
    block = []
    for line in lines[start + 1:]:
        if re.match(r"^  \S", line) or re.match(r"^\S", line):
            break
        block.append(line)
    return block


def _env_example() -> dict[str, str]:
    values = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


def test_the_service_is_opt_in_so_a_checkout_without_a_slack_app_never_crash_loops():
    block = "\n".join(_service("slack-bridge"))

    assert re.search(r"^\s+profiles:\s*\[\s*\"?slack\"?\s*\]\s*$", block, re.MULTILINE) or re.search(
        r"^\s+profiles:\s*\n\s+- \"?slack\"?\s*$", block, re.MULTILINE
    )


def test_the_service_publishes_no_port_because_socket_mode_is_outbound_only():
    block = "\n".join(_service("slack-bridge"))

    assert "ports:" not in block
    assert "expose:" not in block


def test_every_setting_comes_from_the_host_environment_and_defaults_to_empty():
    block = "\n".join(_service("slack-bridge"))

    for name in FROM_HOST:
        assert re.search(rf"^\s+{name}: \$\{{{name}:-\}}$", block, re.MULTILINE), name


def test_the_service_reaches_the_api_by_its_compose_name_not_through_the_host():
    block = "\n".join(_service("slack-bridge"))

    assert re.search(r"^\s+PHOENIX_API_URL: http://phoenix-api:8000$", block, re.MULTILINE)


def test_the_database_password_is_not_in_the_connection_url():
    block = "\n".join(_service("slack-bridge"))

    url = re.search(r"^\s+DATABASE_URL: (\S+)$", block, re.MULTILINE).group(1)
    assert url == "postgresql://slack_bridge@postgres:5432/phoenix"
    assert re.search(r"^\s+PGPASSWORD: \$\{SLACK_BRIDGE_PASSWORD(:-)?\}$", block, re.MULTILINE)


def test_the_service_connects_as_the_narrow_role_and_never_as_the_agents_or_the_owners():
    block = "\n".join(_service("slack-bridge"))

    for forbidden in ("phoenix_app", "payment_app", "POSTGRES_PASSWORD", "PHOENIX_APP_PASSWORD", "LLM_API_KEY"):
        assert forbidden not in block


def test_the_service_waits_for_a_healthy_database_and_joins_the_lab_network():
    block = "\n".join(_service("slack-bridge"))

    assert re.search(r"postgres:\s*\n\s+condition: service_healthy", block)
    assert "phoenix-net" in block


def test_a_crash_is_retried_a_few_times_not_forever():
    block = "\n".join(_service("slack-bridge"))

    assert re.search(r"^\s+restart: on-failure:\d+$", block, re.MULTILINE)


def test_the_image_is_built_from_the_phoenix_folder_so_the_package_layout_is_kept():
    block = "\n".join(_service("slack-bridge"))

    assert re.search(r"^\s+context: \./phoenix$", block, re.MULTILINE)
    assert re.search(r"^\s+dockerfile: slack/Dockerfile$", block, re.MULTILINE)


def test_postgres_receives_the_bridge_role_password_so_a_fresh_volume_creates_the_role():
    block = "\n".join(_service("postgres"))

    assert re.search(r"^\s+SLACK_BRIDGE_PASSWORD: \$\{SLACK_BRIDGE_PASSWORD(:-)?\}$", block, re.MULTILINE)


def test_the_env_template_names_every_new_setting_with_placeholders_only():
    values = _env_example()

    for name in (*SECRETS, "PHOENIX_SLACK_CHANNEL", "PHOENIX_SLACK_APPROVERS", "PHOENIX_DASHBOARD_URL"):
        assert name in values, f"{name} missing from .env.example"
    for name in SECRETS + ("PHOENIX_SLACK_CHANNEL",):
        assert values[name].lower().startswith("change-me"), f"{name} in .env.example does not look like a placeholder"
    assert re.fullmatch(r"U[A-Z0-9]{8,}=.+", values["PHOENIX_SLACK_APPROVERS"]) or values["PHOENIX_SLACK_APPROVERS"].startswith("change-me")


def test_no_real_looking_slack_token_is_in_the_template():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert not re.search(r"xox[abprs]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}", text)


def test_the_image_runs_the_bridge_as_an_ordinary_user():
    lines = [line.strip() for line in DOCKERFILE.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")]

    assert lines[0] == "FROM python:3.12-slim"
    user_index = next(i for i, line in enumerate(lines) if line.startswith("USER "))
    assert lines[user_index] != "USER root"
    assert user_index < next(i for i, line in enumerate(lines) if line.startswith("CMD "))
    assert lines[-1] == 'CMD ["python", "-m", "phoenix.slack.bridge"]'


def test_the_image_installs_pinned_requirements_and_copies_only_the_bridge_package():
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "pip install --no-cache-dir -r" in text
    assert "COPY __init__.py" in text and "COPY slack" in text
    for forbidden in ("graph", "api", "tools", ".env"):
        assert not re.search(rf"^COPY .*\b{forbidden}\b", text, re.MULTILINE), forbidden


def test_every_requirement_is_pinned_to_an_exact_version():
    lines = [line.strip() for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert lines
    for line in lines:
        assert "==" in line, line


def test_the_build_context_holds_the_bridge_and_nothing_else_and_no_tests():
    rules = [line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]

    assert rules[0] == "*"
    assert "!__init__.py" in rules and "!slack" in rules
    assert any(rule.startswith("slack/test_") or rule == "**/test_*.py" for rule in rules)
    assert any("__pycache__" in rule for rule in rules)
