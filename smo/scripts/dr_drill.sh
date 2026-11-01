#!/usr/bin/env bash
# Disaster-recovery drill (PR-HA-6.3): restores the newest off-site backup into a FRESH database on a Postgres you give it, checks the schema,
# runs a smoke check, and prints measured timings and the data-loss window so they can be compared with the targets (RPO 15 minutes,
# RTO 1 hour; docs/DISASTER_RECOVERY.md). Exits 1 when a check fails, the RTO is exceeded, or the data-loss window is.
#
#   scripts/dr_drill.sh --admin-url URL [--set STAMP] [--rto-seconds 3600] [--rpo-seconds 900]
#                       [--probe TABLE:TIMESTAMP_COLUMN --high-water ISO8601] [--report FILE.json] [--keep]
#
#   --admin-url   SQLAlchemy URL (postgresql+psycopg://user:password@host:port/postgres) of the Postgres to restore into; the role needs CREATEDB.
#                 The drill creates a database smo_drill_<UTC> there and drops it at the end (--keep leaves it). Never point it at production.
#   --set         restore this set instead of the newest.
#   --probe / --high-water   the data-loss check: TABLE:COLUMN is a timestamp column that every write stamps; ISO8601 is the time of the last
#                 write the lost database acknowledged. The window is that time minus the newest value found in the restored database
#                 (an empty probe table counts as the whole age of the backup). Without them the window reported is the age of the backup at
#                 the start of the drill, which is only what a failure at that moment would lose.
#   --report      writes the timings and verdicts as JSON.
#
# Needs: psql, pg_restore (at least the server's major version), python3 with the SMO's requirements (scripts/migrate.py and
# check_migration_matches_models.py run against the restored database), the AWS CLI, and the bucket variables of dr_s3.sh.
# The RTO printed is the time of this script: fetch, restore, schema checks, smoke. A real recovery adds the time to notice, decide and provision
# a Postgres and re-point the services (the runbook's steps 1 and 2 and 8 to 10, docs/DISASTER_RECOVERY.md), which the drill cannot measure.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
smo_root="$(cd "$here/.." && pwd)"
# shellcheck source=dr_s3.sh
. "$here/dr_s3.sh"
admin_url=""; set_name=""; rto=3600; rpo=900; probe=""; high_water=""; report=""; keep=no
while [ $# -gt 0 ]; do
  case "$1" in
    --admin-url) admin_url="${2:?}"; shift ;;
    --set) set_name="${2:?}"; shift ;;
    --rto-seconds) rto="${2:?}"; shift ;;
    --rpo-seconds) rpo="${2:?}"; shift ;;
    --probe) probe="${2:?}"; shift ;;
    --high-water) high_water="${2:?}"; shift ;;
    --report) report="${2:?}"; shift ;;
    --keep) keep=yes ;;
    -h|--help) sed -n '2,21p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$admin_url" ] || { echo "--admin-url is required (a fresh Postgres to restore into)" >&2; exit 2; }
{ [ -z "$probe" ] && [ -z "$high_water" ]; } || { [ -n "$probe" ] && [ -n "$high_water" ]; } || { echo "--probe and --high-water go together" >&2; exit 2; }

now() { date +%s.%N; }
elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.1f", b - a }'; }
python="${PYTHON:-python3}"
work="$(mktemp -d)"
drill_db="smo_drill_$(date -u +%Y%m%dT%H%M%S)"
# cleanup (EXIT trap): drop the drill database (unless --keep) and remove the work directory. A failure to drop is ignored.
cleanup() {
  if [ "$keep" != yes ]; then
    ( SMO_DATABASE_URL="$admin_url"; export SMO_DATABASE_URL; . "$here/pg_env.sh"; smo_pg_env
      psql -d postgres -qc "DROP DATABASE IF EXISTS \"$drill_db\" WITH (FORCE)" > /dev/null 2>&1 || true )
  fi
  rm -rf "$work"
}
trap cleanup EXIT

# url_for_db NAME: the admin URL with its database part replaced by NAME (the drill database).
url_for_db() {
  ADMIN_URL="$admin_url" DB="$1" "$python" -c '
import os
from urllib.parse import urlsplit, urlunsplit
parts = urlsplit(os.environ["ADMIN_URL"])
print(urlunsplit(parts._replace(path="/" + os.environ["DB"])))'
}
drill_url="$(url_for_db "$drill_db")"
sql() { ( SMO_DATABASE_URL="$drill_url"; export SMO_DATABASE_URL; . "$here/pg_env.sh"; smo_pg_env; psql -v ON_ERROR_STOP=1 -Atq "$@" ); }
fail=0
verdict() { # name ok detail
  if [ "$2" = ok ]; then printf '  PASS  %-34s %s\n' "$1" "$3"; else printf '  FAIL  %-34s %s\n' "$1" "$3"; fail=1; fi
}

t0="$(now)"
echo "disaster-recovery drill: restoring ${set_name:-the newest off-site set} into database $drill_db"

