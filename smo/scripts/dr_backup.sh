#!/usr/bin/env bash
# Off-site backup of the SMO databases to an S3-compatible bucket (PR-HA-6.2, PR-DB-6.5).
#
#   scripts/dr_backup.sh [--compose] [--loop SECONDS] [--no-prune]
#
# One run: db_backup.sh dumps Postgres (and, when SMO_BACKUP_GUI_DB names it, the GUI backend's SQLite database is copied with SQLite's online
# backup API), a manifest is written (UTC time, Postgres version, Alembic revision, size and SHA-256 of each file), the set is uploaded, every
# upload is checked by name and size, then latest.json is written and sets older than the retention are deleted. A run that fails at any step
# leaves latest.json pointing at the last complete set. Exit 0 only when the set is in the bucket.
#
#   (default)   dumps SMO_DATABASE_URL with the host's pg_dump. --compose runs inside the compose postgres container (as db_backup.sh).
#   --loop N    run again every N seconds, forever (the `db-backup` compose service); a failed run is logged and retried at the next tick, so
#               the recovery point objective is N plus the time one run takes.
#   --no-prune  skip retention.
#
#   SMO_BACKUP_RETENTION_DAYS    delete sets older than this many days (default 14) ...
#   SMO_BACKUP_KEEP_MIN          ... but always keep at least this many of the newest (default 5).
#   SMO_BACKUP_GUI_DB            path of the GUI backend's SQLite file (sqlite:////data/gui-bff.db in compose: /data/gui-bff.db); unset: not backed up.
#   SMO_BACKUP_STAGING           where the files are written before upload (default: a temporary directory; a database larger than /tmp allows needs a path here).
# The bucket variables are in dr_s3.sh. The files are mode 0600 and removed after the upload: they hold every table, including secrets.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=dr_s3.sh
. "$here/dr_s3.sh"
db_args=()
loop=""
prune=yes
while [ $# -gt 0 ]; do
  case "$1" in
    --compose) db_args+=(--compose) ;;
    --loop) loop="${2:?--loop needs a number of seconds}"; shift ;;
    --no-prune) prune=no ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# psql_value SQL: one query against the database being backed up (inside the compose postgres container with --compose, else with the host's psql and the
# libpq variables pg_env.sh derives from SMO_DATABASE_URL). SQL is script-internal, never caller input.
psql_value() {   # one value from the database being backed up
  if [ "${db_args[0]:-}" = --compose ]; then
    docker compose exec -T postgres sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"$1\""
  else
    ( . "$here/pg_env.sh"; smo_pg_env; psql -Atc "$1" )
  fi
}

# backup_once: one complete off-site backup, in order: dump (and the GUI database copy), manifest, upload, check every upload by name and size, move latest.json, prune.
# Returns non-zero at the first failing step, before latest.json moves, so latest.json always names a complete set. The staging directory is removed on return.
backup_once() {
  smo_s3_init
  umask 077
  local stamp work
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  if [ -n "${SMO_BACKUP_STAGING:-}" ]; then mkdir -p "$SMO_BACKUP_STAGING"; work="$(mktemp -d "$SMO_BACKUP_STAGING/set.XXXXXX")"; else work="$(mktemp -d)"; fi
  trap 'rm -rf "$work"' RETURN
  local started; started="$(date +%s)"

  "$here/db_backup.sh" ${db_args[@]+"${db_args[@]}"} "$work/smo.dump" > /dev/null
  local files=(smo.dump)
  if [ -n "${SMO_BACKUP_GUI_DB:-}" ]; then
    python3 - "$SMO_BACKUP_GUI_DB" "$work/gui-bff.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)     # the online backup API: a consistent copy while the BFF writes
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    sys.exit("the copy of the GUI database fails its integrity check")
PY
    files+=(gui-bff.db)
  fi

  local revision server tables
  revision="$(psql_value "SELECT string_agg(version_num, ',' ORDER BY version_num) FROM alembic_version")"
  server="$(psql_value 'SHOW server_version' | head -1)"
  tables="$(psql_value "SELECT count(*) FROM information_schema.tables WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema')" | head -1)"
  [ -n "$revision" ] || { echo "no alembic_version row: is this an SMO database at a revision?" >&2; return 1; }

  python3 - "$work" "$stamp" "$revision" "$server" "$tables" "$(( $(date +%s) - started ))" "${files[@]}" <<'PY'
import hashlib, json, os, sys
work, stamp, revision, server, tables, seconds, *names = sys.argv[1:]
files = []
for name in names:
    digest = hashlib.sha256()
    with open(os.path.join(work, name), "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    files.append({"name": name, "bytes": os.path.getsize(os.path.join(work, name)), "sha256": digest.hexdigest()})
manifest = {
    "format": 1,
    "set": stamp,
    "createdAt": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z",
    "postgresServerVersion": server,
    "schemaRevision": revision,
    "tableCount": int(tables),
    "backupSeconds": int(seconds),
    "files": files,
}
with open(os.path.join(work, "manifest.json"), "w") as handle:
    json.dump(manifest, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

  local name bytes
  for name in "${files[@]}"; do smo_s3_put "$work/$name" "$stamp/$name"; done
  smo_s3_put "$work/manifest.json" "$stamp/manifest.json"
  # the set is complete only when every file is there with the size the manifest records; only then does latest.json move
  python3 - "$work/manifest.json" > "$work/expected" <<'PY'
import json, sys
for f in json.load(open(sys.argv[1]))["files"]:
    print(f["name"], f["bytes"])
PY
  smo_s3 s3 ls "$S3_ROOT/$stamp/" > "$work/listing"
  while read -r name bytes; do
    awk -v n="$name" -v b="$bytes" '$4 == n && $3 == b { found = 1 } END { exit !found }' "$work/listing" \
      || { echo "upload check failed: $name is missing or has the wrong size in $S3_ROOT/$stamp/" >&2; return 1; }
  done < "$work/expected"
  smo_s3_put "$work/manifest.json" "latest.json"
  echo "off-site backup $stamp: ${files[*]} to $S3_ROOT/$stamp/ (schema $revision, $(( $(date +%s) - started )) s)"

  if [ "$prune" = yes ]; then
    local keep_days="${SMO_BACKUP_RETENTION_DAYS:-14}" keep_min="${SMO_BACKUP_KEEP_MIN:-5}"
    local cutoff; cutoff="$(date -u -d "-${keep_days} days" +%Y%m%dT%H%M%SZ)"
    local sets=() old=() i
    mapfile -t sets < <(smo_s3_sets)
    for (( i = 0; i < ${#sets[@]} - keep_min; i++ )); do
      if [[ "${sets[$i]}" < "$cutoff" ]]; then old+=("${sets[$i]}"); fi
    done
    for name in ${old[@]+"${old[@]}"}; do smo_s3_rm_set "$name"; echo "retention: deleted $name"; done
  fi
}

if [ -z "$loop" ]; then
  backup_once
else
  while true; do
    backup_once || echo "off-site backup FAILED at $(date -u +%FT%TZ); retrying in ${loop} s" >&2
    sleep "$loop"
  done
fi
