from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from phoenix.graph import verification

SERVICE = "checkout-service"

BEFORE_BYTES = 900_000_000
MEMORY_PROMQL = "process_resident_memory_bytes"
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
            "result": [{"metric": {"__name__": name, "job": SERVICE}, "value": [1759000000.123, value]}],
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


def _prom_bytes(level: float, slope: float = 0.0):
    """The same shape, with the level left as whatever float it was given so a
    test can put a non-finite value in the place Prometheus would."""

    def fake(promql: str) -> dict:
        return _series(str(slope)) if "deriv" in promql else _series(str(level))

    return fake


def _container(status: str, started_at: str) -> dict:
    return {
        "Name": f"/{SERVICE}",
        "State": {"Status": status, "Running": status == "running", "StartedAt": started_at},
    }


def _healthy() -> dict:
    return {"container": {"status": "running"}, "app": {"status": "ok", "http_status": 200}}


# --- readings that cannot be trusted are not readings -------------------------
#
# A restart empties a container's time series, and Prometheus answers a query
# over an empty or too-short range with NaN rather than with nothing. So NaN is
# not a hypothetical here: it is what this module sees on the pass path it most
# wants to take. Every test below builds the exact payload a real Prometheus
# would return, because the shape of the failure is that the value parses
# cleanly as a float and then behaves unlike one.


def test_a_nan_slope_is_not_read_as_memory_holding_steady(monkeypatch):
    """deriv() over fewer than two points returns NaN, which is the normal state
    in the window right after a restart. `nan > 1024` is False, so a slope that
    could not be computed would otherwise satisfy "is not climbing" and pass."""
    monkeypatch.setattr(verification, "query_prometheus", _prom(400_000_000, slope=float("nan")))

    outcome, detail = verification.run_check(
        "overload", SERVICE, {"bytes": BEFORE_BYTES, "slope": 0.0}, ACTION_AT
    )

    assert outcome == "inconclusive"
    assert "slope" in detail["reason"]


def test_a_nan_working_set_is_not_a_reading(monkeypatch):
    monkeypatch.setattr(
        verification, "query_prometheus", _prom_bytes(float("nan"), slope=0.0)
    )

    outcome, detail = verification.run_check(
        "overload", SERVICE, {"bytes": BEFORE_BYTES, "slope": 0.0}, ACTION_AT
    )

    assert outcome == "inconclusive"
    assert detail["reason"]


def test_a_nan_working_set_never_raises_out_of_the_check(monkeypatch):
    """int(nan) raises ValueError. A restart has already happened by the time
    this runs, so an exception here unwinds past the print of the final state
    and the operator gets a traceback instead of the diagnosis."""
    monkeypatch.setattr(
        verification, "query_prometheus", _prom_bytes(float("nan"), slope=0.0)
    )

    outcome, _ = verification.run_check(
        "overload", SERVICE, {"bytes": BEFORE_BYTES, "slope": 0.0}, ACTION_AT
    )

    assert outcome in {"inconclusive", "fail", "pass"}


def test_an_ambiguous_series_is_inconclusive_rather_than_graded_on_an_arbitrary_one(
    monkeypatch,
):
    """result[0] of a multi-series match is whichever the engine ordered first.
    Reporting a confident verdict about that series is worse than reporting
    nothing, so an ambiguous match is refused."""

    def two_series(promql: str) -> dict:
        payload = _series("0.0") if "deriv" in promql else _series("400000000.0")
        payload["data"]["result"].append(
            {"metric": {"__name__": MEMORY_PROMQL, "name": SERVICE, "job": "other"}, "value": [1, "999999999"]}
        )
        return payload

    monkeypatch.setattr(verification, "query_prometheus", two_series)

    outcome, _ = verification.run_check(
        "overload", SERVICE, {"bytes": BEFORE_BYTES, "slope": 0.0}, ACTION_AT
    )

    assert outcome == "inconclusive"


def test_the_working_set_queries_are_scoped_to_the_job_the_alert_rule_uses():
    """An unscoped selector can match the same metric from another job, and then
    the check is reading an arbitrary series.

    Scoped to job="<service>" rather than job="cadvisor": prometheus.yml names
    each scrape job after its target, so this is the label that both identifies
    the service and keeps the constraint that _single_sample relies on."""
    assert f'job="{SERVICE}"' in verification._memory_promql(SERVICE)
    assert f'job="{SERVICE}"' in verification._slope_promql(SERVICE)