# 1. fetch the set from the bucket and check it against its manifest
fetched="$("$here/dr_fetch.sh" ${set_name:+--set "$set_name"} "$work/set" | tail -1)"
t1="$(now)"
read_manifest() { "$python" -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$work/set/manifest.json" "$1"; }
created="$(read_manifest createdAt)"; want_revision="$(read_manifest schemaRevision)"; want_tables="$(read_manifest tableCount)"

# 2. a fresh database, and the dump restored into it (db_restore.sh: one transaction, no errors tolerated)
( SMO_DATABASE_URL="$admin_url"; export SMO_DATABASE_URL; . "$here/pg_env.sh"; smo_pg_env; psql -d postgres -v ON_ERROR_STOP=1 -qc "CREATE DATABASE \"$drill_db\"" )
SMO_DATABASE_URL="$drill_url" "$here/db_restore.sh" --yes "$work/set/smo.dump" > /dev/null
t2="$(now)"

# 3. schema: the revision and table count the manifest records; migrate to this checkout's head (a no-op for a backup of this release; the
#    forward upgrade for an older one) and compare every ORM model with the restored schema
got_revision="$(sql -c "SELECT string_agg(version_num, ',' ORDER BY version_num) FROM alembic_version")"
got_tables="$(sql -c "SELECT count(*) FROM information_schema.tables WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema')")"
[ "$got_revision" = "$want_revision" ] && verdict "schema revision" ok "$got_revision" || verdict "schema revision" bad "restored $got_revision, manifest $want_revision"
[ "$got_tables" = "$want_tables" ] && verdict "table count" ok "$got_tables" || verdict "table count" bad "restored $got_tables, manifest $want_tables"
schema_ok=ok
if ! ( cd "$smo_root" && SMO_DATABASE_URL="$drill_url" PYTHONPATH="$smo_root/shared${PYTHONPATH:+:$PYTHONPATH}" "$python" scripts/migrate.py \
        && SMO_DATABASE_URL="$drill_url" PYTHONPATH="$smo_root/shared${PYTHONPATH:+:$PYTHONPATH}" "$python" scripts/check_migration_matches_models.py ) > "$work/schema.log" 2>&1; then
  schema_ok=bad; tail -15 "$work/schema.log" >&2
fi
verdict "migrate + models check" "$schema_ok" "scripts/migrate.py, scripts/check_migration_matches_models.py"
t3="$(now)"

# 4. smoke: every table answers a query; the GUI database, when the set has one, passes its integrity check and has its users
smoke_ok=ok
sql -c "SELECT format('SELECT 1 FROM %I.%I LIMIT 1;', schemaname, tablename) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')" \
  | sql -f - > /dev/null 2>&1 || smoke_ok=bad
verdict "every table answers a query" "$smoke_ok" "$got_tables tables"
if [ -s "$work/set/gui-bff.db" ]; then
  gui="$("$python" - "$work/set/gui-bff.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
ok = db.execute("PRAGMA integrity_check").fetchone()[0]
users = db.execute("SELECT count(*) FROM gui_user").fetchone()[0] if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'gui_user'").fetchone() else 0
print(f"{ok} {users}")
PY
)" || gui="error 0"
  read -r gui_ok gui_users <<< "$gui"
  [ "$gui_ok" = ok ] && [ "$gui_users" -gt 0 ] && verdict "GUI database" ok "integrity ok, $gui_users user(s)" || verdict "GUI database" bad "integrity $gui_ok, $gui_users user(s)"
fi
t4="$(now)"

# 5. the data-loss window
if [ -n "$probe" ]; then
  ptable="${probe%%:*}"; pcol="${probe##*:}"
  loss="$(sql -c "SELECT COALESCE(extract(epoch FROM (timestamptz '$high_water' - max($pcol))), 1e9)::numeric(14,1) FROM $ptable")"
  loss_what="newest row in $ptable.$pcol is $loss s older than the last acknowledged write ($high_water)"
else
  loss="$("$python" -c '
import sys
from datetime import datetime, timezone
print(round((datetime.now(timezone.utc) - datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))).total_seconds(), 1))' "$created")"
  loss_what="age of the backup now (no --probe given)"
fi
rpo_ok=bad; awk -v l="$loss" -v r="$rpo" 'BEGIN { exit !(l <= r) }' && rpo_ok=ok
verdict "data-loss window <= ${rpo} s" "$rpo_ok" "${loss} s: $loss_what"

total="$(elapsed "$t0" "$t4")"
rto_ok=bad; awk -v t="$total" -v r="$rto" 'BEGIN { exit !(t <= r) }' && rto_ok=ok
verdict "recovery time <= ${rto} s" "$rto_ok" "${total} s"

echo
echo "timings (s): fetch+verify $(elapsed "$t0" "$t1"), restore $(elapsed "$t1" "$t2"), schema checks $(elapsed "$t2" "$t3"), smoke $(elapsed "$t3" "$t4"), total $total"
echo "set $fetched (created $created, schema $want_revision); targets RPO ${rpo} s, RTO ${rto} s; measured loss ${loss} s, measured RTO ${total} s"
if [ -n "$report" ]; then
  "$python" - "$report" "$fetched" "$created" "$want_revision" "$total" "$loss" "$rto" "$rpo" "$fail" "$(elapsed "$t0" "$t1")" "$(elapsed "$t1" "$t2")" "$(elapsed "$t2" "$t3")" "$(elapsed "$t3" "$t4")" <<'PY'
import json, sys
path, name, created, revision, total, loss, rto, rpo, fail, fetch, restore, schema, smoke = sys.argv[1:]
json.dump({"set": name, "createdAt": created, "schemaRevision": revision,
           "rtoSeconds": float(total), "rtoTargetSeconds": int(rto), "dataLossSeconds": float(loss), "rpoTargetSeconds": int(rpo),
           "phases": {"fetchVerify": float(fetch), "restore": float(restore), "schemaChecks": float(schema), "smoke": float(smoke)},
           "passed": fail == "0"}, open(path, "w"), indent=2)
PY
fi
[ "$fail" = 0 ] || { echo "DRILL FAILED" >&2; exit 1; }
echo "DRILL PASSED"
