#!/bin/sh
# One upstream database per app server, built identically, before zero-cache's first sync.
set -e
for db in e2e_ts e2e_py; do
  psql -v ON_ERROR_STOP=1 -U postgres -c "CREATE DATABASE $db"
  psql -v ON_ERROR_STOP=1 -U postgres -d "$db" -f /schema.sql
done
