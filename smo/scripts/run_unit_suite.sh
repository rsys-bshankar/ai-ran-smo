#!/usr/bin/env bash
# Runs one unit suite under coverage and leaves smo/coverage/<name>.json (PR-V-1). Used by the CI job `unit-and-integration`; run it the same way locally:
#
#   scripts/run_unit_suite.sh onboarding onboarding app .:../shared
#
#   NAME        the suite (its floor is `tests.floor` in the module's module.yaml)
#   DIR         the directory whose tests/ is run (relative to smo/)
#   SOURCE      what coverage measures, relative to DIR (`app`, `smo_shared`, `smo_sdk`)
#   PYTHONPATH  the PYTHONPATH of that suite
set -euo pipefail
name=$1 dir=$2 source=$3 pythonpath=$4
smo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$smo/coverage"
mkdir -p "$out"
echo "::group::$name"
(cd "$smo/$dir" && PYTHONPATH="$pythonpath" python -m coverage run --data-file="$out/.cov-$name" --source="$source" -m pytest tests/ -v \
  && python -m coverage json --data-file="$out/.cov-$name" -o "$out/$name.json" -q)
echo "::endgroup::"
