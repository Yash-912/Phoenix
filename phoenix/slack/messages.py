"""What the bridge says in Slack, built from the API's redacted view of an approval.

Everything here is a pure function of its arguments. The text it is given comes from
logs and tool output, so every dynamic string is escaped for Slack (& < >) before it
goes into a message: a log line holding <!channel> must arrive as text, not as a ping to
the whole channel. Slack counts the escaped length against its limits, so each string
is cut to fit after escaping, never in the middle of an entity.

A button carries only the approval id. Whatever the click then does, it does by asking
the API about that id, so nothing in the message can be edited into a different action.
"""

import re
from datetime import datetime

HEADER_LIMIT = 150
SECTION_LIMIT = 3000
FIELD_LIMIT = 1000
FALLBACK_LIMIT = 1000
CONFIRM_TEXT_LIMIT = 300
EVIDENCE_LINES = 3
EVIDENCE_LINE_LIMIT = 300

OUTCOME_BLOCK_ID = "outcome"

APPROVE_ACTION = "phoenix_approve"
DENY_ACTION = "phoenix_deny"

ELLIPSIS = "…"


def escape(text: str) -> str:
    """Slack's three control characters, as Slack asks them to be written."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fit(text, limit: int) -> str:
    """Escape text and cut it so the escaped result is at most `limit` characters."""
    raw = "" if text is None else str(text)
    escaped = escape(raw)
    if len(escaped) <= limit:
        return escaped
    # Each character grows by at most four when escaped, so shrink in steps sized to
    # the overflow. The ellipsis takes one place of the limit.
    while len(escaped) > limit - 1:
        raw = raw[: max(0, len(raw) - max(1, (len(escaped) - limit + 1) // 4))]
        escaped = escape(raw)
    return escaped + ELLIPSIS


def _plain(text, limit: int) -> str:
    """Text for a plain_text object. Angle brackets are dropped rather than escaped,
    since plain_text does not promise to decode entities."""
    cleaned = re.sub(r"[<>]", "", "" if text is None else str(text))
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + ELLIPSIS


def _section(title: str, body: str, limit: int = SECTION_LIMIT) -> dict:
    prefix = f"*{title}*\n"
    return {"type": "section", "text": {"type": "mrkdwn", "text": prefix + _fit(body, limit - len(prefix))}}


def _field(title: str, body) -> dict:
    prefix = f"*{title}*\n"
    return {"type": "mrkdwn", "text": prefix + _fit(body, FIELD_LIMIT - len(prefix))}


def _date_token(moment: datetime) -> str:
    """A Slack date, shown in the reader's own time zone, with a fallback for clients that cannot."""
    # The text after | is shown by clients that cannot render a date token.
    fallback = _fit(moment.isoformat(), 200).replace("|", "/")
    return f"<!date^{int(moment.timestamp())}^{{date_short_pretty}} {{time}}|{fallback}>"


def _expiry(expires_at) -> str:
    raw = "" if expires_at is None else str(expires_at)
    try:
        return _date_token(datetime.fromisoformat(raw))
    except ValueError:
        return _fit(raw, 200)


def _evidence_lines(evidence) -> list[str]:
    items = [item for item in (evidence or []) if isinstance(item, dict)]
    lines = []
    for item in items[-EVIDENCE_LINES:]:
        source = _fit(item.get("source") or "evidence", 60)
        lines.append(f"• *{source}*: {_fit(item.get('summary'), EVIDENCE_LINE_LIMIT)}")
    return lines


def _button(text: str, action_id: str, approval_id: int, style: str, confirm: dict | None = None) -> dict:
    button = {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": text},
        "style": style,
        "value": str(approval_id),
    }
    if confirm:
        button["confirm"] = confirm
    return button


def pending_message(approval: dict, dashboard_url: str) -> dict:
    """The message for one open request: kwargs for chat.postMessage, less the channel."""
    approval_id = approval["id"]
    incident_id = approval.get("incident_id")
    action = approval.get("action")
    container = approval.get("container")
    args = approval.get("args") or []
    args_text = ", ".join(str(arg) for arg in args) if isinstance(args, list) else str(args)

    confirm = {
        "title": {"type": "plain_text", "text": "Approve this action?"},
        "text": {"type": "mrkdwn", "text": _fit(
            f"Phoenix will run {action} on {container} once it resumes. Phoenix re-reads the live system first.",
            CONFIRM_TEXT_LIMIT,
        )},
        "confirm": {"type": "plain_text", "text": "Approve"},
        "deny": {"type": "plain_text", "text": "Cancel"},
    }

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": _plain(f"Approval needed: {action} on {container}", HEADER_LIMIT)}},
        {"type": "section", "fields": [
            _field("Incident", f"#{incident_id} · {approval.get('service_name') or 'unknown service'}"),
            _field("Tier", approval.get("tier")),
            _field("Target", container),
            _field("Arguments", args_text or "none"),
            {"type": "mrkdwn", "text": f"*Expires*\n{_expiry(approval.get('expires_at'))}"},
        ]},
        _section("Diagnosis", approval.get("incident_summary") or "No diagnosis recorded."),
    ]
    lines = _evidence_lines(approval.get("evidence"))
    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Recent evidence*\n" + "\n".join(lines)}})
    link = escape(f"{dashboard_url}/dashboard/#/incidents/{incident_id}")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"<{link}|Open the incident in the dashboard>"}})
    blocks.append({
        "type": "actions",
        "block_id": f"approval:{approval_id}",
        "elements": [
            _button("Approve", APPROVE_ACTION, approval_id, "primary", confirm),
            _button("Deny", DENY_ACTION, approval_id, "danger"),
        ],
    })

    return {
        "text": _fit(f"Approval needed: {action} on {container} (incident #{incident_id})", FALLBACK_LIMIT),
        "blocks": blocks,
    }


def _closed(blocks: list[dict], text: str, fallback: str) -> dict:
    """The message with its buttons and any earlier outcome line removed, and one outcome line added."""
    kept = [block for block in blocks if block.get("type") != "actions" and block.get("block_id") != OUTCOME_BLOCK_ID]
    kept.append({"type": "section", "block_id": OUTCOME_BLOCK_ID, "text": {"type": "mrkdwn", "text": text}})
    return {"text": fallback, "blocks": kept}


def settled_message(blocks: list[dict], decision: str, approver: str, channel: str, decided_at: datetime) -> dict:
    """The same message once it has been answered: no buttons, and a line saying who and how.

    decision is "approved" or "denied". Whatever outcome line an earlier edit added is
    replaced, so settling a message twice leaves exactly one.
    """
    word = "Approved" if decision == "approved" else "Denied"
    who = _fit(approver, 100)
    via = _fit(channel, 30)
    return _closed(blocks, f"*{word}* by {who} via {via} at {_date_token(decided_at)}", f"{word} by {who} via {via}")


def expired_message(blocks: list[dict], deadline: datetime) -> dict:
    """The same message once its time ran out with no answer. Expiry escalates and is never an approval."""
    return _closed(
        blocks,
        f"*Expired*: no one answered by {_date_token(deadline)}. Phoenix escalated this and took no action. "
        f"Expiry is never an approval.",
        "Expired: no one answered, escalated, no action taken",
    )
