#!/bin/bash
set -e

# Same reasoning as 003_create_app_role.sh: a .sh file so PAYMENT_APP_PASSWORD
# gets substituted from the container's environment.
#
# payment_app is deliberately its own role, not phoenix_app reused. The agent
# investigating an incident and the application being investigated must not
# share credentials -- phoenix_app's grants are scoped to the incident audit
# trail, and payment_app's are scoped to charges. Neither role can touch the
# other's tables.

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE payment_app WITH LOGIN PASSWORD '$PAYMENT_APP_PASSWORD';

    GRANT CONNECT ON DATABASE $POSTGRES_DB TO payment_app;
    GRANT USAGE ON SCHEMA public TO payment_app;
    GRANT SELECT, INSERT ON charges TO payment_app;
EOSQL
