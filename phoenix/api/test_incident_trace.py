"""The trace the dashboard shows, built from the fixture incidents.

Fixtures are shaped from the Gate 0 dumps, not exported from a database
(fixtures/README.md), so these tests pin what the assembly does with each shape of
incident, not what the live system measured.
"""

from datetime import datetime

import fixture_store
import incident_trace
from incident_trace import audit_rows, build_trace, derive_outcome, overview_row

NOW = datetime.fromisoformat(fixture_store.FIXTURE_NOW)
FIXTURES = fixture_store.load()


def trace(incident_id):
    return build_trace(FIXTURES[incident_id], now=NOW)


def kinds(t):
    return [step["kind"] for step in t["timeline"]]


# --- outcome ------------------------------------------------------------------------


def test_the_graphs_own_final_status_wins_over_anything_derived():
    assert derive_outcome("resolved", llm_unavailable=True, pending_approvals=1) == "resolved"


def test_a_run_with_no_memory_row_is_read_from_the_audit_trail():
    assert derive_outcome(None, investigation_failed=True, llm_unavailable=True) == "investigation_failed"
    assert derive_outcome(None, pending_approvals=1, audit_events=4) == "awaiting_approval"
    assert derive_outcome(None, llm_unavailable=True, audit_events=3) == "llm_unavailable"
    assert derive_outcome(None, audit_events=3) == "investigating"
    assert derive_outcome(None) == "not_started"


def test_every_fixture_incident_gets_the_outcome_it_is_named_for():
    expected = {
        33: "resolved", 34: "pr_exists", 35: "pr_opened", 36: "insufficient_evidence", 37: "escalated",
        38: "llm_unavailable", 39: "awaiting_approval", 40: "escalated", 41: "action_unavailable", 42: "investigation_failed",
    }
    assert {i: trace(i)["incident"]["outcome"]["key"] for i in FIXTURES} == expected


def test_a_status_the_graph_adds_later_is_still_shown_in_its_own_words():
    entry = incident_trace.outcome_entry("some_new_status")
    assert entry["label"] == "Some new status"


def test_the_list_row_keeps_alert_status_and_outcome_separate():
    row = overview_row(fixture_store.overview_rows(FIXTURES)[0])
    assert {"alert_status", "outcome"} <= row.keys()
    assert row["outcome"]["key"] in incident_trace.OUTCOMES


def test_every_outcome_the_fixes_added_has_a_label():
    for key in ("llm_unavailable", "insufficient_evidence", "escalated", "action_unavailable", "pr_opened", "pr_exists"):
        assert incident_trace.OUTCOMES[key]["label"]


# --- the rollback incident, narrated from the trace alone -------------------------------


def test_the_rollback_incident_reads_start_to_finish_in_the_narrative():
    text = " ".join(trace(33)["narrative"])
    assert "HighErrorRate on checkout-service" in text
    assert "v18" in text
    assert "tester approved rollback_deployment on checkout-service via api" in text
    assert "Verification passed" in text


def test_the_leader_and_what_it_beat_come_with_scores():
    hypotheses = trace(33)["hypotheses"]
    assert hypotheses["leader"]["category"] == "deploy"
    assert hypotheses["leader"]["score"] == 0.80
    assert hypotheses["confidence"] == 0.80 and hypotheses["confidence_threshold"] == 0.75
    [loser] = hypotheses["discarded"]
    assert loser["category"] == "config"
    assert loser["behind_leader_by"] == 0.80
    assert [p["iteration"] for p in hypotheses["passes"]] == [1, 2]


def test_the_tier_and_why_are_the_agents_own_recorded_reason():
    tier = trace(33)["tier"]
    assert tier["tier"] == 2
    assert tier["label"] == "Tier 2"
    assert tier["policy_mode"] == "guarded"
    assert tier["gate"] == "held for approval"
    assert "maps to rollback_deployment" in tier["why"]


def test_approval_is_recorded_with_who_how_and_when():
    [approval] = trace(33)["approvals"]
    assert (approval["status"], approval["decided_by"], approval["decision_channel"]) == ("approved", "tester", "api")
    assert approval["can_decide"] is False


