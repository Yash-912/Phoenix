"""Posts each open approval request to Slack once, and keeps doing so.

Usage: python -m phoenix.slack.bridge

Three things happen here. A loop posts each open request to the channel and records it. The same
loop closes any posted message whose request has since been answered elsewhere or has expired. And a
Socket Mode connection (outbound only, no port) receives the Approve and Deny clicks, acks
them, and sends the decision to the API under the clicker's approver name.

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
from datetime import datetime, timezone

from psycopg_pool import ConnectionPool
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from phoenix.slack import messages
from phoenix.slack.api_client import SLACK_CHANNEL, ApiClient, ApiError
from phoenix.slack.config import SlackConfig, SlackConfigError, load_config
from phoenix.slack.handlers import handle_click
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


def make_click_listener(config: SlackConfig, api, store, *, now=lambda: datetime.now(timezone.utc)):
    """The function Bolt calls for a click on Approve or Deny.

    ack() comes first, before the payload is even read, because Slack gives three seconds.
    The rest happens after it: the decision goes to the API, and a recorded decision edits
    the message and marks its row settled. A failure at any step after the decision is
    logged and, where the clicker needs to know, told to them alone.
    """

    def on_click(ack, body, respond, client):
        ack()
        try:
            action = body["actions"][0]
            user_id = body["user"]["id"]
            channel_id = (body.get("channel") or {}).get("id", "")
            action_id, value = action.get("action_id"), action.get("value")
        except (KeyError, IndexError, TypeError, AttributeError):
            _say("[slack-bridge] a click payload was malformed and was dropped")
            return

        outcome = handle_click(config, api, user_id=user_id, action_id=action_id, channel_id=channel_id, value=value)
        if outcome.ignored:
            _say(f"[slack-bridge] ignored a click on {action_id!r} that is not one of ours")
            return
        if outcome.reply:
            # Logged so the owner can see who tried to answer and what they were told, including a
            # refusal that changed nothing and so leaves no other trace.
            _say(f"[slack-bridge] Slack user {user_id} clicked {action_id} on approval {value} and was told privately: {outcome.reply}")
            _reply(respond, outcome.reply)
            return

        settle = outcome.settle
        _say(f"[slack-bridge] {settle.approver} {settle.decision} approval {settle.approval_id} from Slack")
        try:
            message = messages.settled_message(body["message"]["blocks"], settle.decision, settle.approver, SLACK_CHANNEL, now())
            client.chat_update(channel=channel_id, ts=body["message"]["ts"], text=message["text"], blocks=message["blocks"])
        except Exception as exc:  # noqa: BLE001 - the decision is already recorded, so a failed edit must not undo it
            _say(f"[slack-bridge] approval {settle.approval_id} was {settle.decision} but its message could not be edited: {type(exc).__name__}: {exc}")
            _reply(respond, "Your answer was recorded, but I could not update the message. The dashboard shows the outcome.")
            return
        try:
            store.settle(settle.approval_id)
        except StoreError as exc:
            _say(f"[slack-bridge] approval {settle.approval_id} was edited but could not be marked settled: {exc}")

    return on_click


def _reply(respond, text: str) -> None:
    """A note only the clicker sees. If it cannot be sent, that is logged and nothing else happens."""
    try:
        respond(text=text, response_type="ephemeral", replace_original=False)
    except Exception as exc:  # noqa: BLE001 - the click is already acked and handled
        _say(f"[slack-bridge] could not send a reply to the clicker: {type(exc).__name__}: {exc}")


def build_app(config: SlackConfig, api, store, **app_options) -> App:
    """The Bolt app with the two buttons wired to the listener. Nothing else is handled."""
    app = App(token=config.bot_token, **app_options)
    app.action(re.compile(r"^(" + messages.APPROVE_ACTION + "|" + messages.DENY_ACTION + r")$"))(make_click_listener(config, api, store))
    return app


@dataclass(frozen=True)
class SettleResult:
    """settled: messages edited and marked settled this pass. wait: seconds Slack asked us to hold off."""

    settled: int = 0
    wait: float = 0.0


# Slack answers that mean this message can never be edited: it was deleted, or the channel
# is gone. Retrying would never end, so the row is marked settled and the reason is logged.
UNEDITABLE = frozenset({"message_not_found", "channel_not_found", "cant_update_message"})


def _moment(value) -> datetime:
    """A timestamp from the API, or now if it is missing or unreadable."""
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(timezone.utc)


def _closing_message(config: SlackConfig, approval: dict) -> dict | None:
    """What a posted message should now say, or None while its request is still open."""
    status = approval.get("status")
    if status == "pending" and not approval.get("overdue"):
        return None
    blocks = messages.pending_message(approval, config.dashboard_url)["blocks"]
    if status in ("approved", "denied"):
        return messages.settled_message(
            blocks, status, approval.get("decided_by") or "unknown", approval.get("decision_channel") or "unknown",
            _moment(approval.get("decided_at")),
        )
    if status == "expired" or status == "pending":
        # A pending request past its deadline is as good as expired: the API refuses an answer
        # to it, and the watcher is about to mark it.
        return messages.expired_message(blocks, _moment(approval.get("expires_at")))
    return None


def settle_pass(config: SlackConfig, api, store, slack) -> SettleResult:
    """Close every posted message whose request is no longer open, whichever channel closed it.

    A click edits its own message, but an answer from the dashboard or the API, and an expiry,
    leave the buttons up. Each unsettled message is checked against the API and edited once its
    request has been decided or has run out of time. The row is marked settled only after the
    edit lands, so a failure anywhere leaves it to be tried again on the next pass.
    """
    try:
        waiting = store.unsettled()
    except StoreError as exc:
        _say(f"[slack-bridge] no settling this pass, the notification table is not usable: {exc}")
        return SettleResult()

    settled = 0
    for notification in sorted(waiting, key=lambda row: row.approval_id):
        approval_id = notification.approval_id
        try:
            approval = api.get_approval(approval_id)
        except ApiError as exc:
            # If the API is down it is down for every message, so the pass stops here.
            _say(f"[slack-bridge] no more settling this pass, the API is not usable: {exc}")
            return SettleResult(settled)
        if approval is None:
            _say(f"[slack-bridge] approval {approval_id} is not known to the API, leaving its message alone")
            continue
        try:
            message = _closing_message(config, approval)
        except Exception as exc:  # noqa: BLE001 - one odd row must not stop the others
            _say(f"[slack-bridge] approval {approval_id} could not be turned into a closing message: {type(exc).__name__}: {exc}")
            continue
        if message is None:
            continue
        try:
            slack.chat_update(channel=notification.channel_id, ts=notification.message_ts, text=message["text"], blocks=message["blocks"])
        except SlackApiError as exc:
            wait = _retry_after(exc)
            if wait is not None:
                _say(f"[slack-bridge] Slack rate-limited the edit of approval {approval_id}; holding off {wait:.0f}s")
                return SettleResult(settled, wait)
            code = exc.response.get("error") if exc.response is not None else None
            if code in UNEDITABLE:
                _say(f"[slack-bridge] the message for approval {approval_id} can no longer be edited ({code}), so it is closed in the table")
                _mark_settled(store, approval_id)
            else:
                _say(f"[slack-bridge] the message for approval {approval_id} was not edited, will retry next pass: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - leave the row open and try again next pass
            _say(f"[slack-bridge] the message for approval {approval_id} was not edited, will retry next pass: {type(exc).__name__}: {exc}")
            continue
        if _mark_settled(store, approval_id):
            settled += 1
            _say(f"[slack-bridge] closed the message for approval {approval_id} ({approval.get('status')})")
    return SettleResult(settled)


def _mark_settled(store, approval_id: int) -> bool:
    try:
        store.settle(approval_id)
    except StoreError as exc:
        # The message is already closed. The next pass edits it again to the same text.
        _say(f"[slack-bridge] approval {approval_id} was edited but could not be marked settled: {exc}")
        return False
    return True


def run_forever(config: SlackConfig, api, store, slack, *, sleep=time.sleep, should_stop=lambda: False) -> None:
    """Check the table, then post and settle on every poll. Raises if the migrations are missing."""
    store.check_ready()
    _say(f"[slack-bridge] posting pending approvals to {config.channel_id} every {config.poll_seconds:g}s")
    while not should_stop():
        posted = post_pass(config, api, store, slack)
        settled = settle_pass(config, api, store, slack)
        sleep(max(config.poll_seconds, posted.wait, settled.wait))


def main() -> None:
    try:
        config = load_config()
        pool = ConnectionPool(config.database_url, min_size=1, max_size=3, open=False, timeout=5)
        pool.open()
        store = NotificationStore(pool, database_url=config.database_url)
        # Checked before the connection opens, so a missing migration stops the process
        # before it can receive a click it could not settle.
        store.check_ready()
        api = ApiClient(config.api_url, token=config.approval_token)
        app = build_app(config, api, store)
        handler = SocketModeHandler(app, config.app_token)
        handler.connect()
        _say("[slack-bridge] connected to Slack over Socket Mode, listening for Approve and Deny")
        run_forever(config, api, store, app.client)
    except (SlackConfigError, StoreError) as exc:
        _say(f"[slack-bridge] cannot start: {exc}")
        raise SystemExit(1) from exc
    except SlackApiError as exc:
        _say(f"[slack-bridge] cannot start: Slack refused the connection: {exc}")
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        _say("[slack-bridge] stopped")


if __name__ == "__main__":
    main()
