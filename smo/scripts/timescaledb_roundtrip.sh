#!/usr/bin/env bash
# The TimescaleDB round trip (D8, plan task S1.10): the stack's Postgres has the timescaledb extension (created by the `migrate` service), and a hypertable
# takes rows and gives them back. Run from smo/ against the compose stack (the CI job "Store profiles round trip" does):
#
#   scripts/timescaledb_roundtrip.sh
#
# It makes a scratch schema, a hypertable partitioned by day, 72 hourly rows (three chunks), reads them back with time_bucket and checks the sums, then drops
# the schema. The common_dme_telemetry hypertables themselves are S1.9's revisions, not this script's. Uses the database's bootstrap role `smo`, as migrate does.
set -euo pipefail

psql() { docker compose exec -T postgres psql -U smo -d smo -v ON_ERROR_STOP=1 -At "$@"; }

version=$(psql -c "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
[ -n "$version" ] || { echo "FAIL: the timescaledb extension is not created in database smo" >&2; exit 1; }
echo "timescaledb $version is installed"

schema="ts_roundtrip_$$"
trap 'psql -c "DROP SCHEMA IF EXISTS $schema CASCADE" > /dev/null' EXIT
psql > /dev/null <<SQL
CREATE SCHEMA $schema;
CREATE TABLE $schema.sample (at TIMESTAMPTZ NOT NULL, cell TEXT NOT NULL, value DOUBLE PRECISION NOT NULL);
SELECT create_hypertable('$schema.sample', by_range('at', INTERVAL '1 day'));
INSERT INTO $schema.sample SELECT TIMESTAMPTZ '2026-11-01 00:00:00+00' + make_interval(hours => h), 'cell-' || (h % 3), h FROM generate_series(0, 71) AS h;
SQL
chunks=$(psql -c "SELECT count(*) FROM show_chunks('$schema.sample')")
rows=$(psql -c "SELECT count(*), sum(value) FROM $schema.sample")
days=$(psql -c "SELECT string_agg(to_char(day, 'YYYY-MM-DD') || '=' || n, ',' ORDER BY day) FROM (SELECT time_bucket(INTERVAL '1 day', at) AS day, count(*) AS n FROM $schema.sample GROUP BY 1) d")
echo "hypertable: $chunks chunks; rows|sum: $rows; per day: $days"
[ "$chunks" = 3 ] || { echo "FAIL: wanted 3 chunks, got $chunks" >&2; exit 1; }
[ "$rows" = "72|2556" ] || { echo "FAIL: wanted 72 rows summing to 2556, got $rows" >&2; exit 1; }
[ "$days" = "2026-11-01=24,2026-11-02=24,2026-11-03=24" ] || { echo "FAIL: time_bucket gave $days" >&2; exit 1; }
echo "TimescaleDB round trip: ok"
