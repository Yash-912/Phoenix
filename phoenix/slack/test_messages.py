"""The message a person reads before they approve: what it says and what it refuses to carry.

Evidence comes from logs and tool output, so every dynamic string is untrusted text on
its way into Slack. The tests plant hostile text and check it arrives inert.
"""

import json

from phoenix.slack import messages

DASHBOARD = "https://phoenix.example.com"

APPROVAL = {
    "id": 7,
    "incident_id": 12,
    "service_name": "checkout-service",
    "action": "rollback_deployment",
    "container": "checkout-service",
    "args": ["v17"],
    "tier": 2,
    "status": "pending",
    "incident_summary": "bad_deploy: checkout-service v18 raised the 5xx rate right after it shipped",
    "plan_reasoning": "ignored here",
    "evidence": [
        {"source": "prometheus", "iteration": 1, "summary": "5xx rate rose from 0.1% to 31%"},
        {"source": "loki", "iteration": 1, "summary": "KeyError in checkout handler"},
        {"source": "deploy_history", "iteration": 2, "summary": "v18 deployed 4 minutes before onset"},
        {"source": "git", "iteration": 2, "summary": "v18 changed the cart total calculation"},
    ],
    "expires_at": "2026-11-01T12:15:00+00:00",
}


def _flat(message) -> str:
    return json.dumps(message)


def _blocks(message, kind):
    return [block for block in message["blocks"] if block["type"] == kind]


def _text_of(message) -> str:
    """Every mrkdwn string in the message, joined, for content checks."""
    parts = []
    for block in message["blocks"]:
        if "text" in block:
            parts.append(block["text"]["text"])
        for field in block.get("fields", []):
            parts.append(field["text"])
        for element in block.get("elements", []):
            if "text" in element and isinstance(element["text"], dict):
                parts.append(element["text"]["text"])
    return "\n".join(parts)


def test_the_message_names_the_action_the_target_the_incident_and_the_tier():
    message = messages.pending_message(APPROVAL, DASHBOARD)

    header = _blocks(message, "header")[0]["text"]["text"]
    assert header == "Approval needed: rollback_deployment on checkout-service"
    body = _text_of(message)
    assert "#12" in body and "checkout-service" in body
    assert "Tier" in body and "2" in body
    assert "v17" in body
    assert message["text"] == "Approval needed: rollback_deployment on checkout-service (incident #12)"


def test_the_message_carries_the_diagnosis_and_the_three_most_recent_evidence_lines():
    body = _text_of(messages.pending_message(APPROVAL, DASHBOARD))

    assert "bad_deploy: checkout-service v18 raised the 5xx rate" in body
    assert "KeyError in checkout handler" in body
    assert "v18 deployed 4 minutes before onset" in body
    assert "v18 changed the cart total calculation" in body
    assert "5xx rate rose from 0.1% to 31%" not in body


def test_the_plan_reasoning_stays_out_of_slack():
    assert "ignored here" not in _flat(messages.pending_message(APPROVAL, DASHBOARD))


def test_the_expiry_is_a_slack_date_with_a_readable_fallback():
    body = _text_of(messages.pending_message(APPROVAL, DASHBOARD))

    assert "<!date^1793535300^" in body
    assert "|2026-11-01T12:15:00+00:00>" in body


def test_the_link_points_at_the_incident_page_of_the_dashboard():
    body = _text_of(messages.pending_message(APPROVAL, DASHBOARD))

    assert "<https://phoenix.example.com/dashboard/#/incidents/12|" in body


def test_approve_asks_for_confirmation_and_deny_does_not():
    message = messages.pending_message(APPROVAL, DASHBOARD)

    buttons = {button["action_id"]: button for button in _blocks(message, "actions")[0]["elements"]}
    assert set(buttons) == {"phoenix_approve", "phoenix_deny"}
    assert "confirm" in buttons["phoenix_approve"]
    assert "rollback_deployment" in buttons["phoenix_approve"]["confirm"]["text"]["text"]
    assert "confirm" not in buttons["phoenix_deny"]
    assert buttons["phoenix_approve"]["style"] == "primary"
    assert buttons["phoenix_deny"]["style"] == "danger"


def test_a_button_carries_only_the_approval_id():
    message = messages.pending_message(APPROVAL, DASHBOARD)

    for button in _blocks(message, "actions")[0]["elements"]:
        assert button["value"] == "7"


def test_markup_in_evidence_arrives_inert():
    hostile = dict(APPROVAL, evidence=[
        {"source": "loki", "iteration": 1, "summary": "<!channel> <@U012ABCDEF> <https://evil.example|click> & done"},
    ])

    body = _text_of(messages.pending_message(hostile, DASHBOARD))

    assert "<!channel>" not in body
    assert "<@U012ABCDEF>" not in body
    assert "<https://evil.example" not in body
    assert "&lt;!channel&gt;" in body
    assert "&amp; done" in body


def test_markup_in_the_diagnosis_the_args_and_the_service_arrives_inert():
    hostile = dict(
        APPROVAL,
        incident_summary="<!here> look",
        args=["<!everyone>"],
        service_name="<!channel>",
        container="<!channel>",
    )

    message = messages.pending_message(hostile, DASHBOARD)

    for forbidden in ("<!here>", "<!everyone>", "<!channel>"):
        assert forbidden not in _flat(message)


