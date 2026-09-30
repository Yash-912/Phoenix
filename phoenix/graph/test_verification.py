from phoenix.graph import verification

SERVICE = "checkout-service"

BEFORE_BYTES = 900_000_000
MEMORY_PROMQL = "container_memory_working_set_bytes"
ACTION_AT = "2026-09-30T10:00:00Z"
STARTED_BEFORE = "2026-09-30T09:00:00Z"
STARTED_AFTER = "2026-09-30T10:00:30Z"


def _series(value: str, name: str = MEMORY_PROMQL) -> dict:
    """The shape query_prometheus actually returns: a raw instant vector whose
    sample value is a string, because that is what Prometheus's JSON encodes."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {"__name__": name, "name": SERVICE}, "value": [1759000000.123, value]}],
        },
    }


def _no_data() -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


def _prom(after_bytes: int, slope: float = 0.0):
    """A Prometheus answering the level query with a reading and the deriv query
    with a slope, which is the two-query shape read_signal makes."""

    def fake(promql: str) -> dict:
        return _series(str(slope)) if "deriv" in promql else _series(str(after_bytes))

    return fake


def _container(status: str, started_at: str) -> dict:
    return {
        "Name": f"/{SERVICE}",
        "State": {"Status": status, "Running": status == "running", "StartedAt": started_at},
    }


def _healthy() -> dict:
    return {"container": {"status": "running"}, "app": {"status": "ok", "http_status": 200}}


def _check(monkeypatch, outcome: str, detail: dict, category="overload", before=None):
    monkeypatch.setattr(verification, "run_check", lambda *a: (outcome, detail))
    return verification.run_check(category, SERVICE, before, ACTION_AT)


# --- the signal that could not be read is never a pass -------------------------


def test_a_signal_that_cannot_be_read_is_never_a_pass(monkeypatch):
    monkeypatch.setattr(
        verification,
        "query_prometheus",
        lambda promql: {"status": "error", "error": "Connection refused"},
    )

    outcome, detail = verification.run_check(
        "overload", SERVICE, {"bytes": BEFORE_BYTES, "slope": 0.0}, ACTION_AT
    )

    assert outcome == "inconclusive"
    assert detail["reason"] != ""


def test_a_prometheus_that_answers_with_no_data_is_inconclusive_not_a_pass(monkeypatch):
    monkeypatch.setattr(verification, "query_prometheus", lambda promql: _no_data())

    outcome, _ = verification.run_check(
        "overload", SERVICE, {"bytes": BEFORE_BYTES, "slope": 0.0}, ACTION_AT
    )

    assert outcome == "inconclusive"


def test_a_container_whose_state_cannot_be_read_is_inconclusive(monkeypatch):
    monkeypatch.setattr(
        verification,
        "get_container_state",
        lambda name: {"status": "error", "error": "404 Client Error"},
    )
    monkeypatch.setattr(verification, "inspect_health", lambda name: _healthy())

    outcome, _ = verification.run_check("crash", SERVICE, {}, ACTION_AT)

    assert outcome == "inconclusive"


def test_overload_without_a_pre_action_snapshot_is_inconclusive(monkeypatch):
    monkeypatch.setattr(verification, "query_prometheus", _prom(400_000_000))

    outcome, _ = verification.run_check("overload", SERVICE, {}, ACTION_AT)

    assert outcome == "inconclusive"


# --- overload: absolute drop, and a slope that is not climbing ----------------


def test_memory_that_dropped_below_half_the_pre_action_reading_passes(monkeypatch):
    monkeypatch.setattr(verification, "query_prometheus", _prom(400_000_000, slope=0.0))

    outcome, detail = verification.run_check(
        "overload", SERVICE, {"bytes": BEFORE_BYTES, "slope": 0.0}, ACTION_AT
    )

    assert outcome == "pass"
    assert detail["before"] == BEFORE_BYTES
    assert detail["after"] == 400_000_000


def test_memory_that_did_not_drop_fails(monkeypatch):
    monkeypatch.setattr(verification, "query_prometheus", _prom(880_000_000, slope=0.0))

    outcome, _ = verification.run_check(
        "overload", SERVICE, {"bytes": BEFORE_BYTES, "slope": 0.0}, ACTION_AT
    )

    assert outcome == "fail"


def test_memory_that_dropped_and_then_started_climbing_again_fails(monkeypatch):
    monkeypatch.setattr(verification, "query_prometheus", _prom(400_000_000, slope=5000.0))

    outcome, detail = verification.run_check(
        "overload", SERVICE, {"bytes": BEFORE_BYTES, "slope": 0.0}, ACTION_AT
    )

    assert outcome == "fail"
    assert "slope" in detail["reason"]


# --- crash: this action restarted it, and it is healthy now -------------------


def test_a_container_that_was_already_running_before_the_action_does_not_verify(monkeypatch):
    monkeypatch.setattr(verification, "get_container_state", lambda name: _container("running", STARTED_BEFORE))
    monkeypatch.setattr(verification, "inspect_health", lambda name: _healthy())

    outcome, detail = verification.run_check("crash", SERVICE, {}, ACTION_AT)

    assert outcome == "fail"
    assert "started" in detail["reason"]


def test_a_container_started_after_the_action_which_is_now_healthy_passes(monkeypatch):
    monkeypatch.setattr(verification, "get_container_state", lambda name: _container("running", STARTED_AFTER))
    monkeypatch.setattr(verification, "inspect_health", lambda name: _healthy())

    outcome, detail = verification.run_check("crash", SERVICE, {}, ACTION_AT)

    assert outcome == "pass"
    assert detail["started_at"] == STARTED_AFTER


def test_a_healthy_container_whose_app_health_probe_fails_does_not_verify(monkeypatch):
    monkeypatch.setattr(verification, "get_container_state", lambda name: _container("running", STARTED_AFTER))
    monkeypatch.setattr(
        verification,
        "inspect_health",
        lambda name: {"container": {"status": "running"}, "app": {"status": "error", "error": "503"}},
    )

    outcome, detail = verification.run_check("crash", SERVICE, {}, ACTION_AT)

    assert outcome == "fail"


def test_a_container_that_is_not_running_does_not_verify(monkeypatch):
    monkeypatch.setattr(verification, "get_container_state", lambda name: _container("exited", STARTED_AFTER))
    monkeypatch.setattr(verification, "inspect_health", lambda name: _healthy())

    outcome, _ = verification.run_check("crash", SERVICE, {}, ACTION_AT)

    assert outcome == "fail"
