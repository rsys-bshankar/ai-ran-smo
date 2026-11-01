#!/usr/bin/env bash
# Creates the secret files the compose stack mounts (PR-SEC-4.3). Run it once before `docker compose up`.
#
#   scripts/init_secrets.sh
#
# smo/secrets/enrollment_secret  what every SMO module presents to SME when it registers, so SME can tell it from an rApp (PR-SEC-14);
#                           mounted by every module except the sample rApps, never given to an rApp
# smo/secrets/db_password   the Postgres password: a random 48-hex-character value, never printed. An existing
#                           file is left alone, so running this again does not lock the stack out of its own
#                           database. To rotate it, see docs/SECRETS.md.
#
# smo/secrets/db_password_onboarding
#                           the password of the database role smo_onboarding (PR-DB-2.6), which Onboarding connects as: it can use its own schema and
#                           nothing else. scripts/db_roles.py (run by the `migrate` service) sets it in the database; a file that is missing means
#                           no role is made. One file per module that has a role (`migrations/db_roles.json`), as modules adopt them.
#
# The directory is owner-only (0700) and git-ignored. The file itself is 0644: Compose bind-mounts it as it is,
# and the services run as uid 10001, not as you, so it has to be readable by other users; the directory is what
# keeps other accounts on this machine out. Postgres and every module read it at /run/secrets/db_password
# (POSTGRES_PASSWORD_FILE, SMO_DATABASE_PASSWORD_FILE): neither the compose file nor any container's
# environment carries the password.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dir="$here/secrets"

umask 077
mkdir -p "$dir"
chmod 700 "$dir"
# create_secret NAME: a random 48-hex-character value in $dir/NAME, 0644, kept if it exists
create_secret() {
  local path="$dir/$1"
  if [ -s "$path" ]; then
    echo "kept: $path (already exists)"
    return
  fi
  python3 -c 'import secrets; print(secrets.token_hex(24), end="")' > "$path"
  chmod 644 "$path"
  echo "created: $path"
}
create_secret db_password
create_secret objectstore_access_key     # the object store's S3 keys (profiles objectstore and minio, D10): smo_shared.objectstore reads them as files
create_secret objectstore_secret_key
create_secret db_password_onboarding
create_secret db_password_ran-analytics
create_secret db_password_so-smos
create_secret db_password_sa-smos
create_secret db_password_intent-service
create_secret db_password_mdaf
create_secret db_password_energy-saving-rapp
create_secret db_password_mobility-optimization-rapp
create_secret db_password_coverage-optimization-rapp
create_secret db_password_traffic-steering-rapp
create_secret db_password_aimgf
create_secret db_password_dme
create_secret db_password_focom
create_secret db_password_nfo
create_secret db_password_r1-termination
create_secret db_password_ran-nf-oam
create_secret db_password_rapp-mgmt
create_secret db_password_sme
create_secret enrollment_secret
