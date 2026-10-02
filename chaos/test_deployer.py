"""Applying a deployment, and the boundary that keeps it from being arbitrary.

These tests never run docker. The point is that the refusals happen before any
process is spawned, and that the argv is assembled from constants rather than
from anything a caller passed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chaos.lib import deployer

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Recorder:
    """Stands in for subprocess.run, recording argv without executing it."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.returncode = returncode
        self.stdout = ""
        self.stderr = "compose failed"
        # Overrides keyed on argv[1], so a test can make one docker subcommand
        # fail while the rest succeed. Anything unset uses self.returncode.
        self.returncodes: dict[str, int] = {}

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        subcommand = argv[1] if len(argv) > 1 else ""
        return subprocess.CompletedProcess(
            argv, self.returncodes.get(subcommand, self.returncode), self.stdout, self.stderr
        )


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(deployer.subprocess, "run", rec)
    monkeypatch.setattr(deployer, "_inspect_container", lambda service: ("sha256:abc", "v17"))
    return rec


def _env_for(recorder: _Recorder, version: str) -> dict:
    """The environment compose was invoked with on the call that names `version`."""
    for argv, kwargs in recorder.calls:
        if version in kwargs["env"].get("CHECKOUT_VERSION", ""):
            return kwargs["env"]
    raise AssertionError(f"no compose call carried CHECKOUT_VERSION={version}; saw {recorder.calls}")


# --- refusals: nothing may be spawned for any of these ---------------------


@pytest.mark.parametrize("service", ["worker-service", "postgres", "", "checkout-service; rm -rf /"])
def test_a_service_outside_the_deployable_set_is_refused_before_any_process_starts(recorder, service):
    with pytest.raises(deployer.DeploymentError, match="not deployable"):
        deployer.apply_deployment(service, "v17")

    assert recorder.calls == []


@pytest.mark.parametrize(
    "version",
    ["v19", "latest", "", "v17 && curl evil.example", "../../etc/passwd", "v17:regression"],
)
def test_a_version_outside_the_known_set_is_refused_before_any_process_starts(recorder, version):
    with pytest.raises(deployer.DeploymentError, match="unknown version"):
        deployer.apply_deployment("checkout-service", version)

    assert recorder.calls == []


def test_a_build_failure_raises_rather_than_recording_a_deployment_that_did_not_happen(recorder):
    recorder.returncode = 1

    with pytest.raises(deployer.DeploymentError, match="build"):
        deployer.apply_deployment("checkout-service", "v17")


def test_a_failed_recreate_raises_rather_than_writing_a_marker(recorder, monkeypatch):
    """A marker is a claim that the container is up. Writing one after a failed
    compose would leave history claiming a deployment that did not occur."""
    monkeypatch.setattr(deployer, "build_artifact", lambda version: None)
    monkeypatch.setattr(
        deployer.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "container refused to start"),
    )

    written = []
    monkeypatch.setattr(
        deployer,
        "write_deployment_marker",
        lambda **kwargs: written.append(kwargs) or {"timestamp": "now"},
    )

    with pytest.raises(deployer.DeploymentError, match="failed"):
        deployer.apply_deployment("checkout-service", "v17")

    assert written == []


# --- the argv is fixed ------------------------------------------------------


def test_the_compose_invocation_carries_no_caller_supplied_string(recorder):
    deployer.apply_deployment("checkout-service", "v17")

    argv = recorder.calls[0][0]
    assert argv[0:2] == ["docker", "compose"]
    assert argv[3] == deployer.COMPOSE_PROJECT
    assert argv[5] == str(deployer.COMPOSE_FILE)
    # Nothing beyond the fixed prefix and the fixed service name.
    assert set(argv) <= {"docker", "compose", deployer.COMPOSE_PROJECT, str(deployer.COMPOSE_FILE),
                         "-p", "-f", "build", "up", "-d", "--no-deps", "--force-recreate",
                         "checkout-service"}


def test_the_subprocess_is_never_run_through_a_shell(recorder):
    deployer.apply_deployment("checkout-service", "v17")

    for _argv, kwargs in recorder.calls:
        assert kwargs["shell"] is False


def test_a_recreate_is_forced_so_compose_cannot_report_a_successful_no_op(recorder):
    """Without --force-recreate, compose sees the same service definition, leaves
    the old container running, and exits 0 -- a rollback that changed nothing
    while appearing to succeed."""
    deployer.apply_deployment("checkout-service", "v17")

    up_call = next(argv for argv, _ in recorder.calls if "up" in argv)
    assert "--force-recreate" in up_call
    assert up_call[-1] == "checkout-service"


