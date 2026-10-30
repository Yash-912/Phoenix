"""reset_all is the one-command return to healthy, so it has to undo every scenario.

checkout-service's fault lives in the image, not in a flag an endpoint can clear,
so reset_all redeploys the good version when the running one is not it -- and
leaves a healthy checkout alone, since each redeploy recreates the container and
writes a deployment marker."""

from chaos import deploy_bad_v18, reset_all


class _Response:
    status_code = 200
    text = "ok"

    def raise_for_status(self):
        return None


import subprocess

import pytest

_REAL_UNPAUSE = reset_all._unpause_lab_containers


@pytest.fixture(autouse=True)
def _no_real_docker(monkeypatch):
    """main() asks docker which containers are paused; no test here may reach it."""
    monkeypatch.setattr(reset_all, "_unpause_lab_containers", lambda: True)


def _stub_everything_else(monkeypatch):
    monkeypatch.setattr(reset_all.requests, "post", lambda *a, **k: _Response())
    monkeypatch.setattr(reset_all.config_pool, "reset", lambda: {"config": {"DB_POOL_SIZE": "10"}})


def test_a_checkout_left_on_the_bad_version_is_redeployed_as_the_good_one(monkeypatch):
    _stub_everything_else(monkeypatch)
    monkeypatch.setattr(reset_all, "_checkout_version", lambda: deploy_bad_v18.BAD_VERSION)
    calls = []
    monkeypatch.setattr(reset_all.deploy_bad_v18, "reset", lambda: calls.append("reset"))

    reset_all.main()

    assert calls == ["reset"]


def test_a_checkout_already_on_the_good_version_is_not_redeployed(monkeypatch):
    _stub_everything_else(monkeypatch)
    monkeypatch.setattr(reset_all, "_checkout_version", lambda: deploy_bad_v18.GOOD_VERSION)
    monkeypatch.setattr(reset_all.deploy_bad_v18, "reset", lambda: (_ for _ in ()).throw(AssertionError("redeployed")))

    reset_all.main()


def test_a_checkout_whose_version_cannot_be_read_is_redeployed_rather_than_assumed_healthy(monkeypatch):
    _stub_everything_else(monkeypatch)
    monkeypatch.setattr(reset_all, "_checkout_version", lambda: None)
    calls = []
    monkeypatch.setattr(reset_all.deploy_bad_v18, "reset", lambda: calls.append("reset"))

    reset_all.main()

    assert calls == ["reset"]


def test_a_failed_checkout_redeploy_is_reported_and_does_not_stop_the_rest(monkeypatch, capsys):
    _stub_everything_else(monkeypatch)
    monkeypatch.setattr(reset_all, "_checkout_version", lambda: deploy_bad_v18.BAD_VERSION)

    def fail():
        raise reset_all.DeploymentError("image missing")

    monkeypatch.setattr(reset_all.deploy_bad_v18, "reset", fail)

    reset_all.main()

    assert "checkout-service: FAILED image missing" in capsys.readouterr().out


# ---- a reset leaves a record that the fault was reverted -------------------------------------

from datetime import datetime, timedelta, timezone

from chaos.lib import deploy_tracker
from phoenix.graph import correlation


def _write_fault(service: str, image_tag: str, config: dict) -> None:
    deploy_tracker.write_deployment_marker(service=service, image_tag=image_tag, config=config, deployed_by="chaos")


def _stubbed_reset(monkeypatch):
    _stub_everything_else(monkeypatch)
    monkeypatch.setattr(reset_all, "_checkout_version", lambda: deploy_bad_v18.GOOD_VERSION)


def test_a_slow_query_left_on_is_recorded_as_reverted(monkeypatch):
    _stubbed_reset(monkeypatch)
    _write_fault("payment-service", "slow-query", {"slow_query": True})

    reset_all.main()

    latest = deploy_tracker.get_recent_deployments("payment-service", 1)[0]
    assert latest["config"] == {"slow_query": False}
    assert "--reset" in latest["deployed_by"]


