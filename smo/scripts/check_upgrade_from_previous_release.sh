#!/usr/bin/env bash
# PR-OPS-10.5: upgrade a running previous release to this commit, on the same database.
#
#   scripts/check_upgrade_from_previous_release.sh [previous-tag]
#
# `check_previous_release_code.sh` (OPS-5.2) proves the OLD code runs on the NEW schema. This is the other half, the one an operator does: install the
# previous release (newest final `smo-v*` tag, built from its own source, its own `migrate` service), put real data into it with its own demo runbook,
# stop it, and start THIS commit's stack on the same database volume, so this commit's `migrate` service upgrades data that exists, not an empty schema.
# Then it checks that the data is still there and that the upgraded stack answers.
#
# The control comes first: a migration that passes on an empty database and fails on real data (a NOT NULL column with no default on a table that has
# rows) is seeded into a copy of the migrations, and the upgrade of the populated database must refuse it and leave the database where it was. A check
# that cannot fail proves nothing, and the fresh-database migration check is exactly the one that would pass this migration.
#
# Not done: the previous release's runbook is not replayed a second time on the upgraded database (it onboards a CSAR that must not already exist), and
# the data check is on what the runbook leaves (row counts per table, the packages and instances by id), not on every column.
#
# Needs docker and, on the host, the runtime dependencies (for migrate.py). Leaves nothing behind (the stack, its volumes and the worktree are removed).
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
repo="$(git -C "$here" rev-parse --show-toplevel)"
tag="${1:-$(git -C "$repo" tag --list 'smo-v*' --sort=-version:refname | grep -v -- '-rc\.' | head -1 || true)}"
if [ -z "$tag" ]; then
  if [ -n "${REQUIRE_PREVIOUS_RELEASE:-}" ]; then echo "no release tag found, and one is required (does the checkout fetch tags?)" >&2; exit 1; fi
  echo "no release tag to upgrade from: skipped"; exit 0
fi
rel="${here#"$repo"/}"
scratch="$(mktemp -d)"
prev_smo="$scratch/prev/$rel"
project="upgrade"
made_env=""
compose_prev() { (cd "$prev_smo" && GUI_COOKIE_SECURE=false docker compose -p "$project" "$@"); }
# This commit's stack takes its base images from the GHCR mirror when CI has set it up (scripts/ci_mirror.py setup wrote the override; docs/MIRROR.md). The previous
# release's stack does not: it builds and pulls its own older pins, which the mirror does not hold, so its images come from wherever its files say.
compose_new() {
  local files=()
  [ -f "$here/.ci-mirror/docker-compose.mirror.yml" ] && files=(-f docker-compose.yml -f .ci-mirror/docker-compose.mirror.yml)
  (cd "$here" && GUI_COOKIE_SECURE=false docker compose -p "$project" "${files[@]}" "$@")
}
# cleanup (EXIT trap): tear down both stacks and their volumes, remove the previous release's worktree and the scratch directory, and delete the .env this script made;
# the exit status of the run is kept. Teardown failures are ignored on purpose: cleanup must never decide the result.
cleanup() {
  status=$?
  compose_new down -v --remove-orphans >/dev/null 2>&1 || true
  [ -d "$prev_smo" ] && { compose_prev down -v --remove-orphans >/dev/null 2>&1 || true; }
  git -C "$repo" worktree remove --force "$scratch/prev" >/dev/null 2>&1 || true
  rm -rf "$scratch" >/dev/null 2>&1 || sudo rm -rf "$scratch" >/dev/null 2>&1 || true
  git -C "$repo" worktree prune >/dev/null 2>&1 || true
  [ -n "$made_env" ] && rm -f "$here/.env"
  exit "$status"
}
trap cleanup EXIT

git -C "$repo" worktree add --detach "$scratch/prev" "refs/tags/$tag" >/dev/null
echo "== upgrade from $tag ($(git -C "$repo" rev-parse --short "refs/tags/$tag")) to this commit ($(git -C "$repo" rev-parse --short HEAD))"

