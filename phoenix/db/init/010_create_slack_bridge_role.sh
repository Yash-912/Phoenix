#!/bin/bash
set -e

# Same reasoning as 003 and 006: a .sh file so SLACK_BRIDGE_PASSWORD gets
# substituted from the container's environment.
#
# slack_bridge is deliberately its own role. It is the one process that talks to
# Slack, and everything it learns about approvals comes through the API, which
# redacts. So it gets no grant on approvals, incidents or audit_log: it can read
# and insert its own bookkeeping rows and set settled_at, and that is all. Delete
# is not granted, and settled_at is the only column it may update.
#
# Init scripts only run on an empty data directory. A database that already exists
# needs 009 and then this file applied by hand (see the README, "Slack approvals").

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE slack_bridge WITH LOGIN PASSWORD '$SLACK_BRIDGE_PASSWORD';

    GRANT CONNECT ON DATABASE $POSTGRES_DB TO slack_bridge;
    GRANT USAGE ON SCHEMA public TO slack_bridge;
    GRANT SELECT, INSERT ON approval_notifications TO slack_bridge;
    GRANT UPDATE (settled_at) ON approval_notifications TO slack_bridge;
EOSQL
