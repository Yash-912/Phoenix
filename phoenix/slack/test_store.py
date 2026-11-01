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


# --- why the database could not be reached ------------------------------------

from psycopg_pool import PoolTimeout

URL = "postgresql://slack_bridge@postgres:5432/phoenix"
AUTH_FAILED = psycopg.OperationalError(
    'connection failed: connection to server at "10.0.0.5", port 5432 failed: FATAL:  password authentication failed for user "slack_bridge"\n'
    "connection to server at 10.0.0.5 failed: second line"
)


def _store_that_times_out(database_url=URL):
    return NotificationStore(_Pool(error=PoolTimeout("couldn't get a connection after 5.00 sec")), database_url=database_url)


def test_a_pool_timeout_with_a_missing_role_says_so_and_names_the_migrations(monkeypatch):
    def refuse(url, **kwargs):
        raise AUTH_FAILED

    monkeypatch.setattr(store.psycopg, "connect", refuse)

    with pytest.raises(StoreError) as caught:
        _store_that_times_out().check_ready()

    message = str(caught.value)
    assert 'password authentication failed for user "slack_bridge"' in message
    assert "009" in message and "010" in message and "README" in message
    assert "SLACK_BRIDGE_PASSWORD" in message
    assert "second line" not in message


def test_a_pool_timeout_for_any_other_reason_gives_that_reason_and_no_migration_advice(monkeypatch):
    def refuse(url, **kwargs):
        raise psycopg.OperationalError("connection failed: connection refused\n  Is the server running?")

    monkeypatch.setattr(store.psycopg, "connect", refuse)

    with pytest.raises(StoreError) as caught:
        _store_that_times_out().check_ready()

    assert "connection refused" in str(caught.value)
    assert "009" not in str(caught.value)


def test_a_pool_timeout_where_a_direct_connection_works_says_it_timed_out(monkeypatch):
    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(store.psycopg, "connect", lambda url, **kwargs: Conn())

    with pytest.raises(StoreError, match="timed out"):
        _store_that_times_out().check_ready()


def test_without_a_url_a_pool_timeout_is_still_a_plain_store_error():
    with pytest.raises(StoreError, match="could not be reached"):
        _store_that_times_out(database_url=None).check_ready()


def test_the_direct_check_never_puts_the_password_in_the_message(monkeypatch):
    seen = {}

    def refuse(url, **kwargs):
        seen["url"] = url
        raise AUTH_FAILED

    monkeypatch.setattr(store.psycopg, "connect", refuse)

    with pytest.raises(StoreError) as caught:
        _store_that_times_out("postgresql://slack_bridge:s3cretpw@postgres:5432/phoenix").check_ready()

    assert seen["url"].endswith("/phoenix")
    assert "s3cretpw" not in str(caught.value)
