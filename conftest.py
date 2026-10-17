"""Project-wide test isolation for deployment history.

chaos.lib.deploy_tracker.write_deployment_marker writes to
PHOENIX_DEPLOYMENTS_ROOT (default: the repository's own deployments/
directory -- the same one the live lab and a real investigation read from).
Several tests call apply_deployment/apply_config directly without mocking
write_deployment_marker (they mock subprocess.run and _inspect_container
instead, to prove the argv and environment are correct), so without this
fixture every test run writes real marker files into the repo's live
deployment history -- fabricated entries with a fake "sha256:abc" digest and a
literal "chaos/deploy.py" label, indistinguishable from genuine history to
rollback_target.py and correlation.py, and timestamped newer than anything
real. A live investigation run after `pytest` would make its Tier 2 decision
on test fixtures.

Redirecting every test to a fresh temp directory is the fix, not auditing each
test for whether it happens to mock write_deployment_marker today: a future
test that calls apply_deployment/apply_config and forgets to mock it would
reintroduce the same leak silently.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_deployment_history(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOENIX_DEPLOYMENTS_ROOT", str(tmp_path / "deployments"))


@pytest.fixture(autouse=True)
def _reset_incident_onset():
    """Clear the incident onset that a graph run leaves in deploy_tool.

    nodes.py sets deploy_tool's module-level incident start time on every
    run, and nothing clears it. A test that runs a graph would otherwise
    leak its onset into a later test that reads it (the latency window is
    anchored to it), so the result depended on test order.
    """
    from phoenix.tools import deploy_tool

    deploy_tool.set_incident_started_at(None)
    yield
    deploy_tool.set_incident_started_at(None)