def test_the_action_has_its_own_start_finish_and_reason():
    [action] = trace(33)["actions"]
    assert action["action"] == "rollback_deployment" and action["ok"] is True
    assert "newest non-regression artifact" in action["why"]


def test_verification_carries_the_outcome_and_the_sentence():
    verification = trace(33)["verification"]
    assert verification["outcome"] == "pass"
    assert "genuinely reverted" in verification["reason"]


def test_a_prior_incident_match_is_surfaced_as_context():
    [match] = trace(33)["memory"]["matches"]
    assert match["incident_id"] == 23 and match["status"] == "resolved"
    assert "context only" in match["text"]


def test_decisions_come_first_and_minor_steps_are_marked_not_dropped():
    steps = trace(33)["timeline"]
    assert [s["kind"] for s in steps if not s["minor"]][0] == "diagnose"
    assert any(s["minor"] and s["event_type"] == "observer_pass" for s in steps)
    assert [s["id"] for s in steps] == sorted(s["id"] for s in steps)


# --- approvals ------------------------------------------------------------------------------


def test_an_open_request_can_be_decided_and_the_narrative_says_it_is_waiting():
    t = trace(39)
    [approval] = t["approvals"]
    assert approval["status"] == "pending" and approval["can_decide"] is True
    assert "Waiting for an approver" in " ".join(t["narrative"])


def test_a_pending_request_past_its_deadline_cannot_be_decided():
    later = datetime.fromisoformat("2026-10-31T09:00:00+00:00")
    [approval] = build_trace(FIXTURES[39], now=later)["approvals"]
    assert approval["can_decide"] is False and approval["overdue"] is True


def test_a_denied_request_names_who_denied_it_and_the_run_escalated():
    t = trace(37)
    assert "tester denied rollback_config" in " ".join(t["narrative"])
    assert t["stop_reason"] == "tester denied rollback_config on auth-service"
    assert t["incident"]["outcome"]["key"] == "escalated"


def test_an_expired_request_is_not_shown_as_an_approval():
    t = trace(40)
    [approval] = t["approvals"]
    assert approval["status"] == "expired" and approval["can_decide"] is False
    assert "expired and was not approved" in " ".join(t["narrative"])
    assert "approval" in kinds(t)


# --- outcomes the fixes added -----------------------------------------------------------------


def test_insufficient_evidence_carries_the_report_and_the_failed_read():
    t = trace(36)
    assert t["evidence_report"]["failed_reads"][0]["source"] == "query_prometheus"
    assert any(p["kind"] == "failed_read" and "query_prometheus" in p["text"] for p in t["problems"])
    assert any(item["failed"] for item in t["evidence"])
    assert t["verification"] is None and t["actions"] == []


def test_a_model_outage_is_a_failure_step_not_a_finding():
    t = trace(38)
    assert t["incident"]["outcome"]["key"] == "llm_unavailable"
    assert t["hypotheses"]["leader"] is None
    assert any(p["kind"] == "llm_unavailable" for p in t["problems"])
    assert "Stopped because" in " ".join(t["narrative"])


def test_action_unavailable_is_a_stop_with_its_reason_and_no_action():
    t = trace(41)
    assert t["actions"] == [] and t["tier"]["tier"] is None
    assert "no allowed action maps to disk_pressure" in t["stop_reason"]


def test_investigation_failed_is_shown_with_the_error_and_the_attempt_count():
    t = trace(42)
    [step] = [s for s in t["timeline"] if s["event_type"] == "investigation_failed"]
    assert step["facts"]["attempts"] == 3 and step["kind"] == "failure"
    assert "connection refused" in t["stop_reason"]


# --- tier 3 -----------------------------------------------------------------------------------


def test_an_existing_pr_is_reported_as_exists_with_the_branch_cleanup():
    block = trace(34)["tier3"]
    assert block["pr"]["status"] == "exists" and block["pr"]["number"] == 38
    assert block["branch_cleanup"]["result"] == "deleted"
    assert block["defect"]["function_name"] == "_find_charge_slow"
    assert block["proposals"][0]["scope_ok"] is True
    assert block["validation"]["tests_passed"] is True