def test_the_working_set_queries_do_not_select_cadvisor():
    """A regression guard, and the reason this file has one.

    The queries used to select cAdvisor's container_memory_working_set_bytes on a
    name label. Measured against a running Prometheus, that returns zero series on
    Docker Desktop/WSL2: cAdvisor exposes seven root-level cgroup series with no
    name label at all. Every overload check therefore graded inconclusive no
    matter what the container was doing, and the failure looked like a flaky
    query rather than a dead selector.

    Asserted as an absence, because a test that only pins the current string would
    still pass if someone reintroduced the dead selector alongside it. The check
    that matters is not "the query has the right shape" but "the query names a
    series that exists" -- and that can only be settled against a live Prometheus,
    which is why this guard exists rather than being the whole test."""
    for promql in (verification._memory_promql(SERVICE), verification._slope_promql(SERVICE)):
        assert "container_memory_working_set_bytes" not in promql
        assert "cadvisor" not in promql
        assert verification.MEMORY_METRIC in promql


def test_an_infinite_slope_is_climbing_rather_than_unmeasurable(monkeypatch):
    """Distinct from NaN: +Inf is a slope that really is unbounded, which is a
    leak, not a broken query."""
    monkeypatch.setattr(verification, "query_prometheus", _prom(400_000_000, slope=float("inf")))

    outcome, _ = verification.run_check(
        "overload", SERVICE, {"bytes": BEFORE_BYTES, "slope": 0.0}, ACTION_AT
    )

    assert outcome == "fail"


# --- timestamps are compared as instants, not as strings ---------------------


def test_a_container_that_started_before_the_action_does_not_verify_even_when_the_two_timestamps_look_later_in_the_string():
    """Docker writes StartedAt as RFC3339 with trailing zeros trimmed
    ("...:00.123Z"); the remediator writes action_at through isoformat(), which
    keeps microseconds and an offset ("...:00.123456+00:00"). Compared as
    strings, 'Z' (0x5A) sorts after '+' (0x2B), so a container that started
    456 microseconds BEFORE the action reads as having started after it.

    The window is sub-second, so this rarely fires on the success path -- a
    restart lands well after action_at. It fires precisely on the claim this
    check exists to make: proving the action is what brought the container up.
    """
    # Fixed, not now(): the wall clock's microseconds would decide whether this
    # test exercises the bug, so it would pass or fail by luck of the second.
    action_instant = datetime(2026, 9, 30, 10, 0, 0, 123456, tzinfo=timezone.utc)
    action_at = action_instant.isoformat()
    started_at = "2026-09-30T10:00:00.123Z"
    assert (
        datetime.fromisoformat(started_at.replace("Z", "+00:00")) < action_instant
    ), "fixture must be genuinely earlier"

    with patch.object(verification, "get_container_state", return_value=_container("running", started_at)), \
         patch.object(verification, "inspect_health", return_value=_healthy()):
        outcome, detail = verification.run_check("crash", SERVICE, {}, action_at)

    assert outcome == "fail"


def test_a_container_started_after_the_action_verifies_when_the_two_formats_differ():
    """The same comparison, the other way: a real restart must still pass with
    action_at carrying an offset and StartedAt a Z."""
    action_at = datetime.now(timezone.utc).isoformat()
    started_at = (datetime.fromisoformat(action_at) + timedelta(seconds=30)).isoformat().replace(
        "+00:00", "Z"
    )

    with patch.object(verification, "get_container_state", return_value=_container("running", started_at)), \
         patch.object(verification, "inspect_health", return_value=_healthy()):
        outcome, _ = verification.run_check("crash", SERVICE, {}, action_at)

    assert outcome == "pass"


def test_an_unreadable_action_timestamp_is_inconclusive_rather_than_skipped(monkeypatch):
    """The guard is written `if action_at and started_at < action_at`, so a
    planned_action missing its timestamp falls straight through to the pass
    branch. Absence of a comparison is not a comparison that succeeded."""
    monkeypatch.setattr(verification, "get_container_state", lambda n: _container("running", STARTED_AFTER))
    monkeypatch.setattr(verification, "inspect_health", lambda n: _healthy())

    outcome, _ = verification.run_check("crash", SERVICE, {}, None)

    assert outcome == "inconclusive"


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
        lambda name: {
            "container": {"status": "running"},
            "app": {"status": "unhealthy", "http_status": 503},
        },
    )

    outcome, detail = verification.run_check("crash", SERVICE, {}, ACTION_AT)

    assert outcome == "fail"


