"""The Slack bridge's configuration: tokens, channel, who may answer, where the API is.

Read once from environment variables when the bridge starts. An invalid value stops
the process instead of falling back to something looser, for the same reason
safety_policy.py does: a bridge with no approver map, or a channel it guessed, would
look healthy while posting buttons nobody could use.

Nothing here comes from the model or from a Slack message. The map from a Slack user
ID to an approver name is the only place a Slack identity becomes an approver, and the
API still checks the name against PHOENIX_APPROVERS before it records anything.
"""

import math
import os
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

ENV_APP_TOKEN = "SLACK_APP_TOKEN"
ENV_BOT_TOKEN = "SLACK_BOT_TOKEN"
ENV_CHANNEL = "PHOENIX_SLACK_CHANNEL"
ENV_SLACK_APPROVERS = "PHOENIX_SLACK_APPROVERS"
ENV_APPROVAL_TOKEN = "PHOENIX_APPROVAL_TOKEN"
ENV_API_URL = "PHOENIX_API_URL"
ENV_DASHBOARD_URL = "PHOENIX_DASHBOARD_URL"
ENV_POLL_SECONDS = "PHOENIX_SLACK_POLL_SECONDS"
ENV_DATABASE_URL = "DATABASE_URL"

DEFAULT_API_URL = "http://phoenix-api:8000"
DEFAULT_POLL_SECONDS = 5.0

APP_TOKEN_PREFIX = "xapp-"
BOT_TOKEN_PREFIX = "xoxb-"

# Slack IDs are upper-case letters and digits. A channel ID starts C (or G on older
# private channels), a user ID starts U (or W on enterprise grids). A channel name
# such as "general" is refused: names change, and a message sent by name can land in
# a different channel than the one the owner meant.
CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{8,}")
USER_ID = re.compile(r"[UW][A-Z0-9]{8,}")


class SlackConfigError(ValueError):
    """The configuration is unusable. The process must not start on it."""


@dataclass(frozen=True)
class SlackConfig:
    app_token: str
    bot_token: str
    channel_id: str
    approvers: Mapping[str, str]
    approval_token: str
    api_url: str
    dashboard_url: str
    database_url: str
    poll_seconds: float


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SlackConfigError(f"{name} is required and is not set")
    return value


def _token(env: Mapping[str, str], name: str, prefix: str) -> str:
    value = _required(env, name)
    # The value is never put in the message: a token pasted into the wrong variable
    # is still a token, and an error is the kind of text that ends up in a log.
    if not value.startswith(prefix):
        raise SlackConfigError(f"{name} must start with {prefix!r}")
    return value


def _url(env: Mapping[str, str], name: str, default: str | None = None) -> str:
    value = env.get(name, "").strip() or default
    if not value:
        raise SlackConfigError(f"{name} is required and is not set")
    if not re.match(r"https?://[^/\s]+", value):
        raise SlackConfigError(f"{name} must be a full URL starting http:// or https://, not {value!r}")
    return value.rstrip("/")


def slack_approvers(raw: str) -> dict[str, str]:
    """Slack user id -> approver name, from PHOENIX_SLACK_APPROVERS.

    An empty map is an error, not "nobody may answer": a bridge that posts buttons
    no one can use would look healthy and do nothing.
    """
    mapping: dict[str, str] = {}
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        user_id, sep, name = (piece.strip() for piece in entry.partition("="))
        if not sep or not name or not USER_ID.fullmatch(user_id):
            raise SlackConfigError(
                f"{ENV_SLACK_APPROVERS} entry {entry!r} must look like U012ABCDEF=alice, "
                f"a Slack user ID (not a display name) then an approver name"
            )
        if user_id in mapping:
            raise SlackConfigError(f"{ENV_SLACK_APPROVERS} lists {user_id} twice")
        mapping[user_id] = name
    if not mapping:
        raise SlackConfigError(f"{ENV_SLACK_APPROVERS} is empty, so no one could answer from Slack")
    return mapping


def _poll_seconds(env: Mapping[str, str]) -> float:
    raw = env.get(ENV_POLL_SECONDS, "").strip()
    if not raw:
        return DEFAULT_POLL_SECONDS
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise SlackConfigError(f"{ENV_POLL_SECONDS} must be a number of seconds, not {raw!r}") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise SlackConfigError(f"{ENV_POLL_SECONDS} must be a positive number of seconds, not {raw!r}")
    return seconds


def load_config(env: Mapping[str, str] = os.environ) -> SlackConfig:
    channel = _required(env, ENV_CHANNEL)
    if not CHANNEL_ID.fullmatch(channel):
        raise SlackConfigError(f"{ENV_CHANNEL} must be a Slack channel ID such as C0123456789, not a name")
    return SlackConfig(
        app_token=_token(env, ENV_APP_TOKEN, APP_TOKEN_PREFIX),
        bot_token=_token(env, ENV_BOT_TOKEN, BOT_TOKEN_PREFIX),
        channel_id=channel,
        approvers=MappingProxyType(slack_approvers(_required(env, ENV_SLACK_APPROVERS))),
        approval_token=_required(env, ENV_APPROVAL_TOKEN),
        api_url=_url(env, ENV_API_URL, DEFAULT_API_URL),
        dashboard_url=_url(env, ENV_DASHBOARD_URL),
        database_url=_required(env, ENV_DATABASE_URL),
        poll_seconds=_poll_seconds(env),
    )