def test_every_field_is_cut_to_slacks_limits():
    huge = "x" * 10_000
    oversized = dict(APPROVAL, action=huge, container=huge, incident_summary=huge, args=[huge] * 50,
                     evidence=[{"source": huge, "iteration": 1, "summary": huge}] * 3, service_name=huge)

    message = messages.pending_message(oversized, DASHBOARD)

    assert len(_blocks(message, "header")[0]["text"]["text"]) <= 150
    for block in message["blocks"]:
        if "text" in block and block["type"] == "section":
            assert len(block["text"]["text"]) <= 3000
        for field in block.get("fields", []):
            assert len(field["text"]) <= 2000
    assert len(message["blocks"]) <= 50
    assert len(message["text"]) <= 3000


def test_escaping_does_not_push_a_field_over_the_limit_or_cut_an_entity_in_half():
    ampersands = "&" * 3000
    message = messages.pending_message(dict(APPROVAL, incident_summary=ampersands), DASHBOARD)

    section = next(b for b in message["blocks"] if b["type"] == "section" and "Diagnosis" in b.get("text", {}).get("text", ""))
    text = section["text"]["text"]
    assert len(text) <= 3000
    body = text.split("\n", 1)[1]
    assert body.replace("&amp;", "").replace("…", "") == ""


def test_a_missing_summary_or_empty_evidence_still_builds_a_message():
    bare = dict(APPROVAL, incident_summary="", evidence=[], args=[])

    message = messages.pending_message(bare, DASHBOARD)

    assert _blocks(message, "actions")
    assert "no surviving hypothesis" in _text_of(message) or "No diagnosis" in _text_of(message)


def test_an_unparseable_expiry_falls_back_to_the_raw_text():
    body = _text_of(messages.pending_message(dict(APPROVAL, expires_at="soon"), DASHBOARD))

    assert "soon" in body
    assert "<!date^" not in body


def test_the_message_holds_no_token_shaped_text_of_its_own():
    flat = _flat(messages.pending_message(APPROVAL, DASHBOARD))

    for prefix in ("xoxb-", "xapp-", "Bearer "):
        assert prefix not in flat


# --- the message after someone has answered -----------------------------------

from datetime import datetime, timezone

ANSWERED_AT = datetime(2026, 11, 1, 12, 5, 0, tzinfo=timezone.utc)


def _settled(decision="approved", approver="alice", channel="slack", blocks=None):
    original = blocks if blocks is not None else messages.pending_message(APPROVAL, DASHBOARD)["blocks"]
    return messages.settled_message(original, decision, approver, channel, ANSWERED_AT)


def test_a_settled_message_has_no_buttons_so_it_cannot_be_answered_twice():
    settled = _settled()

    assert _blocks(settled, "actions") == []


def test_a_settled_message_keeps_what_the_person_decided_on():
    original = messages.pending_message(APPROVAL, DASHBOARD)["blocks"]

    settled = _settled(blocks=original)

    kept = [block for block in original if block["type"] != "actions"]
    assert settled["blocks"][: len(kept)] == kept
    body = _text_of(settled)
    assert "bad_deploy: checkout-service v18" in body
    assert "<https://phoenix.example.com/dashboard/#/incidents/12|" in body


def test_an_approval_names_who_which_channel_and_when():
    body = _text_of(_settled("approved", "alice", "slack"))

    assert "Approved" in body
    assert "alice" in body
    assert "via slack" in body
    assert "<!date^1793534700^" in body
    assert "|2026-11-01T12:05:00+00:00>" in body


def test_a_denial_says_denied():
    body = _text_of(_settled("denied", "bob", "dashboard"))

    assert "Denied" in body and "bob" in body and "via dashboard" in body
    assert "Approved" not in body


def test_the_settled_text_is_the_last_block_and_the_fallback_names_the_outcome():
    settled = _settled()

    assert "Approved" in settled["blocks"][-1]["text"]["text"]
    assert settled["text"].startswith("Approved by alice")


def test_an_approver_name_with_markup_is_escaped_in_the_settled_text():
    settled = _settled(approver="<!channel>")

    assert "<!channel>" not in _flat(settled)
    assert "&lt;!channel&gt;" in _text_of(settled)


DEADLINE = datetime(2026, 11, 1, 12, 15, 0, tzinfo=timezone.utc)


def test_an_expired_message_says_no_one_answered_and_that_expiry_is_never_an_approval():
    original = messages.pending_message(APPROVAL, DASHBOARD)["blocks"]

    expired = messages.expired_message(original, DEADLINE)

    assert _blocks(expired, "actions") == []
    outcome = expired["blocks"][-1]["text"]["text"]
    assert "Expired" in outcome and "no one answered" in outcome
    assert "never an approval" in outcome
    assert "<!date^1793535300^" in outcome and "|2026-11-01T12:15:00+00:00>" in outcome
    assert expired["text"].startswith("Expired")


def test_an_expired_message_keeps_what_the_person_would_have_decided_on():
    original = messages.pending_message(APPROVAL, DASHBOARD)["blocks"]

    expired = messages.expired_message(original, DEADLINE)

    kept = [block for block in original if block["type"] != "actions"]
    assert expired["blocks"][: len(kept)] == kept


def test_expiring_twice_or_after_an_answer_leaves_exactly_one_outcome_line():
    answered = _settled()

    expired = messages.expired_message(answered["blocks"], DEADLINE)

    outcomes = [b for b in expired["blocks"] if b.get("block_id") == messages.OUTCOME_BLOCK_ID]
    assert len(outcomes) == 1 and "Expired" in outcomes[0]["text"]["text"]


def test_settling_a_message_that_has_no_buttons_adds_nothing_twice():
    once = _settled()

    twice = messages.settled_message(once["blocks"], "approved", "alice", "slack", ANSWERED_AT)

    assert len([b for b in twice["blocks"] if "Approved" in b.get("text", {}).get("text", "")]) == 1
