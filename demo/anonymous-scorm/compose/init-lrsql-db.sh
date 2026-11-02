#!/bin/bash
# Creates the lrsql database next to the LMS database in the same instance
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER lrsql_user WITH PASSWORD 'lrsql_password';
    CREATE DATABASE lrsql_db OWNER lrsql_user;
EOSQL
