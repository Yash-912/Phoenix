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