def test_an_opened_pr_keeps_its_branch_and_the_run_started_at_tier_1():
    t = trace(35)
    assert t["tier3"]["pr"] == {"status": "opened", "url": "https://github.com/example/lab/pull/44", "number": 44, "error": None}
    assert t["tier3"]["branch_cleanup"]["result"] == "kept"
    assert t["tier"]["tier"] == 3
    assert t["actions"][0]["action"] == "restart_service"
    assert "pull request" in " ".join(t["narrative"])


def test_the_new_audit_events_each_have_a_readable_title():
    seen = {step["event_type"]: step["title"] for i in FIXTURES for step in trace(i)["timeline"]}
    for event in ("branch_pushed", "remote_branch_deleted", "remote_branch_kept", "investigation_failed", "llm_unavailable", "action_unavailable"):
        assert event in seen and seen[event] != incident_trace._humanize(event)


def test_an_event_the_dashboard_does_not_know_still_appears():
    rows = {**FIXTURES[33], "audit": FIXTURES[33]["audit"] + [{"id": 9999, "node": "graph", "event_type": "brand_new_event", "created_at": None, "detail": {"x": 1}, "reasoning_text": "something new"}]}
    step = build_trace(rows, now=NOW)["timeline"][-1]
    assert (step["title"], step["kind"], step["text"]) == ("Brand new event", "other", "something new")


# --- llm prompts, redaction, the raw layer ----------------------------------------------------


def test_the_trace_says_prompts_are_not_recorded_rather_than_showing_nothing():
    llm = trace(33)["llm"]
    assert llm["recorded"] is False and "not implemented" in llm["note"]


def test_llm_events_are_picked_up_when_the_graph_starts_recording_them():
    row = {"id": 9000, "node": "diagnoser", "event_type": "llm_call", "created_at": None, "detail": {"prompt": "p", "response": "r"}, "reasoning_text": None}
    t = build_trace({**FIXTURES[33], "audit": FIXTURES[33]["audit"] + [row]}, now=NOW)
    assert t["llm"]["recorded"] is True and t["timeline"][-1]["kind"] == "llm"
    assert audit_rows(FIXTURES[33]["audit"] + [row])["rows"][-1]["kind"] == "llm"


def test_a_planted_secret_in_any_section_never_leaves_the_trace():
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    rows = {key: (value.copy() if isinstance(value, list) else value) for key, value in FIXTURES[33].items()}
    rows["evidence"] = [{**rows["evidence"][0], "summary": f"token {secret} in log", "raw_data": {"line": f"Authorization: Bearer {secret}", "password": "hunter2"}}]
    rows["audit"] = [{**rows["audit"][0], "reasoning_text": f"saw {secret}", "detail": {"api_key": secret}}] + rows["audit"][1:]
    rows["approvals"] = [{**rows["approvals"][0], "plan_reasoning": f"use {secret}"}]
    t = build_trace(rows, now=NOW)
    dumped = repr(t) + repr(audit_rows(rows["audit"]))
    assert secret not in dumped and "hunter2" not in dumped
    assert t["redaction"]["vendor_token"] >= 2


def test_large_raw_evidence_is_cut_and_marked_not_silently_dropped():
    rows = {**FIXTURES[33], "evidence": [{**FIXTURES[33]["evidence"][0], "raw_data": {"blob": "x" * 20000}}]}
    raw = build_trace(rows, now=NOW)["evidence"][0]["raw_data"]
    assert raw["truncated"] is True and raw["original_chars"] > 20000 and len(raw["preview"]) <= incident_trace.RAW_LIMIT


def test_the_raw_layer_keeps_every_row_in_order_with_its_detail():
    out = audit_rows(FIXTURES[33]["audit"])
    assert len(out["rows"]) == len(FIXTURES[33]["audit"])
    assert out["rows"][0]["detail"]["requested_tools"] == ["query_prometheus", "query_loki"]


def test_every_fixture_builds_and_is_json_serialisable():
    import json
    for incident_id in FIXTURES:
        json.dumps(trace(incident_id))
        json.dumps(audit_rows(FIXTURES[incident_id]["audit"]))
