"""The bridge's table and role, read as text.

These do not run the SQL. They hold the two facts the design depends on: the bridge's
role can touch its own table and nothing else, and the table does not alter
`approvals`. Applying the files to a real Postgres is a separate, manual check.
"""

import re
from pathlib import Path

INIT = Path(__file__).resolve().parents[1] / "db" / "init"
TABLE_SQL = (INIT / "009_approval_notifications.sql").read_text(encoding="utf-8")
ROLE_SH = (INIT / "010_create_slack_bridge_role.sh").read_text(encoding="utf-8")


def _statements(text: str) -> list[str]:
    """The SQL statements in a file, comments removed and whitespace collapsed."""
    without_comments = "\n".join(line for line in text.splitlines() if not line.strip().startswith(("--", "#")))
    return [re.sub(r"\s+", " ", part).strip() for part in without_comments.split(";") if part.strip()]


def _grants(text: str) -> set[str]:
    return {statement for statement in _statements(text) if "GRANT " in statement}


def test_the_table_has_one_row_per_approval_and_a_foreign_key_to_it():
    statements = _statements(TABLE_SQL)

    assert len(statements) == 1
    create = statements[0]
    assert create.startswith("CREATE TABLE approval_notifications (")
    assert "approval_id BIGINT PRIMARY KEY REFERENCES approvals(id)" in create
    assert "channel_id TEXT NOT NULL" in create
    assert "message_ts TEXT NOT NULL" in create
    assert "posted_at TIMESTAMPTZ NOT NULL DEFAULT now()" in create
    assert "settled_at TIMESTAMPTZ" in create


def test_the_migration_never_alters_or_grants_on_the_approvals_table():
    for statement in _statements(TABLE_SQL):
        assert not re.search(r"\b(ALTER|UPDATE|DELETE|DROP|GRANT)\b", statement, re.IGNORECASE), statement
        assert not re.search(r"\bON approvals\b", statement), statement


def test_the_role_may_read_insert_and_set_settled_at_on_its_own_table_and_nothing_else():
    assert _grants(ROLE_SH) == {
        "GRANT CONNECT ON DATABASE $POSTGRES_DB TO slack_bridge",
        "GRANT USAGE ON SCHEMA public TO slack_bridge",
        "GRANT SELECT, INSERT ON approval_notifications TO slack_bridge",
        "GRANT UPDATE (settled_at) ON approval_notifications TO slack_bridge",
    }


def test_the_role_has_no_blanket_grant_and_no_delete():
    assert "ALL TABLES" not in ROLE_SH
    assert "ALL SEQUENCES" not in ROLE_SH
    assert "DELETE" not in ROLE_SH
    assert "SUPERUSER" not in ROLE_SH.upper()


def test_the_role_is_created_with_a_password_from_the_environment_not_a_literal():
    assert "CREATE ROLE slack_bridge WITH LOGIN PASSWORD '$SLACK_BRIDGE_PASSWORD'" in ROLE_SH
    assert ROLE_SH.startswith("#!/bin/bash\nset -e\n")
    assert "ON_ERROR_STOP=1" in ROLE_SH