def test_an_unreachable_health_probe_is_inconclusive_not_a_failed_check(monkeypatch):
    """inspect_health writes {"status": "error"} when the request raises, which
    is what a timeout looks like. Grading that as an unhealthy service would
    fail a restart that may well have worked, loop back to the observer, and
    spend another attempt on a check that never ran."""
    monkeypatch.setattr(verification, "get_container_state", lambda name: _container("running", STARTED_AFTER))
    monkeypatch.setattr(
        verification,
        "inspect_health",
        lambda name: {
            "container": {"status": "running"},
            "app": {"status": "error", "error": "HTTPConnectionPool: Read timed out"},
        },
    )

    outcome, detail = verification.run_check("crash", SERVICE, {}, ACTION_AT)

    assert outcome == "inconclusive"
    assert "timed out" in detail["reason"] or "reached" in detail["reason"]


def test_a_container_that_is_not_running_does_not_verify(monkeypatch):
    monkeypatch.setattr(verification, "get_container_state", lambda name: _container("exited", STARTED_AFTER))
    monkeypatch.setattr(verification, "inspect_health", lambda name: _healthy())

    outcome, _ = verification.run_check("crash", SERVICE, {}, ACTION_AT)

    assert outcome == "fail"


# --- Phase 4: deploy (Tier 2) verification ---------------------------------
#
# A deploy is verified by two independent facts: which artifact the container
# says it is, and whether the business endpoint works. /health is green in both
# artifacts, so none of these tests consult it -- that is the trap this category
# exists to avoid.


def _deploy_container(version: str | None = "v17", digest: str = "sha256:abc") -> dict:
    labels = {"app.version": version} if version else {}
    return {
        "Image": digest,
        "Config": {"Labels": labels},
        "State": {"Status": "running"},
    }


def _endpoint(status_code: int = 200, body: str = '{"order_id":"demo-order-1"}') -> dict:
    return {"status": "ok", "url": "http://localhost:8001/checkout", "http_status": status_code,
            "body": body, "serving": status_code < 500}


def _deploy_probes(monkeypatch, version="v17", endpoint=None, digest="sha256:abc") -> None:
    monkeypatch.setattr(verification, "get_container_state", lambda name: _deploy_container(version, digest))
    monkeypatch.setattr(
        verification, "_probe_business_endpoint",
        lambda name: endpoint if endpoint is not None else _endpoint(),
    )


def test_a_rollback_verifies_when_the_artifact_is_replaced_and_the_endpoint_serves(monkeypatch):
    _deploy_probes(monkeypatch)

    outcome, detail = verification.run_check("deploy", SERVICE, {}, ACTION_AT, "v17")

    assert outcome == "pass"
    assert detail["running_version"] == "v17"
    assert detail["image_digest"] == "sha256:abc"


def test_a_container_still_running_the_bad_release_does_not_verify(monkeypatch):
    """The failure a fake verification would miss: identity unchanged."""
    _deploy_probes(monkeypatch, version="v18")

    outcome, detail = verification.run_check("deploy", SERVICE, {}, ACTION_AT, "v17")

    assert outcome == "fail"
    assert "v18" in detail["reason"]
    assert "not replaced" in detail["reason"]


def test_the_right_artifact_that_still_rejects_traffic_does_not_verify(monkeypatch):
    """Identity alone is not recovery: v17 could be up but wedged."""
    _deploy_probes(monkeypatch, version="v17", endpoint=_endpoint(500, "checkout unavailable (v17)"))

    outcome, detail = verification.run_check("deploy", SERVICE, {}, ACTION_AT, "v17")

    assert outcome == "fail"
    assert "still rejects" in detail["reason"]


def test_a_container_with_no_version_label_is_unreadable_rather_than_a_pass(monkeypatch):
    """No identity means no basis for calling it recovered."""
    _deploy_probes(monkeypatch, version=None)

    outcome, detail = verification.run_check("deploy", SERVICE, {}, ACTION_AT, "v17")

    assert outcome == "inconclusive"
    assert "app.version" in detail["reason"]


def test_a_deploy_check_with_no_recorded_target_cannot_verify(monkeypatch):
    _deploy_probes(monkeypatch)

    outcome, detail = verification.run_check("deploy", SERVICE, {}, ACTION_AT, None)

    assert outcome == "inconclusive"
    assert "no target version" in detail["reason"]


def test_an_unreachable_container_is_inconclusive_not_a_failure(monkeypatch):
    monkeypatch.setattr(
        verification, "get_container_state",
        lambda name: {"status": "error", "error": "connection refused"},
    )

    outcome, _ = verification.run_check("deploy", SERVICE, {}, ACTION_AT, "v17")

    assert outcome == "inconclusive"


def test_the_deploy_check_never_reads_the_health_endpoint(monkeypatch):
    """/health is green in v18, so consulting it would pass a broken release."""

    def forbidden(name):
        raise AssertionError("a deploy check must not trust /health")

    monkeypatch.setattr(verification, "inspect_health", forbidden)
    _deploy_probes(monkeypatch, version="v18")

    outcome, _ = verification.run_check("deploy", SERVICE, {}, ACTION_AT, "v17")

    assert outcome == "fail"


