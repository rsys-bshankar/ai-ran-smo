#!/usr/bin/env bash
# Type check (PR-V-2b..2j) of every module: `scripts/run_mypy.sh [module...]` from smo/ (the `lint` job runs it with no argument).
# The same settings everywhere (check_untyped_defs: the bodies of functions without annotations are checked too, which is where the findings are;
# a package without type information is Any). A new module is added to MODULES in the PR that adds it. A sample rApp is `samples/<name>` (its `app` package, with the
# SDK and the shared library on the path, as its tests have them). Not covered: tests_integration (103 findings in 23 files at the last count, mostly the
# importlib `spec.loader` idiom and untyped test doubles; the sample rApps' and modules' tests are not checked either, only their `app`).
set -euo pipefail
cd "$(dirname "$0")/.."
MODULES=(onboarding rapp-mgmt mlmr mllf so-smos sme dme r1-termination nfo ran-analytics mdaf sa-smos mock-o1-adaptor focom gui-bff aimgf intent-service ran-nf-oam sdk echo
         samples/energy-saving-rapp samples/mobility-optimization-rapp samples/coverage-optimization-rapp samples/traffic-steering-rapp)
[ "$#" -gt 0 ] && MODULES=("$@")
status=0
for m in "${MODULES[@]}"; do
  echo "== $m"
  target=app; [ -d "$m/app" ] || target=.      # the SDK is a package at the module root, tests included
  pythonpath=.:../shared; case "$m" in samples/*) pythonpath=.:../../shared:../../sdk ;; esac
  (cd "$m" && PYTHONPATH="$pythonpath" python -m mypy --python-version 3.11 --check-untyped-defs --ignore-missing-imports --disable-error-code import-untyped --warn-unused-ignores --warn-redundant-casts \
      --explicit-package-bases "$target") || status=1
done
exit $status