# one set of secrets for both stacks: they share the database volume, and the previous release mounts whichever of these files it declares
# (0.1.0 only db_password; 0.2.0 also enrollment_secret). A file it does not declare is simply not mounted.
(cd "$here" && scripts/init_secrets.sh >/dev/null)
# the previous release's own init first (it may declare a secret this commit no longer has, e.g. 0.4.0's db_password_a1-related), then ours over it
(cd "$prev_smo" && scripts/init_secrets.sh >/dev/null)
mkdir -p "$prev_smo/secrets" && cp "$here"/secrets/* "$prev_smo/secrets/"
[ -f "$here/.env" ] || { cp "$here/.env.example" "$here/.env"; made_env=1; }
cp "$prev_smo/.env.example" "$prev_smo/.env"
url="postgresql+psycopg://smo:$(cat "$here/secrets/db_password")@localhost:5432/smo"
host_migrate() { (cd "$1" && SMO_DATABASE_URL="$url" python scripts/migrate.py "${@:2}"); }
psql_prev() { compose_prev exec -T postgres psql -U smo -d smo -v ON_ERROR_STOP=1 -At "$@"; }
psql_new() { compose_new exec -T postgres psql -U smo -d smo -v ON_ERROR_STOP=1 -At "$@"; }

# row count of every table (in whatever schema: a module's tables move into a schema of its own, PR-DB-2.5; names are unique across schemas), and the ids of the
# packages and instances (named with their schemas: since 0029 `public` holds no view of a module's table, and before it an ALTER TABLE of the view refused for the wrong reason)
snapshot() {
  "$1" -c "SELECT table_name || '|' || (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text
           FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema') AND table_type = 'BASE TABLE' ORDER BY 1"
  "$1" -c "SELECT 'package|' || package_id FROM onboarding.application_package ORDER BY 1"
  "$1" -c "SELECT 'instance|' || instance_id FROM rapp_mgmt.rapp_instance ORDER BY 1"
}

echo "== install the previous release and put data into it (its own runbook)"
compose_prev up -d --build --wait
docker run --rm --network "${project}_default" --network-alias demo-consumer \
  -e SMO_E2E_LIVE=1 -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work/smo/shared:/work/smo/sdk \
  -v "$scratch/prev:/work" -w /work/smo "${SMO_IMAGE_PYTHON_3_11_SLIM:-python:3.11-slim@sha256:9f6ef439f51f4b36dc5c6bb265c2d3dd810bde94c5bbec77ae86b6650c488cf9}" sh -c "
    pip install -q --require-hashes -r requirements/dev.txt &&
    pip install -q --no-deps -e shared &&
    python -m pytest tests_integration/test_demo_runbook.py::test_full_runbook_sequence_succeeds -v -p no:cacheprovider"
before="$scratch/before.txt"
snapshot psql_prev > "$before"
revision_before="$(host_migrate "$here" --current | tail -1)"
echo "previous release's schema revision: $revision_before"
grep -q '^application_package|[1-9]' "$before" || { echo "FAIL: the previous release's runbook left no package behind: this check would prove nothing" >&2; exit 1; }
grep -c '' "$before" | sed 's/^/rows in the snapshot: /'

echo "== stop the previous release (its data volume stays)"
compose_prev down --remove-orphans
compose_new up -d --wait postgres

echo "== control: a migration that breaks on real data must be refused, and the database left where it was"
seeded="$scratch/seeded"
mkdir -p "$seeded" && cp -r "$here/migrations" "$here/alembic.ini" "$here/scripts" "$here/shared" "$seeded/"
head_rev="$(cd "$here" && python - <<'PY'
from alembic.config import Config
from alembic.script import ScriptDirectory
config = Config("alembic.ini")
config.set_main_option("script_location", "migrations")
print(repr(tuple(sorted(ScriptDirectory.from_config(config).get_heads()))))   # every branch head: the seeded revision follows them all
PY
)"
cat > "$seeded/migrations/versions/9999_seeded_break.py" <<PY
"""Seeded: fine on an empty database, impossible on one with packages in it."""
from alembic import op

revision = "9999"
down_revision = $head_rev
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE onboarding.application_package ADD COLUMN seeded_break VARCHAR NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE onboarding.application_package DROP COLUMN seeded_break")
PY
if host_migrate "$seeded" >/dev/null 2>&1; then
  echo "FAIL: a migration that cannot run on a populated database was accepted: this check cannot fail" >&2
  exit 1
fi
[ "$(host_migrate "$here" --current | tail -1)" = "$revision_before" ] || { echo "FAIL: the refused migration changed the schema revision" >&2; exit 1; }
echo "ok: the breaking migration was refused and the schema is unchanged"

echo "== the real upgrade: this commit's stack, its migrate service, the same database"
compose_new up -d --build --wait
echo "schema revision now: $(host_migrate "$here" --current | tail -1)"
(cd "$here" && SMO_DATABASE_URL="$url" python scripts/check_migration_matches_models.py)
compose_new exec -T r1-termination python3 - < "$here/scripts/compose_e2e.py"
compose_new exec -T sme python3 - < "$here/scripts/compose_e2e_roles.py"       # PR-SEC-14: the roles work on the upgraded stack

echo "== the data survived"
after="$scratch/after.txt"
snapshot psql_new > "$after"
# tables whose rows come and go by themselves (the outbox drains, idempotency keys expire)
volatile='^(notification_outbox|idempotency_key)\|'
lost=0
while IFS='|' read -r name count; do
  case "$name" in package|instance) continue ;; esac
  echo "$name|$count" | grep -Eq "$volatile" && continue
  now="$(grep -E "^$name\|" "$after" | head -1 | cut -d'|' -f2 || true)"   # a table that is gone is reported below, not by set -e
  if [ -z "$now" ] || [ "$now" -lt "$count" ]; then echo "LOST: $name had $count rows, now ${now:-no table}" >&2; lost=1; fi
done < <(grep -Ev '^(package|instance)\|' "$before")
diff <(grep -E '^(package|instance)\|' "$before") <(grep -E '^(package|instance)\|' "$after") || { echo "LOST: the packages or instances differ" >&2; lost=1; }
[ "$lost" = 0 ] || exit 1
echo "OK: $tag upgraded to this commit with its data, and the check fails when it should"
