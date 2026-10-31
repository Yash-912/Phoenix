"""The phoenix-api service receives the approval settings it reads, and the env template names them.

phoenix/api/main.py refuses every approval decision when PHOENIX_APPROVAL_TOKEN is
empty, and the compose file used to pass only DATABASE_URL, so the dashboard's
approve button always got a 503. These tests read docker-compose.yml and
.env.example as text (no YAML dependency, no docker).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"

API_READS = ("PHOENIX_APPROVAL_TOKEN", "PHOENIX_APPROVERS")
TEMPLATE_NAMES = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "CURVET_APP_KEY",
    "CURVET_APP_SECRET",
    "PHOENIX_APPROVAL_TOKEN",
    "PHOENIX_APPROVERS",
)


def _phoenix_api_block() -> list[str]:
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index("  phoenix-api:")
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


def test_phoenix_api_passes_the_approval_settings_from_the_host_environment():
    block = "\n".join(_phoenix_api_block())
    for name in API_READS:
        assert re.search(rf"^\s+{name}: \$\{{{name}:-\}}$", block, re.MULTILINE), name


def test_the_approval_token_defaults_to_empty_so_the_endpoint_stays_closed():
    block = "\n".join(_phoenix_api_block())
    assert "PHOENIX_APPROVAL_TOKEN: ${PHOENIX_APPROVAL_TOKEN:-}" in block


def test_the_env_template_names_the_llm_curvet_and_approval_settings():
    values = _env_example()
    for name in TEMPLATE_NAMES:
        assert name in values, f"{name} missing from .env.example"


def test_the_env_template_holds_placeholders_only():
    values = _env_example()
    for name in TEMPLATE_NAMES:
        value = values[name].lower()
        assert value.startswith("change-me") or "example.invalid" in value or value == "approver-name", (
            f"{name} in .env.example does not look like a placeholder"
        )
