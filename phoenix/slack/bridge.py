"""Posts each open approval request to Slack once, and keeps doing so.

Usage: python -m phoenix.slack.bridge

This is a second host-side process, run the way the watcher is: it never calls the graph
and the graph never calls it. The graph writes a pending approval and pauses; this asks
the API for pending approvals, posts the ones it has not posted, and records each post in
its own table so a restart does not post them again. Dashboard and HTTP approvals keep
working with this process stopped, which is the point: an approval never lives only in Slack.

A pass that cannot do its whole job does nothing it cannot undo. If the API is down there
is nothing to post. If the notification table cannot be read, posting would repost the same
request on the next pass, so the pass is skipped. If Slack refuses a post, nothing is
recorded and the next pass tries again.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from psycopg_pool import ConnectionPool
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from phoenix.slack import messages
from phoenix.slack.api_client import ApiClient, ApiError
from phoenix.slack.config import SlackConfig, SlackConfigError, load_config
from phoenix.slack.store import NotificationStore, StoreError

# Anything that looks like a Slack token is masked before it is printed. The bridge never
# logs a token on purpose; this covers an exception whose text happens to carry one.
_SLACK_TOKEN = re.compile(r"\b(?:xox[abprs]|xapp)-[A-Za-z0-9-]+")

DEFAULT_RATE_LIMIT_WAIT_SECONDS = 30.0


def _say(message: str) -> None:
    print(_SLACK_TOKEN.sub("[SLACK_TOKEN]", message))


@dataclass(frozen=True)
class PassResult:
    """posted: messages posted and recorded this pass. wait: seconds Slack asked us to hold off."""

    posted: int = 0
    wait: float = 0.0


def _retry_after(exc: SlackApiError) -> float | None:
    """Seconds to wait if Slack rate-limited the call, otherwise None."""
    response = exc.response
    if getattr(response, "status_code", None) != 429:
        return None
    try:
        return float(response.headers.get("Retry-After", DEFAULT_RATE_LIMIT_WAIT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_RATE_LIMIT_WAIT_SECONDS


def post_pass(config: SlackConfig, api, store, slack) -> PassResult:
    """Post every open request that has no message yet, oldest first."""
    try:
        pending = api.list_pending()
    except ApiError as exc:
        _say(f"[slack-bridge] no posting this pass, the API is not usable: {exc}")
        return PassResult()
    if not pending:
        return PassResult()
    pending = sorted(pending, key=lambda approval: approval["id"])

    # Read before posting. A message that cannot be recorded would be posted again on the
    # next pass, so an unreadable table means no posting at all.
    try:
        known = store.notified_ids([approval["id"] for approval in pending])
    except StoreError as exc:
        _say(f"[slack-bridge] no posting this pass, the notification table is not usable: {exc}")
        return PassResult()

    posted = 0
    for approval in pending:
        approval_id = approval["id"]
        if approval_id in known:
            continue
        try:
            message = messages.pending_message(approval, config.dashboard_url)
            response = slack.chat_postMessage(channel=config.channel_id, unfurl_links=False, **message)
            message_ts = response["ts"]
        except SlackApiError as exc:
            wait = _retry_after(exc)
            if wait is not None:
                _say(f"[slack-bridge] Slack rate-limited the post of approval {approval_id}; holding off {wait:.0f}s")
                return PassResult(posted, wait)
            _say(f"[slack-bridge] approval {approval_id} was not posted, will retry next pass: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one bad request must not stop the rest
            _say(f"[slack-bridge] approval {approval_id} was not posted, will retry next pass: {type(exc).__name__}: {exc}")
            continue
        try:
            store.record(approval_id, config.channel_id, message_ts)
        except StoreError as exc:
            # Posted but unrecorded: it will be posted a second time. That is accepted, since
            # both messages carry the same approval id and the first answer wins. Posting more
            # while the table is failing would only add to it, so the pass stops here.
            _say(f"[slack-bridge] approval {approval_id} was posted but could not be recorded, stopping this pass: {exc}")
            return PassResult(posted)
        posted += 1
        _say(f"[slack-bridge] posted approval {approval_id} to {config.channel_id}")
    return PassResult(posted)


def run_forever(config: SlackConfig, api, store, slack, *, sleep=time.sleep, should_stop=lambda: False) -> None:
    """Check the table, then post on every poll. Raises if the migrations are missing."""
    store.check_ready()
    _say(f"[slack-bridge] posting pending approvals to {config.channel_id} every {config.poll_seconds:g}s")
    while not should_stop():
        result = post_pass(config, api, store, slack)
        sleep(max(config.poll_seconds, result.wait))


def main() -> None:
    try:
        config = load_config()
        pool = ConnectionPool(config.database_url, min_size=1, max_size=3, open=False, timeout=5)
        pool.open()
        run_forever(config, ApiClient(config.api_url), NotificationStore(pool), WebClient(token=config.bot_token))
    except (SlackConfigError, StoreError) as exc:
        _say(f"[slack-bridge] cannot start: {exc}")
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        _say("[slack-bridge] stopped")


if __name__ == "__main__":
    main()
