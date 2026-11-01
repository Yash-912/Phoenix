"""What a click on Approve or Deny does.

handle_click is a function of four values taken from the Slack payload (who clicked, which
button, in which channel, and the button's value) and the API client. Nothing else from
the payload is read: not the message text, not the blocks. The approval id comes from the
button value and the identity from the signed user id, so editing what a message says
cannot change what a click does.

The Slack user id is turned into an approver name through the configured map, and only
that name goes to the API. The API still checks the name against its own approver list,
the token and the deadline, and the database still lets only the first answer win. This
module decides nothing the API would not also check.

Acking the click is the caller's job and comes first, because Slack gives three seconds.
"""

import re
from dataclasses import dataclass

from phoenix.slack.api_client import DecisionResult
from phoenix.slack.config import SlackConfig
from phoenix.slack.messages import APPROVE_ACTION, DENY_ACTION

# Ids are plain digits. Anything else in a button value is not ours.
APPROVAL_ID = re.compile(r"[0-9]{1,18}")

NOT_AN_APPROVER = (
    "You are not on the approver list for Slack, so nothing was recorded. "
    "Ask the owner to add your Slack ID, or answer from the dashboard."
)


@dataclass(frozen=True)
class Settle:
    """A decision that was recorded, so the message should be edited to show it."""

    approval_id: int
    decision: str  # "approved" or "denied"
    approver: str


@dataclass(frozen=True)
class ClickOutcome:
    """reply: an ephemeral note for the person who clicked. settle: set when a decision was recorded.
    ignored: the click was not one of ours and nothing at all should happen."""

    reply: str | None = None
    settle: Settle | None = None
    ignored: bool = False


def _reply_for(result: DecisionResult) -> str:
    if result.kind == "already_decided":
        return f"Already {result.status}. The first answer wins, so your click changed nothing."
    if result.kind == "expired":
        return "The deadline passed. This request escalates and is never an approval."
    if result.kind == "not_on_list":
        return (
            "Your Slack ID maps to a name the server does not accept as an approver. "
            "Ask the owner to check PHOENIX_APPROVERS."
        )
    if result.kind == "not_configured":
        return "Approvals are not set up on the server (the token or the approver list), so nothing was recorded. Use the dashboard once that is fixed."
    if result.kind == "not_found":
        return "That request no longer exists."
    if result.kind == "unreachable":
        return "Could not reach Phoenix, so nothing was recorded. Use the dashboard."
    return "Phoenix gave an unexpected answer, so nothing was recorded. Use the dashboard."


def handle_click(config: SlackConfig, api, *, user_id: str, action_id: str, channel_id: str, value) -> ClickOutcome:
    if action_id not in (APPROVE_ACTION, DENY_ACTION):
        return ClickOutcome(ignored=True)
    if channel_id != config.channel_id:
        return ClickOutcome(ignored=True)
    if not isinstance(value, str) or not APPROVAL_ID.fullmatch(value):
        return ClickOutcome(ignored=True)

    approver = config.approvers.get(user_id)
    if approver is None:
        return ClickOutcome(reply=NOT_AN_APPROVER)

    approval_id = int(value)
    decision = "approve" if action_id == APPROVE_ACTION else "deny"
    result = api.decide(approval_id, decision, approver)
    if result.kind == "recorded":
        return ClickOutcome(settle=Settle(approval_id, result.status, approver))
    return ClickOutcome(reply=_reply_for(result))
