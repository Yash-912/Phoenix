"""Choosing the rollback target, and refusing when there is nothing safe."""

from datetime import datetime, timedelta, timezone

import pytest

from phoenix.graph import rollback_target
from phoenix.graph.rollback_target import resolve_rollback_target

SERVICE = "checkout-service"


def _marker(version: str, *, regression: bool = False, minutes_ago: int = 0) -> dict:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "service": SERVICE,
        "timestamp": when.isoformat(),
        "image_tag": version,
        "config": {"regression": regression},
        "deployed_by": "test",
    }


def _history(markers: list[dict], monkeypatch) -> None:
    monkeypatch.setattr(
        rollback_target,
        "get_recent_deployments",
        lambda service, limit=10: {"status": "ok", "service": service, "deployments": markers},
    )


def test_the_target_is_the_newest_known_good_artifact_not_the_previous_one(monkeypatch):
    """Rolling back to whatever ran last is how the release before the bad one
    gets restored, which in a real incident is often the broken one."""
    _history([_marker("v18", regression=True), _marker("v17"), _marker("v16", minutes_ago=5)], monkeypatch)

    target = resolve_rollback_target(SERVICE, running_version="v18")

    assert target.version == "v17"
    assert target.from_version == "v18"


def test_the_marker_being_reverted_is_recorded_as_provenance(monkeypatch):
    """The new marker needs to name what it undid, so the chain reads from
    either end rather than only forwards."""
    bad = _marker("v18", regression=True)
    _history([bad, _marker("v17")], monkeypatch)

    target = resolve_rollback_target(SERVICE, running_version="v18")

    assert target.from_marker == bad["timestamp"]


def test_a_service_already_running_the_good_artifact_is_not_rolled_back_again(monkeypatch):
    """Scoring keys on the version appearing in labels and logs, so right after a
    successful rollback the stale v18 evidence still scores 0.95. Without this the
    graph would roll back a healed service a second time."""
    _history([_marker("v18", regression=True), _marker("v17")], monkeypatch)

    assert resolve_rollback_target(SERVICE, running_version="v17") is None


def test_a_history_with_nothing_known_good_yields_no_target(monkeypatch):
    _history([_marker("v18", regression=True)], monkeypatch)

    assert resolve_rollback_target(SERVICE, running_version="v18") is None


def test_a_version_named_as_good_but_marked_a_regression_is_not_a_target(monkeypatch):
    """The regression flag is the more specific fact. A version name is not
    evidence that a release was healthy, so a marker claiming v17 while also
    recording that it carries the regression is refused."""
    _history([_marker("v18", regression=True), _marker("v17", regression=True)], monkeypatch)

    assert resolve_rollback_target(SERVICE, running_version="v18") is None


def test_the_regression_flag_is_what_excludes_a_version_the_allowlist_admits(monkeypatch):
    """Pins the difference between the two allowlists: v17 is an artifact the
    tool may deploy, but this marker says this particular deployment carried the
    regression, so it is not a restore candidate."""
    from phoenix.tools import remediation_tool

    _history([_marker("v18", regression=True), _marker("v17", regression=True)], monkeypatch)

    assert "v17" in remediation_tool.ALLOWED_ROLLBACK_VERSIONS
    assert resolve_rollback_target(SERVICE, running_version="v18") is None


def test_no_history_at_all_yields_no_target(monkeypatch):
    _history([], monkeypatch)

    assert resolve_rollback_target(SERVICE, running_version="v18") is None


def test_an_unknown_running_version_still_resolves(monkeypatch):
    """No label means we cannot prove the service is already healed, so the
    refusal must not depend on knowing it."""
    _history([_marker("v18", regression=True), _marker("v17")], monkeypatch)

    assert resolve_rollback_target(SERVICE, running_version=None).version == "v17"


def test_a_version_outside_the_allowlist_is_never_a_target(monkeypatch):
    _history([_marker("latest"), _marker("v17")], monkeypatch)

    assert resolve_rollback_target(SERVICE, running_version="v18").version == "v17"


def test_everything_policy_can_resolve_the_tool_will_also_accept():
    """The two allowlists answer different questions and are deliberately not
    equal: the tool's is "artifacts that may be deployed at all" (v17 and v18),
    while the resolver's is "artifacts known to be healthy" (v17 only). What must
    hold is that the narrower is a subset of the wider, or policy would resolve a
    target the tool then refuses -- a rollback that fails at the last step for a
    reason no reader of the trail could see.
    """
    from phoenix.tools import remediation_tool

    assert rollback_target.KNOWN_GOOD_VERSIONS <= remediation_tool.ALLOWED_ROLLBACK_VERSIONS
    assert "v18" in remediation_tool.ALLOWED_ROLLBACK_VERSIONS
    assert "v18" not in rollback_target.KNOWN_GOOD_VERSIONS


def test_a_version_string_cannot_carry_a_shell_metacharacter_into_the_allowlist():
    """Not a security claim about the subprocess -- argv is fixed and shell=False
    elsewhere. This pins that the membership test is an exact match, so a value
    that merely contains a valid version is still refused."""
    for hostile in ("v17; rm -rf /", " v17", "v17 ", "v17&&whoami", "$v17"):
        assert hostile not in rollback_target.KNOWN_GOOD_VERSIONS, hostile


@pytest.mark.parametrize("version", sorted({"v17"}))
def test_the_resolver_returns_the_version_the_tool_would_accept(version, monkeypatch):
    _history([_marker("v18", regression=True), _marker(version)], monkeypatch)

    assert resolve_rollback_target(SERVICE, running_version="v18").version == version
