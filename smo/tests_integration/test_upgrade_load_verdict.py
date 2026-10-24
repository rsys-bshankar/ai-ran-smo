"""The verdict of the load through an upgrade (scripts/upgrade_load_verdict.py, PR-V-10)."""

import importlib.util
import json
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("upgrade_load_verdict", SMO_ROOT / "scripts" / "upgrade_load_verdict.py")
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)


def result(calls=5000, errors=0, routes=None, timeline=None):
    """A `load_run.py` result with the given totals and route rows (by default two clean routes, one of them the exempt package list)."""
    return {"total": {"requests": calls, "errors": errors},
            "routes": routes if routes is not None else [{"route": "alarm list", "requests": 100, "errors": 0, "statuses": {200: 100}},
                                                          {"route": "package list", "requests": 100, "errors": 0, "statuses": {200: 100}}],
            "timeline": timeline or [], "seconds": 600}


def test_a_load_that_ran_and_was_answered_passes():
    """A load that made enough calls and had no errors passes with no problem."""
    assert v.verdict(result()) == []


def test_a_load_that_made_too_few_calls_did_not_run_through_the_upgrade():
    """A load that made fewer than 2000 calls fails, because it cannot have run through the upgrade, and 2000 passes."""
    assert "only 1999 calls were made (at least 2000 expected)" in v.verdict(result(calls=1999))[0]
    assert v.verdict(result(calls=2000)) == []


def test_the_error_rate_is_judged_over_the_whole_run():
    """The error rate of the routes that stay up is judged against a 1 % budget (adjustable): 1 % passes, just above it fails with the rate in the message."""
    assert v.verdict(result(calls=5100, errors=50)) == []                        # 1 % of the 5000 calls outside the package list: the budget
    assert "51 of 5000 calls were errors on the routes that stay up (1.02% > 1.00%)" in v.verdict(result(calls=5100, errors=51))[0]
    assert v.verdict(result(calls=5100, errors=51), max_error_rate=0.02) == []


def test_a_route_that_is_not_allowed_a_gap_must_have_no_errors_and_one_that_is_may():
    """A route that is not allowed any errors fails on the first one, a route that is allowed some does not, and a route that is under the budget
    in total is judged on its own.
    """
    routes = [{"route": "alarm list", "requests": 100, "errors": 1, "statuses": {200: 99, 503: 1}},
              {"route": "package list", "requests": 200, "errors": 40, "statuses": {200: 160, 0: 40}}]
    problems = v.verdict(result(errors=41, routes=routes), max_error_rate=0.05)
    assert len(problems) == 1 and problems[0].startswith("alarm list: 1 errors")
    assert v.verdict(result(errors=1, routes=[{"route": "alarm list", "requests": 100, "errors": 1, "statuses": {}}]), max_route_errors=1) == []


def test_the_command_prints_when_the_errors_were_and_exits_1_on_failure(tmp_path, capsys):
    """The command prints the time slices that had errors (not the clean ones), prints the failing route on standard error and exits 1, and exits 0
    for a clean result.
    """
    path = tmp_path / "r.json"
    path.write_text(json.dumps(result(errors=0, timeline=[{"from_s": 0, "to_s": 10, "calls": 100, "errors": 0}, {"from_s": 120, "to_s": 130, "calls": 100, "errors": 4}],
                                      routes=[{"route": "alarm list", "requests": 100, "errors": 4, "statuses": {0: 4}}])))
    assert v.main([str(path)]) == 1
    out = capsys.readouterr()
    assert "120-130 s: 4 errors of 100 calls" in out.out and "0-10 s" not in out.out and "FAIL: alarm list: 4 errors" in out.err
    path.write_text(json.dumps(result()))
    assert v.main([str(path)]) == 0


def test_report_only_prints_the_problems_and_exits_zero(tmp_path, capsys):
    """With `--report-only` the problems are printed as a note and the exit code is 0."""
    path = tmp_path / "r.json"
    path.write_text(json.dumps(result(calls=300, errors=100)))
    assert v.main([str(path)]) == 1
    assert v.main([str(path), "--report-only"]) == 0
    assert "NOTE (report only)" in capsys.readouterr().err


def test_the_failed_calls_are_printed_for_the_log(tmp_path, capsys):
    """The failed calls (time, route, status, duration and detail) are printed so the log shows what failed, even when the run passes."""
    result = {"total": {"requests": 3000, "errors": 1}, "routes": [], "timeline": [{"from_s": 140, "to_s": 150, "calls": 18, "errors": 1}],
              "failures": [{"at": "02:44:41.123", "t_s": 140.2, "route": "alarm list", "status": 503, "ms": 812, "detail": "AUTH_SERVICE_UNAVAILABLE"}]}
    path = tmp_path / "r.json"
    path.write_text(json.dumps(result))
    assert v.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "02:44:41.123" in out and "alarm list" in out and "503" in out and "AUTH_SERVICE_UNAVAILABLE" in out


