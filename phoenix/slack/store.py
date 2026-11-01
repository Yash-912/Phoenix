"""The bridge's own record of which Slack message belongs to which approval.

This is the only table the bridge writes. It says "approval 7 was posted as message
X in channel Y, and has (or has not) been edited to show its outcome". A restart reads
it instead of posting the same request again. Everything the bridge knows about the
approvals themselves comes through the API, so no statement here names that table.

Every failure is raised as StoreError. The bridge treats that as "skip this pass":
posting a message it cannot record would post it again on the next one.
"""

from dataclasses import dataclass

import psycopg
from psycopg_pool import PoolTimeout

NOT_READY_HELP = (
    "apply migrations 009_approval_notifications.sql and 010_create_slack_bridge_role.sh "
    "to this database (see the README, 'Slack approvals')"
)

# A database made before the bridge existed has no slack_bridge role. Postgres answers a login as
# a role that does not exist with the same "authentication failed" as a wrong password, so the
# message has to cover both.
AUTH_HELP = (
    " (a database created before the Slack bridge existed has no slack_bridge role yet: "
    "apply migrations 009_approval_notifications.sql and 010_create_slack_bridge_role.sh, see the "
    "README, 'Slack approvals'; otherwise check SLACK_BRIDGE_PASSWORD)"
)

CHECK_READY = "SELECT approval_id FROM approval_notifications LIMIT 1"

NOTIFIED_IDS = "SELECT approval_id FROM approval_notifications WHERE approval_id = ANY(%(ids)s)"

INSERT_NOTIFICATION = """
INSERT INTO approval_notifications (approval_id, channel_id, message_ts)
VALUES (%(approval_id)s, %(channel_id)s, %(message_ts)s)
ON CONFLICT (approval_id) DO NOTHING
"""

UNSETTLED = """
SELECT approval_id, channel_id, message_ts
FROM approval_notifications
WHERE settled_at IS NULL
ORDER BY approval_id
"""

# Only an open row can be settled, so settling twice changes nothing the second time.
SETTLE = """
UPDATE approval_notifications
SET settled_at = now()
WHERE approval_id = %(approval_id)s AND settled_at IS NULL
"""


class StoreError(RuntimeError):
    """The notification table could not be read or written."""


class StoreNotReady(StoreError):
    """The table or the role is missing, so the migrations have not been applied."""


@dataclass(frozen=True)
class Notification:
    approval_id: int
    channel_id: str
    message_ts: str


def _first_line(exc: Exception) -> str:
    """Postgres and libpq append detail lines and excerpts to an error; the first line says it all."""
    text = str(exc)
    return text.splitlines()[0] if text else type(exc).__name__


class NotificationStore:
    def __init__(self, pool, *, database_url: str | None = None) -> None:
        self._pool = pool
        # Only used to find out why the pool could not connect. The pool reports a timeout and
        # keeps the real reason (a wrong password, a missing role, a refused connection) in its log.
        self._database_url = database_url

    def check_ready(self) -> None:
        """Fail at startup, with the fix in the message, if the migrations are missing."""
        try:
            with self._pool.connection() as conn:
                conn.execute(CHECK_READY).fetchone()
        except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege) as exc:
            raise StoreNotReady(f"the notification table is not usable ({_first_line(exc)}); {NOT_READY_HELP}") from exc
        except PoolTimeout as exc:
            raise self._why_unreachable(exc) from exc
        except psycopg.Error as exc:
            raise StoreError(f"the database could not be reached: {exc}") from exc

    def _why_unreachable(self, timeout: PoolTimeout) -> StoreError:
        if not self._database_url:
            return StoreError(f"the database could not be reached: {timeout}")
        try:
            psycopg.connect(self._database_url, connect_timeout=5).close()
        except psycopg.Error as direct:
            reason = _first_line(direct)
            hint = AUTH_HELP if "authentication failed" in reason.lower() else ""
            return StoreError(f"the database could not be reached: {reason}{hint}")
        return StoreError(
            f"the database could not be reached: the connection pool timed out ({timeout}), "
            "although a direct connection works; try again"
        )

    def notified_ids(self, approval_ids: list[int]) -> set[int]:
        """Which of these approvals already have a message."""
        if not approval_ids:
            return set()
        rows = self._run(NOTIFIED_IDS, {"ids": list(approval_ids)}, fetch=True)
        return {row[0] for row in rows}

    def record(self, approval_id: int, channel_id: str, message_ts: str) -> None:
        self._run(INSERT_NOTIFICATION, {"approval_id": approval_id, "channel_id": channel_id, "message_ts": message_ts}, commit=True)

    def unsettled(self) -> list[Notification]:
        return [Notification(*row) for row in self._run(UNSETTLED, None, fetch=True)]

    def settle(self, approval_id: int) -> None:
        self._run(SETTLE, {"approval_id": approval_id}, commit=True)

    def _run(self, sql: str, params: dict | None, *, fetch: bool = False, commit: bool = False):
        try:
            with self._pool.connection() as conn:
                cursor = conn.execute(sql, params)
                rows = cursor.fetchall() if fetch else None
                if commit:
                    conn.commit()
        except psycopg.Error as exc:
            raise StoreError(f"the notification table could not be used: {exc}") from exc
        return rows
