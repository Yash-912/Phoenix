"""reset_all is the one-command return to healthy, so it has to undo every scenario.

checkout-service's fault lives in the image, not in a flag an endpoint can clear,
so reset_all redeploys the good version when the running one is not it -- and
leaves a healthy checkout alone, since each redeploy recreates the container and
writes a deployment marker."""

from chaos import deploy_bad_v18, reset_all


def _stub_everything_else(monkeypatch):
    monkeypatch.setattr(reset_all.requests, "post", lambda *a, **k: type("R", (), {"status_code": 200, "text": "ok"})())
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