# The nightly upgrade lane on main of 23 October (run 39932939882, from smo-v0.4.0): 40 errors in 3743 calls, 39 of them on the package list
# (415 calls, 9.4 %) and one dropped connection on the token route: it failed the old whole-run rate (1.07 %) on the exempt route alone.
OCT_23_ROUTES = [
    {"route": "service discovery", "requests": 427, "errors": 0, "statuses": {"200": 427}},
    {"route": "alarm list", "requests": 612, "errors": 0, "statuses": {"200": 612}},
    {"route": "kpi definitions", "requests": 421, "errors": 0, "statuses": {"200": 421}},
    {"route": "config job list", "requests": 406, "errors": 0, "statuses": {"200": 406}},
    {"route": "rApp instances", "requests": 435, "errors": 0, "statuses": {"200": 435}},
    {"route": "package list", "requests": 415, "errors": 39, "statuses": {"200": 376, "502": 38, "0": 1}},
    {"route": "data types", "requests": 417, "errors": 0, "statuses": {"200": 417}},
    {"route": "config job, dry run", "requests": 425, "errors": 0, "statuses": {"200": 425}},
    {"route": "token", "requests": 185, "errors": 1, "statuses": {"200": 184, "0": 1}},
]


def oct_23(**changes):
    """The 23 October nightly result, with route rows replaced by `changes` (route name to a new row) and the totals recomputed."""
    routes = [changes.get(row["route"], row) for row in OCT_23_ROUTES]
    return result(calls=sum(r["requests"] for r in routes), errors=sum(r["errors"] for r in routes), routes=routes)


def test_the_23_october_nightly_run_passes_once_the_exempt_route_is_judged_apart():
    """The run that failed on 23 October (40 of 3743 calls, 39 on the package list) passes with the gate the lane uses: one error in the 3328 calls of
    the routes that stay up (0.03 %), the token route's one error within its two, and the package list at 9.4 % of its calls, under the 25 % ceiling.
    """
    run = oct_23()
    assert (run["total"]["requests"], run["total"]["errors"]) == (3743, 40)
    assert v.verdict(run, max_route_errors=2) == []


def test_an_exempt_route_that_is_down_the_whole_time_fails():
    """A package list that answered nothing (an outage, not a gap) fails on its own rate, though the routes that stay up are clean."""
    down = {"route": "package list", "requests": 415, "errors": 415, "statuses": {"502": 415}}
    problems = v.verdict(oct_23(**{"package list": down}), max_route_errors=2)
    assert len(problems) == 1 and problems[0].startswith("package list: 415 errors of 415 calls (100.00% > 25.00%)")
    assert v.verdict(oct_23(**{"package list": down}), max_route_errors=2, max_allowed_route_error_rate=1.0) == []


def test_a_route_that_stays_up_still_fails_on_one_error():
    """With the default of no errors on a route that stays up, one error on such a route still fails, whatever the exempt route did."""
    one = {"route": "alarm list", "requests": 612, "errors": 1, "statuses": {"200": 611, "503": 1}}
    problems = v.verdict(oct_23(**{"alarm list": one, "token": {**OCT_23_ROUTES[-1], "errors": 0, "statuses": {"200": 185}}}))
    assert len(problems) == 1 and problems[0].startswith("alarm list: 1 errors")


def test_the_whole_run_rate_leaves_the_exempt_routes_out_of_both_errors_and_calls():
    """The rate is the routes that stay up's errors over their own calls: 20 errors in their 1000 calls fail at 2 %, where counting the package list's
    2000 calls and 30 errors as well would have given 1.67 %."""
    routes = [{"route": "alarm list", "requests": 1000, "errors": 20, "statuses": {"200": 980, "503": 20}},
              {"route": "package list", "requests": 2000, "errors": 30, "statuses": {"200": 1970, "502": 30}}]
    problems = v.verdict(result(calls=3000, errors=50, routes=routes), max_route_errors=100)
    assert problems == ["20 of 1000 calls were errors on the routes that stay up (2.00% > 1.00%)"]


def test_the_min_calls_bound_counts_every_route():
    """The call count includes the exempt route's calls: 1990 calls elsewhere and 10 on the package list make the 2000 asked for."""
    routes = [{"route": "alarm list", "requests": 1990, "errors": 0, "statuses": {}}, {"route": "package list", "requests": 10, "errors": 0, "statuses": {}}]
    assert v.verdict(result(calls=2000, routes=routes)) == []


def test_the_exempt_routes_are_reported_on_their_own_line(tmp_path, capsys):
    """The command prints each exempt route on a line of its own (errors, calls, rate, statuses), whether the run passes or not."""
    path = tmp_path / "r.json"
    path.write_text(json.dumps(oct_23()))
    assert v.main([str(path), "--max-route-errors", "2"]) == 0
    out = capsys.readouterr().out
    assert "allowed a gap: package list: 39 errors of 415 calls (9.40%), statuses" in out
    assert v.exempt_lines(oct_23(), ("package list",)) == [line for line in out.splitlines() if line.startswith("allowed a gap")]
