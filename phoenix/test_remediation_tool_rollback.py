"""The Tier 2 entry point: what it refuses, and what it hands the deployer.

The interesting property is not that rollback works -- the live run proves that
-- but that nothing a caller supplies can become part of the command. These tests
pin the refusals that happen before any process is spawned.
"""

from __future__ import annotations

import pytest

from phoenix.tools import remediation_tool


class _ExplodingDeployer:
    def __call__(self, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError(f"the deployer was invoked with {kwargs!r}")


@pytest.fixture
def no_deploy(monkeypatch):
    """Replace apply_deployment so a passing test proves nothing was executed."""
    sentinel = _ExplodingDeployer()
    monkeypatch.setattr(
        remediation_tool,
        "apply_deployment",
        sentinel,
        raising=False,
    )
    return sentinel


@pytest.fixture
def guarded(monkeypatch):
    """Make the module import the stubbed deployer.

    rollback_deployment imports chaos.lib.deployer lazily so the tool module does
    not depend on the lab at import time. That lazy import is why the patch
    target here is the deployer module itself.
    """
    from chaos.lib import deployer

    calls: list[dict] = []

    def fake_apply(**kwargs):
        calls.append(kwargs)
        return {
            "timestamp": "2026-10-02T09:00:00+00:00",
            "image_digest": "sha256:abc",
            "observed_label_version": kwargs.get("version"),
        }

    monkeypatch.setattr(deployer, "apply_deployment", fake_apply)
    return calls


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize("service", ["worker-service", "postgres", "", "checkout-service; rm -rf /"])
def test_a_service_outside_the_allowlist_is_refused_without_deploying(service, no_deploy):
    result = remediation_tool.rollback_deployment(service, "v17")

    assert result["status"] == "error"
    assert "not rollback-eligible" in result["error"]


@pytest.mark.parametrize(
    "version",
    ["v19", "latest", "", "v17; rm -rf /", "$(whoami)", "v17 && curl evil.example", " v17"],
)
def test_a_version_outside_the_allowlist_is_refused_without_deploying(version, no_deploy):
    result = remediation_tool.rollback_deployment("checkout-service", version)

    assert result["status"] == "error"
    assert "not a known-good artifact" in result["error"]


def test_the_refusal_names_what_was_allowed_so_the_trail_is_readable(no_deploy):
    """An error a reader cannot act on is only marginally better than no error."""
    result = remediation_tool.rollback_deployment("checkout-service", "v19")

    assert result["allowed"] == ["v17", "v18"]


# --- the success path -------------------------------------------------------


def test_a_valid_rollback_reaches_the_deployer_with_the_service_and_version(guarded):
    result = remediation_tool.rollback_deployment("checkout-service", "v17")

    assert result["status"] == "ok"
    assert guarded == [
        {
            "service": "checkout-service",
            "version": "v17",
            "deployed_by": "phoenix/remediation_tool.rollback_deployment",
            "rolled_back_from": None,
            "build": False,
        }
    ]


def test_the_result_reports_the_artifact_that_is_now_running(guarded):
    """Verification reads this, so the value that gets recorded is the one read
    back from the live container rather than the one that was requested."""
    result = remediation_tool.rollback_deployment("checkout-service", "v17")

    assert result["to_version"] == "v17"
    assert result["observed_label_version"] == "v17"
    assert result["image_digest"] == "sha256:abc"


def test_a_rollback_restores_the_known_good_artifact_rather_than_rebuilding_it(guarded):
    """v17 and v18 are one source tree with different compile-time values, so
    rebuilding v17 from today's checkout is not necessarily the v17 that was
    verified good. The rollback must restore the artifact, not reconstruct one."""
    remediation_tool.rollback_deployment("checkout-service", "v17")

    assert guarded[0]["build"] is False


def test_the_marker_being_reverted_is_carried_through_for_the_trail(guarded):
    remediation_tool.rollback_deployment(
        "checkout-service", "v17", rolled_back_from="2026-10-02T08:00:00+00-00.json"
    )

    assert guarded[0]["rolled_back_from"] == "2026-10-02T08:00:00+00-00.json"


def test_a_deployer_failure_is_reported_as_an_error_result_not_raised(monkeypatch):
    """The remediator already handles a failed action; raising past it would skip
    the audit row recording that the rollback was attempted."""
    from chaos.lib import deployer

    def explode(**kwargs):
        raise deployer.DeploymentError("compose exited 1: port already allocated")

    monkeypatch.setattr(deployer, "apply_deployment", explode)

    result = remediation_tool.rollback_deployment("checkout-service", "v17")

    assert result["status"] == "error"
    assert "port already allocated" in result["error"]


# --- it is not one of the proxy's Tier 1 verbs ------------------------------


def test_rollback_is_not_in_the_tier_1_proxy_allowlist():
    """It needs DELETE and NETWORKS on the socket, which the proxy denies. Its
    absence from this table is why the compose path exists at all."""
    assert "rollback_deployment" not in remediation_tool.ALLOWED_DOCKER_ACTIONS
    assert set(remediation_tool.ALLOWED_DOCKER_ACTIONS) == {
        "restart_service",
        "pause_worker",
        "resume_worker",
    }


def test_the_rollback_allowlists_are_declared_next_to_the_proxy_one():
    from phoenix.graph import rollback_target

    assert "checkout-service" in remediation_tool.ALLOWED_ROLLBACK_SERVICES
    assert rollback_target.KNOWN_GOOD_VERSIONS <= remediation_tool.ALLOWED_ROLLBACK_VERSIONS