def test_a_500_is_recorded_as_evidence_rather_than_raised(monkeypatch):
    """A rejection is the thing being verified, so it must survive as a reading."""
    seen = []

    class Response:
        status_code = 500
        text = "checkout unavailable (v18)"

    monkeypatch.setattr(verification.time, "sleep", lambda s: None)

    def fake_get(url, timeout=None):
        seen.append(url)
        return Response()

    monkeypatch.setattr(verification.requests, "get", fake_get)

    result = verification._probe_business_endpoint(SERVICE)

    assert result["http_status"] == 500
    assert result["serving"] is False
    assert "v18" in result["body"]
    assert seen == ["http://localhost:8001/checkout"]


def test_an_unreachable_endpoint_is_retried_then_reported_unreadable(monkeypatch):
    attempts = []
    monkeypatch.setattr(verification.time, "sleep", lambda s: attempts.append(s))

    def refuse(url, timeout=None):
        raise verification.requests.RequestException("connection refused")

    monkeypatch.setattr(verification.requests, "get", refuse)

    result = verification._probe_business_endpoint(SERVICE)

    assert result["status"] == "error"
    assert "connection refused" in result["error"]
    # Bounded: an endpoint that never answers is a failure to read, not a reason
    # to hold the run open indefinitely.
    assert len(attempts) == verification.DEPLOY_SETTLE_ATTEMPTS


def test_a_service_with_no_known_business_endpoint_is_refused_not_guessed(monkeypatch):
    """Guessing a port could read some other service's answer as this one's."""
    monkeypatch.setattr(
        verification.requests, "get",
        lambda *a, **k: pytest.fail("must not probe an endpoint it does not know"),
    )

    result = verification._probe_business_endpoint("mystery-service")

    assert result["status"] == "error"
    assert "no business endpoint" in result["error"]


# --- config: a rollback_config result is verified by the service's own report


AUTH_SERVICE = "auth-service"


def _auth_health(pool_size: str | None = "10", status: str = "ok") -> dict:
    app = {"status": status}
    if pool_size is not None:
        app["db_pool_size"] = pool_size
    return {"container": {"Status": "running"}, "app": app}


def test_a_config_rollback_verifies_when_the_service_reports_the_restored_value(monkeypatch):
    monkeypatch.setattr(verification, "inspect_health", lambda name: _auth_health("10"))

    outcome, detail = verification.run_check("config", AUTH_SERVICE, {}, ACTION_AT, "10")

    assert outcome == "pass"
    assert detail["key"] == "DB_POOL_SIZE"
    assert detail["reported_value"] == "10"


def test_a_service_still_reporting_the_regressed_value_does_not_verify(monkeypatch):
    monkeypatch.setattr(verification, "inspect_health", lambda name: _auth_health("1"))

    outcome, detail = verification.run_check("config", AUTH_SERVICE, {}, ACTION_AT, "10")

    assert outcome == "fail"
    assert "1" in detail["reason"]
    assert "not restored" in detail["reason"]


def test_a_config_check_with_no_recorded_target_cannot_verify(monkeypatch):
    monkeypatch.setattr(verification, "inspect_health", lambda name: _auth_health("10"))

    outcome, detail = verification.run_check("config", AUTH_SERVICE, {}, ACTION_AT, None)

    assert outcome == "inconclusive"
    assert "no target value" in detail["reason"]


def test_an_unreachable_health_probe_is_inconclusive_for_a_config_check(monkeypatch):
    monkeypatch.setattr(verification, "inspect_health", lambda name: _auth_health(status="error"))

    outcome, _ = verification.run_check("config", AUTH_SERVICE, {}, ACTION_AT, "10")

    assert outcome == "inconclusive"


def test_a_health_response_missing_the_pool_field_is_inconclusive_not_a_pass(monkeypatch):
    monkeypatch.setattr(verification, "inspect_health", lambda name: _auth_health(pool_size=None))

    outcome, _ = verification.run_check("config", AUTH_SERVICE, {}, ACTION_AT, "10")

    assert outcome == "inconclusive"


def test_a_service_with_no_known_config_signal_is_refused_not_guessed(monkeypatch):
    monkeypatch.setattr(
        verification, "inspect_health",
        lambda name: pytest.fail("must not probe a service it has no config signal for"),
    )

    outcome, detail = verification.run_check("config", "mystery-service", {}, ACTION_AT, "10")

    assert outcome == "inconclusive"
    assert "no config signal" in detail["reason"]