def test_a_leak_left_on_is_recorded_as_reverted(monkeypatch):
    _stubbed_reset(monkeypatch)
    _write_fault("worker-service", "leaky", {"leak": True})

    reset_all.main()

    assert deploy_tracker.get_recent_deployments("worker-service", 1)[0]["config"] == {"leak": False}


def test_a_service_that_was_already_healthy_gets_no_marker(monkeypatch):
    """Each marker is history the agent reads, so a no-op reset must not add one."""
    _stubbed_reset(monkeypatch)
    _write_fault("payment-service", "healthy", {"slow_query": False})
    before = len(deploy_tracker.get_recent_deployments("payment-service", 50))

    reset_all.main()

    assert len(deploy_tracker.get_recent_deployments("payment-service", 50)) == before


def test_a_service_with_no_history_gets_no_marker(monkeypatch):
    _stubbed_reset(monkeypatch)

    reset_all.main()

    assert deploy_tracker.get_recent_deployments("payment-service", 5) == []
    assert deploy_tracker.get_recent_deployments("worker-service", 5) == []


def test_the_endpoint_is_still_called_when_there_is_nothing_to_record(monkeypatch):
    posted = []
    monkeypatch.setattr(reset_all.requests, "post",
                        lambda url, **k: posted.append(url) or _Response())
    monkeypatch.setattr(reset_all.config_pool, "reset", lambda: {"config": {"DB_POOL_SIZE": "10"}})
    monkeypatch.setattr(reset_all, "_checkout_version", lambda: deploy_bad_v18.GOOD_VERSION)

    reset_all.main()

    assert any(url.endswith("/chaos/slow/disable") for url in posted)
    assert any(url.endswith("/chaos/leak/stop") for url in posted)


def test_an_old_fault_marker_is_not_authoritative_after_the_reset_that_reverted_it(monkeypatch):
    """The stale-evidence failure end to end: write a fault, reset, then read the history
    the way the investigation does, for an incident that begins afterwards."""
    _stubbed_reset(monkeypatch)
    _write_fault("payment-service", "slow-query", {"slow_query": True})
    reset_all.main()
    onset = datetime.now(timezone.utc) + timedelta(seconds=2)

    annotated = correlation.correlate_all(deploy_tracker.get_recent_deployments("payment-service", 10), onset)

    verdicts = {m["image_tag"]: m["correlation"] for m in annotated}
    assert verdicts["slow-query"] == "superseded"
    assert verdicts["healthy"] == "before_incident"


# ---- paused containers are unpaused, and a failed step is a failed reset --------------------------


def _docker(monkeypatch, paused, fail_unpause=(), ps_fails=False):
    """A docker CLI with `paused` containers paused. Returns the argvs it was run with."""
    ran = []

    def run(argv, **kwargs):
        ran.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            if ps_fails:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="cannot connect to the docker daemon")
            return subprocess.CompletedProcess(argv, 0, stdout="".join(f"{name}\n" for name in paused), stderr="")
        if argv[:2] == ["docker", "unpause"]:
            code = 1 if argv[2] in fail_unpause else 0
            return subprocess.CompletedProcess(argv, code, stdout="", stderr="error" if code else "")
        raise AssertionError(f"unexpected docker call {argv}")

    monkeypatch.setattr(reset_all, "_unpause_lab_containers", _REAL_UNPAUSE)
    monkeypatch.setattr(reset_all.subprocess, "run", run)
    return ran


def test_every_paused_lab_container_is_unpaused(monkeypatch):
    ran = _docker(monkeypatch, ["worker-service", "payment-service"])

    assert reset_all._unpause_lab_containers() is True

    assert ["docker", "unpause", "worker-service"] in ran
    assert ["docker", "unpause", "payment-service"] in ran


def test_a_paused_container_that_is_not_part_of_the_lab_is_left_alone(monkeypatch):
    ran = _docker(monkeypatch, ["someone-elses-database", "worker-service"])

    reset_all._unpause_lab_containers()

    assert ["docker", "unpause", "someone-elses-database"] not in ran
    assert ["docker", "unpause", "worker-service"] in ran


