#!/usr/bin/env bash
# PR-V-2c: a mutation-testing pilot on the small, security-relevant parts of the shared library.
#
#   scripts/mutation_pilot.sh
#
# mutmut changes the source one small step at a time (a `>` becomes `>=`, a `*` becomes `/`, a condition becomes false) and runs the tests against each change; a
# change no test notices ("survived", or "no tests": nothing runs that code) is a place where a bug could be introduced and every test would still pass. Coverage says
# a line ran; this says whether a test would notice if it were wrong.
#
# Scope (smo/shared/pyproject.toml, [tool.mutmut] only_mutate): 17 modules of the shared library, chosen because a silent change in them is a security or integrity defect:
# pagination, ratelimit, timeutil, roles (the gateway's role policy and enrollment-secret check), webhook (the SSRF guard), killswitch, invoker, bodylimit, secretfile,
# versioning, identity, correlation, audit (the hash chain), idempotency, security_headers, errors and scope (tenant and region authorization). Tested by shared/tests and the
# gateway's tests/test_roles.py and tests/test_scope.py.
# Fails unless every mutant is killed. First run (October 2026): 131 mutants in four modules, 49 killed. Widened in two steps to 1560 mutants in 16 modules: the first
# eight added modules gave 822 mutants with 73 survivors; adding audit, idempotency, security_headers and errors gave 1604 with 251 survivors. The tests added to kill them are
# shared/tests/test_mutation_survivors.py, test_idempotency_exact.py, test_errors_exact.py and test_audit_exact.py.
# PR-SEC-10 added `scope` (1859 mutants in all, none surviving: the one `no tests` mutant of the first run was `request_scope`, which only a module called, now tested in shared).
# Besides missing tests it found: a CHECK-violation message could override a known SQLSTATE (errors.py), and several mutants that were equivalent to the original (a default
# argument no code path read, a cast that does nothing at run time, a dict key a later key overwrote, a falsy `None` where `False` was meant): the code was changed to not have
# them rather than a mutant being excused. The audit command line's help text is held in module constants, which mutmut does not mutate, and its presence is tested.
#
# PR-CONV-S1.4 added the ProvMnS router `mns/` (dn, errors, patch, scope, registry, service, router; tested by shared/tests/test_mns_*.py on SQLite: the Postgres runs
# of those tests are skipped here). `mns/store.py` (DDL, held by test_mns_store.py on Postgres) and `mns/lifecycle.py` (no-op hooks) are not mutated.
# With it: 4799 mutants in 24 modules, none surviving (2491 in mns/; the first run left 560, killed by shared/tests/test_mns_exact.py and by simplifying
# the code where a mutant behaved like the original).
# PR-CONV-S1.5 and S1.3 added the router's cross-cutting checks (`mns/checks.py`), the shared MSAC decision (`msac.py`) and the dictionary (`mns/dictionary/`), and
# PR-CONV-S1.6 the reference module Echo (`echo/app`, tested by echo/tests on SQLite; its own configuration is echo/pyproject.toml), run after the shared library.
# Needs the dev requirements installed (requirements/dev.txt) and mutmut (requirements/mutation.txt). About four minutes.
set -euo pipefail
cd "$(dirname "$0")/../shared"
rm -rf mutants
export PYTHONPATH=".:../r1-termination"
mutmut run > /tmp/mutmut-run.txt 2>&1 || { tr '\r' '\n' < /tmp/mutmut-run.txt | tail -20; echo "mutmut did not finish" >&2; exit 1; }
tr '\r' '\n' < /tmp/mutmut-run.txt | grep -E "^[^ ]* *[0-9]+/[0-9]+ " | tail -1 || true
not_killed="$(mutmut results 2>&1 | tr '\r' '\n' | sed 's/\x1b\[[0-9;]*m//g' | grep -E ': (survived|no tests|timeout|suspicious|segfault|check was interrupted by user)$' || true)"
if [ -n "$not_killed" ]; then
  echo "$not_killed"
  echo "FAIL: the mutants above were not killed (inspect one with: cd smo/shared && PYTHONPATH=.:../r1-termination mutmut show <name>)" >&2
  exit 1
fi

# Echo (PR-CONV-S1.6): mutmut runs its tests from echo/mutants, so the shared library is named by its absolute path.
cd ../echo
rm -rf mutants
PYTHONPATH=".:$(cd ../shared && pwd)" mutmut run > /tmp/mutmut-echo.txt 2>&1 || { tr '\r' '\n' < /tmp/mutmut-echo.txt | tail -20; echo "mutmut did not finish on echo" >&2; exit 1; }
tr '\r' '\n' < /tmp/mutmut-echo.txt | grep -E "^[^ ]* *[0-9]+/[0-9]+ " | tail -1 || true
not_killed="$(mutmut results 2>&1 | tr '\r' '\n' | sed 's/\x1b\[[0-9;]*m//g' | grep -E ': (survived|no tests|timeout|suspicious|segfault|check was interrupted by user)$' || true)"
if [ -n "$not_killed" ]; then
  echo "$not_killed"
  echo "FAIL: the Echo mutants above were not killed (inspect one with: cd smo/echo && PYTHONPATH=.:\$PWD/../shared mutmut show <name>)" >&2
  exit 1
fi
echo "OK: every mutant was killed"
