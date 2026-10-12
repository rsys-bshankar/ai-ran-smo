#!/usr/bin/env bash
# PR-OPS-5.2: the previous release's code runs on this commit's schema.
#
#   scripts/check_previous_release_code.sh [previous-tag]
#
# The rule (CLAUDE.md, "Schema changes"): a schema revision is additive, so the release before it keeps working on the upgraded database while
# a rolling upgrade is half done. This proves it: build the previous release (newest `smo-v*` tag, or the one given) from its own source,
# start its compose stack on a database migrated to THIS commit's head by THIS commit's scripts/migrate.py (the previous release's own
# `migrate` service is replaced by a no-op: it does not know the newer revisions), and replay the previous release's own demo runbook
# against it. Then, as a control, break the schema on purpose and check the same replay fails: a check that cannot fail proves nothing.
#
# Needs docker and, on the host, the runtime dependencies (for migrate.py). Leaves nothing behind (the stack and the worktree are removed).
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
repo="$(git -C "$here" rev-parse --show-toplevel)"
# the newest final release: a release candidate (`smo-vX.Y.Z-rc.N`) is not what a rolling upgrade starts from
tag="${1:-$(git -C "$repo" tag --list 'smo-v*' --sort=-version:refname | grep -v -- '-rc\.' | head -1 || true)}"
if [ -z "$tag" ]; then
  # CI sets REQUIRE_PREVIOUS_RELEASE: once a release exists, "no tag found" means the checkout lacks the tags, and a silent skip would be a false green
  if [ -n "${REQUIRE_PREVIOUS_RELEASE:-}" ]; then echo "no release tag found, and one is required (does the checkout fetch tags?)" >&2; exit 1; fi
  echo "no release tag to test against: skipped"; exit 0
fi
rel="${here#"$repo"/}"
scratch="$(mktemp -d)"
prev_smo="$scratch/prev/$rel"
project="prevrel"
compose() { (cd "$prev_smo" && GUI_COOKIE_SECURE=false docker compose -p "$project" -f docker-compose.yml -f docker-compose.skipmigrate.yml "$@"); }
# Cleanup must never decide the result: the replay container runs as root and leaves root-owned files (egg-info, bytecode) in the worktree it mounts,
# which a plain `rm` cannot remove. On a CI runner the directory is thrown away anyway; elsewhere sudo (if there is any) finishes the job.
cleanup() {
  status=$?
  # a stack that did not come up says why only in its own logs, which the teardown below throws away: print them first
  if [ "$status" -ne 0 ] && [ -d "$prev_smo" ]; then
    echo "== the stack at failure"
    compose ps -a --format '{{.Service}}: {{.State}} {{.Health}}' 2>&1 | grep -v ' healthy$' || true
    for svc in $(compose ps -a --format '{{.Service}} {{.Health}}' 2>/dev/null | awk '$2 == "unhealthy" {print $1}'); do
      echo "== logs of $svc"; compose logs --no-color --tail=60 "$svc" 2>&1 || true
    done
  fi
  [ -d "$prev_smo" ] && { compose down -v --remove-orphans >/dev/null 2>&1 || true; }
  git -C "$repo" worktree remove --force "$scratch/prev" >/dev/null 2>&1 || true
  rm -rf "$scratch" >/dev/null 2>&1 || sudo rm -rf "$scratch" >/dev/null 2>&1 || true
  git -C "$repo" worktree prune >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

git -C "$repo" worktree add --detach "$scratch/prev" "refs/tags/$tag" >/dev/null
echo "== previous release: $tag ($(git -C "$repo" rev-parse --short "refs/tags/$tag")); this commit: $(git -C "$repo" rev-parse --short HEAD)"
cd "$prev_smo"
scripts/init_secrets.sh >/dev/null
cp .env.example .env
cat > docker-compose.skipmigrate.yml <<'Y'
# the schema comes from the newer commit; the old release's migrate would not know its revisions
services:
  migrate:
    command: ["true"]
Y

# fresh_stack: throw away any earlier run's stack, start only Postgres, migrate it to THIS commit's head with this commit's scripts/migrate.py (and run the
# models check), then make the per-module database roles with the previous release's own script. The previous release's modules are started afterwards by the caller.
fresh_stack() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  compose up -d --wait postgres
  url="postgresql+psycopg://smo:$(cat secrets/db_password)@localhost:5432/smo"
  (cd "$here" && SMO_DATABASE_URL="$url" python scripts/migrate.py)
  (cd "$here" && SMO_DATABASE_URL="$url" python scripts/check_migration_matches_models.py)
  # the previous release's own `migrate` service also makes the per-module roles its modules connect as (smo_<module>, since 0.4.0); it is replaced by a
  # no-op here, so make them the way it would: its own script and manifest, its own password files. Without them every module fails to log in.
  (cd "$prev_smo" && SMO_DATABASE_URL="$url" SMO_DB_ROLE_PASSWORD_DIR="$prev_smo/secrets" python scripts/db_roles.py)
}
# replay PYTEST_TARGET: run the previous release's own tests (its checkout, mounted at /work) in a Python container on the stack's network, as the demo consumer,
# against the live stack (SMO_E2E_LIVE=1). Exit status is pytest's.
replay() {
  docker run --rm --network "${project}_default" --network-alias demo-consumer \
    -e SMO_E2E_LIVE=1 -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work/smo/shared:/work/smo/sdk \
    -v "$scratch/prev:/work" -w /work/smo "${SMO_IMAGE_PYTHON_3_11_SLIM:-python:3.11-slim@sha256:9f6ef439f51f4b36dc5c6bb265c2d3dd810bde94c5bbec77ae86b6650c488cf9}" sh -c "
      pip install -q --require-hashes -r requirements/dev.txt &&
      pip install -q --no-deps -e shared &&
      python -m pytest $1 -v -p no:cacheprovider"
}

# The control comes first, on a stack that is thrown away: a replay changes the stack's data (it onboards packages, deploys instances), so a
# second replay on the same stack is not a fair test. Break one column the previous release needs, replay the first runbook test, expect it to fail.
echo "== control: a schema that breaks the previous release must make the replay fail"
fresh_stack
# the module tables live in schemas of their own since 0.4.0 (public holds nothing of them since revision 0029; the compatibility views the modules never read since 0.4.0 are gone): break the real table
compose exec -T postgres psql -U smo -d smo -v ON_ERROR_STOP=1 -c "ALTER TABLE ran_nf_oam.write_config_job RENAME COLUMN msac_role TO msac_role_broken" >/dev/null
compose up -d --build --wait
if replay "tests_integration/test_demo_runbook.py::test_full_runbook_sequence_succeeds" >/dev/null 2>&1; then
  echo "FAIL: the replay passed on a schema with a column the previous release needs renamed: this check cannot fail" >&2
  exit 1
fi
echo "ok: the replay fails on a breaking schema"

echo "== the real check: this commit's head schema (this commit's scripts/migrate.py), the previous release's code built from its own source"
fresh_stack
compose up -d --build --wait
compose exec -T r1-termination python3 - < scripts/compose_e2e.py
replay tests_integration/test_demo_runbook.py
echo "OK: $tag runs on this commit's schema, and the check fails when it should"