def test_nothing_paused_means_no_unpause_calls(monkeypatch):
    ran = _docker(monkeypatch, [])

    assert reset_all._unpause_lab_containers() is True
    assert [a for a in ran if a[1] == "unpause"] == []


def test_a_failed_unpause_is_reported_as_a_failure(monkeypatch, capsys):
    _docker(monkeypatch, ["worker-service", "payment-service"], fail_unpause=("worker-service",))

    assert reset_all._unpause_lab_containers() is False
    assert "worker-service: unpause FAILED" in capsys.readouterr().out


def test_not_being_able_to_ask_docker_is_a_failure_not_a_clean_reset(monkeypatch):
    _docker(monkeypatch, [], ps_fails=True)

    assert reset_all._unpause_lab_containers() is False


def test_unpausing_comes_before_the_http_resets(monkeypatch):
    order = []
    monkeypatch.setattr(reset_all, "_unpause_lab_containers", lambda: order.append("unpause") or True)
    monkeypatch.setattr(reset_all.requests, "post", lambda url, **k: order.append(url) or _Response())
    monkeypatch.setattr(reset_all.config_pool, "reset", lambda: {"config": {"DB_POOL_SIZE": "10"}})
    monkeypatch.setattr(reset_all, "_checkout_version", lambda: deploy_bad_v18.GOOD_VERSION)

    reset_all.main()

    assert order[0] == "unpause"


def test_the_worker_consumer_is_resumed(monkeypatch):
    posted = []
    monkeypatch.setattr(reset_all.requests, "post", lambda url, **k: posted.append(url) or _Response())
    monkeypatch.setattr(reset_all.config_pool, "reset", lambda: {"config": {"DB_POOL_SIZE": "10"}})
    monkeypatch.setattr(reset_all, "_checkout_version", lambda: deploy_bad_v18.GOOD_VERSION)

    reset_all.main()

    assert "http://localhost:8004/chaos/resume" in posted


def test_a_clean_reset_returns_zero(monkeypatch):
    _stubbed_reset(monkeypatch)

    assert reset_all.main() == 0


def test_an_unreachable_service_makes_the_reset_fail(monkeypatch, capsys):
    _stubbed_reset(monkeypatch)

    def refuse(url, **k):
        raise reset_all.requests.ConnectionError("refused")

    monkeypatch.setattr(reset_all.requests, "post", refuse)

    assert reset_all.main() == 1
    assert "FAILED" in capsys.readouterr().out


def test_an_error_status_from_a_service_makes_the_reset_fail(monkeypatch):
    _stubbed_reset(monkeypatch)

    class _Broken(_Response):
        status_code = 500
        text = "boom"

    monkeypatch.setattr(reset_all.requests, "post", lambda *a, **k: _Broken())

    assert reset_all.main() == 1


def test_a_failed_auth_pool_reset_makes_the_reset_fail(monkeypatch):
    _stubbed_reset(monkeypatch)

    def fail():
        raise reset_all.DeploymentError("no container")

    monkeypatch.setattr(reset_all.config_pool, "reset", fail)

    assert reset_all.main() == 1


def test_a_failed_checkout_redeploy_makes_the_reset_fail(monkeypatch):
    _stub_everything_else(monkeypatch)
    monkeypatch.setattr(reset_all, "_checkout_version", lambda: deploy_bad_v18.BAD_VERSION)

    def fail():
        raise reset_all.DeploymentError("image missing")

    monkeypatch.setattr(reset_all.deploy_bad_v18, "reset", fail)

    assert reset_all.main() == 1


def test_a_failed_unpause_makes_the_reset_fail_but_the_rest_still_runs(monkeypatch):
    _stubbed_reset(monkeypatch)
    monkeypatch.setattr(reset_all, "_unpause_lab_containers", lambda: False)
    posted = []
    monkeypatch.setattr(reset_all.requests, "post", lambda url, **k: posted.append(url) or _Response())

    assert reset_all.main() == 1
    assert posted


def test_running_the_module_exits_with_the_reset_result():
    import pathlib

    source = pathlib.Path(reset_all.__file__).read_text(encoding="utf-8")

    assert "sys.exit(main())" in source