def test_the_version_moves_the_tag_build_args_and_label_together(recorder):
    """One variable selects the artifact. If these could disagree, the label
    could claim v17 while the image runs v18 and verification would pass on a
    lie."""
    deployer.apply_deployment("checkout-service", "v18")

    env = _env_for(recorder, "v18")
    assert env["CHECKOUT_VERSION"] == "v18"
    assert env["CHECKOUT_REGRESSION"] == "true"

    deployer.apply_deployment("checkout-service", "v17")
    assert _env_for(recorder, "v17")["CHECKOUT_REGRESSION"] == "false"


def test_the_regression_flag_follows_the_artifact_rather_than_the_caller(recorder):
    """A caller cannot ask for v18 while suppressing its regression, which would
    produce an artifact that is neither of the two under test."""
    deployer.apply_deployment("checkout-service", "v18")

    assert _env_for(recorder, "v18")["CHECKOUT_REGRESSION"] == "true"


# --- the marker describes what actually started -----------------------------


def test_the_marker_records_the_digest_read_back_from_the_live_container(recorder, monkeypatch):
    monkeypatch.setattr(deployer, "_inspect_container", lambda service: ("sha256:deadbeef", "v18"))

    written = {}
    monkeypatch.setattr(
        deployer,
        "write_deployment_marker",
        lambda **kwargs: written.update(kwargs) or {"timestamp": "now"},
    )

    result = deployer.apply_deployment("checkout-service", "v18")

    assert written["image_digest"] == "sha256:deadbeef"
    assert result["observed_label_version"] == "v18"


def test_an_unreadable_container_does_not_fail_a_deployment_that_worked(recorder, monkeypatch):
    """A proxy hiccup must not be reported as a failed deploy -- but the marker
    has to say the identity was never confirmed."""
    monkeypatch.setattr(deployer, "_inspect_container", lambda service: (None, None))

    written = {}
    monkeypatch.setattr(
        deployer,
        "write_deployment_marker",
        lambda **kwargs: written.update(kwargs) or {"timestamp": "now"},
    )

    result = deployer.apply_deployment("checkout-service", "v17")

    assert written["image_digest"] is None
    assert result["observed_label_version"] is None


def test_a_rollback_records_the_marker_it_reverted(recorder, monkeypatch):
    written = {}
    monkeypatch.setattr(
        deployer,
        "write_deployment_marker",
        lambda **kwargs: written.update(kwargs) or {"timestamp": "now"},
    )

    deployer.apply_deployment("checkout-service", "v17", rolled_back_from="2026-10-02T08:00:00+00-00.json")

    assert written["rolled_back_from"] == "2026-10-02T08:00:00+00-00.json"
    assert written["deployed_by"] == "chaos/deploy.py"


# --- a rollback restores an artifact; it does not reconstruct one ------------
#
# v17 and v18 are the same source tree with different compile-time values, so
# rebuilding v17 from today's checkout yields whatever the checkout is today with
# REGRESSION_ENABLED=false. That is not necessarily the v17 that was verified
# good. The tag is what preserves a known-good artifact, and a rollback that
# rebuilds is quietly relying on the source not having moved.


def test_a_rollback_does_not_rebuild_the_artifact_it_is_restoring(recorder, monkeypatch):
    built: list[str] = []
    monkeypatch.setattr(deployer, "build_artifact", lambda version: built.append(version))

    deployer.apply_deployment("checkout-service", "v17", build=False)

    assert built == []
    assert not any("build" in argv for argv, _ in recorder.calls)


def test_a_rollback_refuses_when_the_artifact_was_never_built(recorder, monkeypatch):
    """Absent the image, compose would pull one from a registry -- an artifact
    nobody in this lab ever verified -- so the deployer stops first."""
    recorder.returncodes["image"] = 1
    monkeypatch.setattr(deployer, "build_artifact", lambda version: pytest.fail("must not build"))

    with pytest.raises(deployer.DeploymentError, match="v17"):
        deployer.apply_deployment("checkout-service", "v17", build=False)

    assert not any("up" in argv for argv, _ in recorder.calls), "no container may be started"


def test_a_rollback_asks_docker_about_the_artifact_without_a_caller_supplied_string(recorder):
    recorder.returncodes["image"] = 1

    with pytest.raises(deployer.DeploymentError):
        deployer.apply_deployment("checkout-service", "v17", build=False)

    inspect_call = next(argv for argv, _ in recorder.calls if argv[1] == "image")
    assert inspect_call[-1] == f"{deployer.IMAGE_REPO}:v17"
    assert set(inspect_call) <= {"docker", "image", "inspect", f"{deployer.IMAGE_REPO}:v17"}


def test_a_forward_release_still_builds_the_artifact_it_is_publishing(recorder, monkeypatch):
    """The rollback path is the one that must not build. Publishing a version
    nobody has built yet is the whole job of the chaos script."""
    built: list[str] = []
    monkeypatch.setattr(deployer, "build_artifact", lambda version: built.append(version))

    deployer.apply_deployment("checkout-service", "v18")

    assert built == ["v18"]


