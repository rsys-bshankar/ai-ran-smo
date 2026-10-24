#!/usr/bin/env python3
"""The verdict of the load that runs through an upgrade (PR-V-10): `scripts/upgrade_load_verdict.py load-results.json [--max-error-rate 0.01] [--min-calls 2000] [--report-only]`.

The load (scripts/load_run.py, paced, started before the first `helm upgrade` and stopped after the last step of the lane) must have run the whole time and must have
been answered. The routes that restart with a gap (`--allow-errors-on`, default `package list`: onboarding keeps its package store on a volume only one pod may hold
and rolls with Recreate) are judged apart from the others (UPGRADE-GATE-EXEMPT):

- at least `--min-calls` calls in all, every route counted;
- the error rate of the routes that stay up (their errors over their calls, the exempt routes left out of both) is at most `--max-error-rate`, and none of them
  answers more than `--max-route-errors` errors;
- each exempt route's error rate, over its own calls, is at most `--max-allowed-route-error-rate` (default 25 %), so a gap passes and an outage does not.

The exempt routes are printed on a line of their own (errors, calls, rate, statuses), and the errors by time (the 10 s slices that had any), either way, so a
failing run says when it happened: the migration, a module's rollout, the rollback.

Exit status 0 when the load was served, 1 when it was not (or did not run).
"""

import argparse
import json
import sys


def verdict(result: dict, max_error_rate: float = 0.01, min_calls: int = 2000, allow_errors_on: tuple[str, ...] = ("package list",),
            max_route_errors: int = 0, max_allowed_route_error_rate: float = 0.25) -> list[str]:
    """Why the run failed, one line each; an empty list when it passed.

    The call count is over every route; the whole-run error rate and the per-route error count are over the routes that stay up only; a route in
    `allow_errors_on` is held to `max_allowed_route_error_rate` of its own calls instead.
    """
    total = result["total"]
    problems = []
    if total["requests"] < min_calls:
        problems.append(f"only {total['requests']} calls were made (at least {min_calls} expected): the load did not run through the upgrade")
    exempt = [row for row in result["routes"] if row["route"] in allow_errors_on]
    calls = total["requests"] - sum(row.get("requests", 0) for row in exempt)
    errors = total["errors"] - sum(row["errors"] for row in exempt)
    rate = errors / max(1, calls)
    if rate > max_error_rate:
        problems.append(f"{errors} of {calls} calls were errors on the routes that stay up ({rate:.2%} > {max_error_rate:.2%})")
    for row in result["routes"]:
        if row["route"] in allow_errors_on:
            own = row["errors"] / max(1, row.get("requests", 0))
            if own > max_allowed_route_error_rate:
                problems.append(f"{row['route']}: {row['errors']} errors of {row.get('requests', 0)} calls ({own:.2%} > {max_allowed_route_error_rate:.2%}), "
                                f"statuses {row['statuses']} (this route may have a gap, not an outage)")
        elif row["errors"] > max_route_errors:
            problems.append(f"{row['route']}: {row['errors']} errors, statuses {row['statuses']} (this route is served through the whole upgrade)")
    return problems


def exempt_lines(result: dict, allow_errors_on: tuple[str, ...] = ("package list",)) -> list[str]:
    """One line per route in `allow_errors_on` that the result has: its errors, calls, error rate and statuses (UPGRADE-GATE-EXEMPT: judged apart)."""
    lines = []
    for row in result["routes"]:
        if row["route"] in allow_errors_on:
            n = row.get("requests", 0)
            lines.append(f"allowed a gap: {row['route']}: {row['errors']} errors of {n} calls ({row['errors'] / max(1, n):.2%}), statuses {row['statuses']}")
    return lines


def main(argv: list[str]) -> int:
    """Prints the totals, the error slices and the first 150 failed calls of a `load_run.py` result, then the problems from `verdict`.

        Returns 1 when there is a problem, unless `--report-only` (then the problems are printed as notes and the status is 0).
    """
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results")
    ap.add_argument("--max-error-rate", type=float, default=0.01)
    ap.add_argument("--min-calls", type=int, default=2000)
    ap.add_argument("--max-route-errors", type=int, default=0)
    ap.add_argument("--report-only", action="store_true", help="print the verdict but exit 0: a jump over a release is outside the rolling guarantee")
    ap.add_argument("--allow-errors-on", action="append", default=None, help="a route that restarts with a gap (default: package list)")
    ap.add_argument("--max-allowed-route-error-rate", type=float, default=0.25,
                    help="the error rate a route that restarts with a gap may have over its own calls (default 0.25)")
    args = ap.parse_args(argv)
    result = json.load(open(args.results, encoding="utf-8"))
    bad = [row for row in result.get("timeline", []) if row["errors"]]
    print(f"{result['total']['requests']} calls over {result.get('seconds', '?')} s, {result['total']['errors']} errors")
    for row in bad:
        print(f"  {row['from_s']}-{row['to_s']} s: {row['errors']} errors of {row['calls']} calls")
    failures = result.get("failures", [])
    if failures:                                                  # when each failed call started and what it was told: the cause is found from these and the pods' logs
        print(f"the failed calls (UTC time the call ended, seconds after the warm-up, route, status, ms, start of the answer), {len(failures)} of {result['total']['errors']}:")
        for f in failures[:150]:
            print(f"  {f['at']} t={f['t_s']:>6} {f['route']:<18} {f['status']} {f['ms']:>6} ms  {f['detail']}")
    allowed = tuple(args.allow_errors_on or ("package list",))
    for line in exempt_lines(result, allowed):
        print(line)
    problems = verdict(result, args.max_error_rate, args.min_calls, allowed, args.max_route_errors, args.max_allowed_route_error_rate)
    for problem in problems:
        print(f"{'NOTE (report only)' if args.report_only else 'FAIL'}: {problem}", file=sys.stderr)
    return 1 if problems and not args.report_only else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
