"""The bridge's own table: what it writes, what it reads, and how a failure is reported.

A fake pool stands in for Postgres, so these check the statements and the error
mapping. The table and the role are exercised against a real Postgres by hand.
"""

import re

import psycopg
import pytest

from phoenix.slack import store
from phoenix.slack.store import Notification, NotificationStore, StoreError, StoreNotReady


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, results, error=None):
        self._results = list(results)
        self._error = error
        self.executed: list[tuple[str, dict | None]] = []
        self.commits = 0

    def execute(self, sql, params=None):
        if self._error:
            raise self._error
        self.executed.append((sql, params))
        return _Cursor(self._results.pop(0) if self._results else [])

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Pool:
    def __init__(self, *results, error=None):
        self.conn = _Conn(results, error)

    def connection(self):
        return self.conn


def test_recording_a_post_inserts_once_and_commits():
    pool = _Pool()

    NotificationStore(pool).record(7, "C0123456789", "1730000000.000100")

    sql, params = pool.conn.executed[0]
    assert "INSERT INTO approval_notifications" in sql
    assert "ON CONFLICT (approval_id) DO NOTHING" in sql
    assert params == {"approval_id": 7, "channel_id": "C0123456789", "message_ts": "1730000000.000100"}
    assert pool.conn.commits == 1


def test_the_notified_ids_are_the_requested_ids_that_already_have_a_message():
    pool = _Pool([(7,), (9,)])

    notified = NotificationStore(pool).notified_ids([7, 8, 9])

    assert notified == {7, 9}
    assert pool.conn.executed[0][1] == {"ids": [7, 8, 9]}


def test_asking_about_no_ids_does_not_touch_the_database():
    pool = _Pool()

    assert NotificationStore(pool).notified_ids([]) == set()
    assert pool.conn.executed == []


def test_unsettled_returns_only_the_open_messages_oldest_first():
    pool = _Pool([(7, "C0123456789", "1730000000.000100"), (9, "C0123456789", "1730000300.000200")])

    rows = NotificationStore(pool).unsettled()

    assert rows == [
        Notification(7, "C0123456789", "1730000000.000100"),
        Notification(9, "C0123456789", "1730000300.000200"),
    ]
    sql = pool.conn.executed[0][0]
    assert "WHERE settled_at IS NULL" in sql
    assert "ORDER BY approval_id" in sql


def test_settling_only_moves_a_row_that_is_still_open():
    pool = _Pool()

    NotificationStore(pool).settle(7)

    sql, params = pool.conn.executed[0]
    assert "SET settled_at = now()" in sql
    assert "settled_at IS NULL" in sql
    assert params == {"approval_id": 7}
    assert pool.conn.commits == 1


def test_no_statement_names_the_approvals_table():
    statements = [store.CHECK_READY, store.NOTIFIED_IDS, store.INSERT_NOTIFICATION, store.UNSETTLED, store.SETTLE]

    for sql in statements:
        assert not re.search(r"\bapprovals\b", sql), sql


@pytest.mark.parametrize("call", [
    lambda s: s.record(7, "C0123456789", "1.1"),
    lambda s: s.notified_ids([7]),
    lambda s: s.unsettled(),
    lambda s: s.settle(7),
])
def test_a_database_error_is_reported_as_a_store_error(call):
    pool = _Pool(error=psycopg.OperationalError("connection refused"))

    with pytest.raises(StoreError, match="connection refused"):
        call(NotificationStore(pool))


def test_the_startup_check_passes_when_the_table_is_readable():
    pool = _Pool([])

    NotificationStore(pool).check_ready()

    assert "approval_notifications" in pool.conn.executed[0][0]


@pytest.mark.parametrize("error", [
    psycopg.errors.UndefinedTable('relation "approval_notifications" does not exist'),
    psycopg.errors.InsufficientPrivilege("permission denied for table approval_notifications"),
])
def test_a_missing_table_or_role_says_which_migrations_to_apply(error):
    pool = _Pool(error=error)

    with pytest.raises(StoreNotReady) as caught:
        NotificationStore(pool).check_ready()

    message = str(caught.value)
    assert "009" in message and "010" in message
    assert "README" in message


def test_the_startup_message_keeps_only_the_first_line_of_the_database_error():
    error = psycopg.errors.UndefinedTable('relation "approval_notifications" does not exist\nLINE 1: SELECT approval_id\n        ^')
    pool = _Pool(error=error)

    with pytest.raises(StoreNotReady) as caught:
        NotificationStore(pool).check_ready()

    assert "\n" not in str(caught.value)
    assert "LINE 1" not in str(caught.value)


def test_an_unreachable_database_at_startup_is_not_blamed_on_the_migrations():
    pool = _Pool(error=psycopg.OperationalError("connection refused"))

    with pytest.raises(StoreError) as caught:
        NotificationStore(pool).check_ready()

    assert not isinstance(caught.value, StoreNotReady)
    assert "009" not in str(caught.value)
