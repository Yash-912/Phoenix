#!/bin/bash
set -e

# This runs as a .sh script (not .sql) specifically so it can read
# PHOENIX_APP_PASSWORD from the container's environment — plain .sql
# init files get no variable substitution, only .sh scripts do.

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE phoenix_app WITH LOGIN PASSWORD '$PHOENIX_APP_PASSWORD';

    GRANT CONNECT ON DATABASE $POSTGRES_DB TO phoenix_app;
    GRANT USAGE ON SCHEMA public TO phoenix_app;

    -- Normal CRUD tables: the app needs to read, create, and update rows.
    GRANT SELECT, INSERT, UPDATE ON incidents TO phoenix_app;
    GRANT SELECT, INSERT ON evidence TO phoenix_app;
    GRANT SELECT, INSERT, UPDATE ON hypotheses TO phoenix_app;

    -- audit_log: SELECT and INSERT only. No UPDATE, no DELETE.
    -- This is the actual enforcement mechanism for FR-10's
    -- "immutable, append-only" requirement — not a promise in
    -- application code, a permission the database itself denies.
    GRANT SELECT, INSERT ON audit_log TO phoenix_app;

    -- BIGSERIAL columns need nextval() on their backing sequence —
    -- without this grant, INSERT would fail even with table INSERT rights.
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO phoenix_app;
EOSQL