def test_an_image_inspection_failure_does_not_read_as_absence_of_the_artifact(recorder, monkeypatch):
    """An unreadable docker daemon must not be silently treated as 'the good
    image is gone' -- that would block a valid rollback mid-incident."""
    monkeypatch.setattr(
        deployer,
        "_image_lookup",
        lambda version: None,
    )

    deployer.apply_deployment("checkout-service", "v17", build=False)

    assert any("up" in argv for argv, _ in recorder.calls)


# --- apply_config: refusals, nothing may be spawned for any of these --------


@pytest.mark.parametrize("service", ["checkout-service", "postgres", "", "auth-service; rm -rf /"])
def test_a_config_service_outside_the_configurable_set_is_refused_before_any_process_starts(recorder, service):
    with pytest.raises(deployer.DeploymentError, match="not configurable"):
        deployer.apply_config(service, {"DB_POOL_SIZE": "10"})

    assert recorder.calls == []


def test_a_config_key_outside_the_known_set_is_refused_before_any_process_starts(recorder):
    with pytest.raises(deployer.DeploymentError, match="not configurable"):
        deployer.apply_config("auth-service", {"REDIS_URL": "redis://evil"})

    assert recorder.calls == []


def test_a_config_value_outside_the_known_set_is_refused_before_any_process_starts(recorder):
    with pytest.raises(deployer.DeploymentError, match="not a known state"):
        deployer.apply_config("auth-service", {"DB_POOL_SIZE": "9999"})

    assert recorder.calls == []


def test_a_config_recreate_failure_raises_rather_than_writing_a_marker(recorder, monkeypatch):
    recorder.returncode = 1

    written = []
    monkeypatch.setattr(
        deployer,
        "write_deployment_marker",
        lambda **kwargs: written.append(kwargs) or {"timestamp": "now"},
    )

    with pytest.raises(deployer.DeploymentError, match="failed"):
        deployer.apply_config("auth-service", {"DB_POOL_SIZE": "10"})

    assert written == []


# --- apply_config: the argv and environment are fixed to validated input ----


def test_apply_config_never_builds(recorder, monkeypatch):
    """A config value is a runtime input; there is no artifact for build_artifact
    to produce and nothing it would be building towards."""
    built: list[str] = []
    monkeypatch.setattr(deployer, "build_artifact", lambda version: built.append(version))

    deployer.apply_config("auth-service", {"DB_POOL_SIZE": "10"})

    assert built == []
    assert not any("build" in argv for argv, _ in recorder.calls)


def test_apply_config_recreates_so_the_new_value_reaches_the_process(recorder):
    deployer.apply_config("auth-service", {"DB_POOL_SIZE": "10"})

    up_call = next(argv for argv, _ in recorder.calls if "up" in argv)
    assert "--force-recreate" in up_call
    assert up_call[-1] == "auth-service"


def test_apply_config_is_never_run_through_a_shell(recorder):
    deployer.apply_config("auth-service", {"DB_POOL_SIZE": "10"})

    for _argv, kwargs in recorder.calls:
        assert kwargs["shell"] is False


def test_apply_config_maps_the_key_to_its_own_compose_variable(recorder):
    """DB_POOL_SIZE is the container's own variable name; AUTH_DB_POOL_SIZE is the
    compose-level one, kept distinct so compose actually sees a changed value on
    the reset back to the container's own default."""
    deployer.apply_config("auth-service", {"DB_POOL_SIZE": "10"})

    up_call_kwargs = next(kwargs for argv, kwargs in recorder.calls if "up" in argv)
    assert up_call_kwargs["env"]["AUTH_DB_POOL_SIZE"] == "10"
    assert "DB_POOL_SIZE" not in up_call_kwargs["env"]


def test_apply_config_records_a_marker_with_no_artifact_change(recorder, monkeypatch):
    written = {}
    monkeypatch.setattr(
        deployer,
        "write_deployment_marker",
        lambda **kwargs: written.update(kwargs) or {"timestamp": "now"},
    )

    deployer.apply_config("auth-service", {"DB_POOL_SIZE": "10"})

    assert written["image_tag"] == "same"
    assert written["config"] == {"DB_POOL_SIZE": "10"}


def test_apply_config_records_the_marker_it_reverted(recorder, monkeypatch):
    written = {}
    monkeypatch.setattr(
        deployer,
        "write_deployment_marker",
        lambda **kwargs: written.update(kwargs) or {"timestamp": "now"},
    )

    deployer.apply_config(
        "auth-service",
        {"DB_POOL_SIZE": "10"},
        rolled_back_from="2026-10-02T08:00:00+00-00.json",
    )

    assert written["rolled_back_from"] == "2026-10-02T08:00:00+00-00.json"
