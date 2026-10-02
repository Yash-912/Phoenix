"""Choosing the config rollback target, and refusing when there is nothing safe."""

from datetime import datetime, timedelta, timezone

from phoenix.graph import config_rollback_target
from phoenix.graph.config_rollback_target import resolve_config_rollback_target

SERVICE = "auth-service"
KEY = "DB_POOL_SIZE"


def _marker(value: str, *, minutes_ago: int = 0) -> dict:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "service": SERVICE,
        "timestamp": when.isoformat(),
        "image_tag": "same",
        "config": {KEY: value},
        "deployed_by": "test",
    }


def _history(markers: list[dict], monkeypatch) -> None:
    monkeypatch.setattr(
        config_rollback_target,
        "get_recent_deployments",
        lambda service, limit=10: {"status": "ok", "service": service, "deployments": markers},
    )


def test_the_target_is_the_declared_known_good_value(monkeypatch):
    _history([_marker("1"), _marker("10", minutes_ago=30)], monkeypatch)

    target = resolve_config_rollback_target(SERVICE, KEY, running_value="1")

    assert target.key == KEY
    assert target.value == "10"


def test_the_newest_marker_is_recorded_as_provenance_even_though_it_is_the_regression(monkeypatch):
    """from_value/from_marker name what is currently in force, which is the
    regression itself -- that is what the rollback is undoing."""
    bad = _marker("1")
    _history([bad, _marker("10", minutes_ago=30)], monkeypatch)

    target = resolve_config_rollback_target(SERVICE, KEY, running_value="1")

    assert target.from_marker == bad["timestamp"]
    assert target.from_value == "1"


def test_a_service_already_reporting_the_good_value_is_not_rolled_back_again(monkeypatch):
    """Mirrors the deploy resolver's same refusal: once the service reports the
    known-good value, evidence that has not caught up must not trigger a second
    rollback of an already-healed service."""
    _history([_marker("1")], monkeypatch)

    assert resolve_config_rollback_target(SERVICE, KEY, running_value="10") is None


def test_an_unknown_running_value_still_resolves(monkeypatch):
    """No readable live value means we cannot prove the service is already
    healed, so the refusal must not depend on knowing it."""
    _history([_marker("1")], monkeypatch)

    assert resolve_config_rollback_target(SERVICE, KEY, running_value=None).value == "10"


def test_a_service_with_no_known_good_config_declared_yields_no_target(monkeypatch):
    _history([_marker("1")], monkeypatch)

    assert resolve_config_rollback_target("payment-service", KEY, running_value="1") is None


def test_an_unconfigurable_key_on_a_known_service_yields_no_target(monkeypatch):
    _history([_marker("1")], monkeypatch)

    assert resolve_config_rollback_target(SERVICE, "SOME_OTHER_KEY", running_value="x") is None


def test_no_history_still_resolves_the_declared_value(monkeypatch):
    """The target is declared, not inferred from history -- history only
    supplies from_marker/from_value for the audit trail, so its absence must
    not block the rollback."""
    _history([], monkeypatch)

    target = resolve_config_rollback_target(SERVICE, KEY, running_value="1")

    assert target.value == "10"
    assert target.from_marker is None
    assert target.from_value is None


def test_everything_the_resolver_can_target_the_tool_will_also_accept():
    """The resolver's declared value must be a value the tool's own allowlist
    accepts, or policy would resolve a target the tool then refuses -- a
    rollback that fails at the last step for a reason no reader of the trail
    could see."""
    from phoenix.tools import remediation_tool

    for service, values in config_rollback_target.KNOWN_GOOD_CONFIG.items():
        for key, value in values.items():
            assert service in remediation_tool.ALLOWED_CONFIG_SERVICES
            assert key in remediation_tool.ALLOWED_CONFIG_KEYS
            assert value in remediation_tool.ALLOWED_CONFIG_VALUES.get(key, set())
