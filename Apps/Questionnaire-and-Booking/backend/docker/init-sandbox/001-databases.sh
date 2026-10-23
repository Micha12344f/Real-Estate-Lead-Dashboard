#!/bin/sh
# Executed only for a new sandbox volume. Reuse the production schema sources.
set -eu
for domain in enquiries responses tokens settings; do
    psql --username "$POSTGRES_USER" --dbname sandbox_contacts -v ON_ERROR_STOP=1 \
        -c "CREATE DATABASE sandbox_${domain};"
done
for domain in contacts enquiries responses tokens settings; do
    for schema in /schemas/$domain/*.sql; do
        psql --username "$POSTGRES_USER" --dbname "sandbox_${domain}" -v ON_ERROR_STOP=1 -f "$schema"
    done
done
psql --username "$POSTGRES_USER" --dbname sandbox_responses -v ON_ERROR_STOP=1 -f /schemas/submissions.sql
