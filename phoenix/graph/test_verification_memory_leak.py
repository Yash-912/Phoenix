"""memory_leak is verified by the same working-set check as overload."""

from phoenix.graph import verification


def test_memory_leak_reads_the_memory_signal(monkeypatch):
    monkeypatch.setattr(verification, "_read_memory", lambda service: {"bytes": 1.0, "slope": 0.0})

    assert verification.read_signal("memory_leak", "worker-service") == {"bytes": 1.0, "slope": 0.0}


def test_memory_leak_passes_when_the_working_set_dropped_and_is_flat(monkeypatch):
    monkeypatch.setattr(verification, "_read_memory", lambda service: {"bytes": 100.0, "slope": 0.0})

    outcome, _detail = verification.run_check("memory_leak", "worker-service", {"bytes": 1000.0}, None)

    assert outcome == verification.OUTCOME_PASS


def test_memory_leak_fails_when_the_working_set_did_not_drop(monkeypatch):
    monkeypatch.setattr(verification, "_read_memory", lambda service: {"bytes": 900.0, "slope": 0.0})

    outcome, _detail = verification.run_check("memory_leak", "worker-service", {"bytes": 1000.0}, None)

    assert outcome == verification.OUTCOME_FAIL


def test_a_category_with_no_check_is_still_inconclusive_never_a_pass():
    outcome, _detail = verification.run_check("slow_query", "payment-service", {}, None)

    assert outcome == verification.OUTCOME_INCONCLUSIVE


def test_the_slope_window_never_reaches_back_past_the_action(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(verification.time, "sleep", lambda s: None)
    action_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()

    window = verification._post_action_window(action_at)

    assert 100 <= window <= 118


def test_the_slope_window_is_capped_at_the_default(monkeypatch):
    from datetime import datetime, timedelta, timezone

    action_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()

    assert verification._post_action_window(action_at) == verification.SLOPE_WINDOW_MINUTES * 60


def test_a_check_that_runs_too_soon_waits_for_two_scrapes(monkeypatch):
    from datetime import datetime, timezone

    slept = []
    monkeypatch.setattr(verification.time, "sleep", lambda s: slept.append(s))

    window = verification._post_action_window(datetime.now(timezone.utc).isoformat())

    assert slept and slept[0] > 0
    assert window == verification.MIN_POST_ACTION_SECONDS - verification.SLOPE_WINDOW_MARGIN_SECONDS


def test_no_action_time_falls_back_to_the_default_window():
    assert verification._post_action_window(None) is None


def test_the_promql_uses_the_post_action_window_when_given():
    assert "[40s]" in verification._slope_promql("worker-service", 40)
    assert "[5m]" in verification._slope_promql("worker-service")


def test_allocator_noise_over_a_post_action_window_is_not_a_leak():
    signal = {"bytes": 52_000_000.0, "slope": 8000.0, "window_seconds": 85}

    outcome, _detail = verification._check_overload(signal, {"bytes": 138_000_000.0})

    assert outcome == verification.OUTCOME_PASS


def test_a_real_leak_rate_over_the_same_window_still_fails():
    signal = {"bytes": 52_000_000.0, "slope": 40_000.0, "window_seconds": 85}

    outcome, detail = verification._check_overload(signal, {"bytes": 138_000_000.0})

    assert outcome == verification.OUTCOME_FAIL
    assert "restart bought time" in detail["reason"]


def test_without_a_window_the_paging_threshold_still_applies():
    signal = {"bytes": 52_000_000.0, "slope": 8000.0}

    outcome, _detail = verification._check_overload(signal, {"bytes": 138_000_000.0})

    assert outcome == verification.OUTCOME_FAIL


# ---- a good restart lands at the fresh-process level, not below half of before ----

MIB = 1024 * 1024
FRESH = 52_000_000.0


def test_a_restart_to_the_fresh_process_level_passes_when_before_was_only_moderately_elevated():
    # 52 MB is 56-63% of 82-93 MB, so the old strict-half rule graded this a fail.
    for before in (82_000_000.0, 88_000_000.0, 93_000_000.0):
        outcome, detail = verification._check_overload(
            {"bytes": FRESH, "slope": 0.0, "window_seconds": 90}, {"bytes": before}
        )

        assert outcome == verification.OUTCOME_PASS, (before, detail["reason"])


def test_a_restart_that_released_nothing_still_fails_even_at_a_low_level():
    outcome, _detail = verification._check_overload(
        {"bytes": FRESH, "slope": 0.0, "window_seconds": 90}, {"bytes": 55_000_000.0}
    )

    assert outcome == verification.OUTCOME_FAIL


def test_a_small_drop_that_is_not_the_fresh_level_still_fails():
    outcome, _detail = verification._check_overload(
        {"bytes": 90_000_000.0, "slope": 0.0, "window_seconds": 90}, {"bytes": 100_000_000.0}
    )

    assert outcome == verification.OUTCOME_FAIL


def test_the_fresh_level_rule_still_requires_the_slope_to_be_flat():
    outcome, detail = verification._check_overload(
        {"bytes": FRESH, "slope": 40_000.0, "window_seconds": 90}, {"bytes": 90_000_000.0}
    )

    assert outcome == verification.OUTCOME_FAIL
    assert "restart bought time" in detail["reason"]


def test_a_service_whose_fresh_level_is_above_the_ceiling_keeps_the_strict_half_rule():
    big = {"bytes": 300 * MIB, "slope": 0.0, "window_seconds": 90}

    assert verification._check_overload(big, {"bytes": 700 * MIB})[0] == verification.OUTCOME_PASS
    assert verification._check_overload(big, {"bytes": 400 * MIB})[0] == verification.OUTCOME_FAIL


def test_the_ceiling_can_be_set_by_env(monkeypatch):
    signal = {"bytes": 70 * MIB, "slope": 0.0, "window_seconds": 90}
    before = {"bytes": 100 * MIB}

    assert verification._check_overload(signal, before)[0] == verification.OUTCOME_FAIL

    monkeypatch.setenv(verification.MEMORY_CEILING_ENV, str(80 * MIB))

    assert verification._check_overload(signal, before)[0] == verification.OUTCOME_PASS


def test_an_unreadable_ceiling_env_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(verification.MEMORY_CEILING_ENV, "not-a-number")

    assert verification.memory_ceiling_bytes() == verification.DEFAULT_MEMORY_CEILING_BYTES

    monkeypatch.setenv(verification.MEMORY_CEILING_ENV, "-5")

    assert verification.memory_ceiling_bytes() == verification.DEFAULT_MEMORY_CEILING_BYTES
