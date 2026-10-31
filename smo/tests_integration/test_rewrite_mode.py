"""Rewrite mode (`scripts/rewrite_mode.json`, plan task S0.8) cannot outlive 1.0, and the gates it pauses read it.

Until 1.0 the converged rewrite keeps nothing for 0.9.0 users, so the CI gates that compare with the previous release print a notice and pass while the
switch is on (`scripts/rewrite_mode.py`). The fail-safe here: once the release version is 1.0.0 or later (a 1.0.0 release candidate counts), the switch
must be off, because 1.0 is where expand-then-contract and the upgrade gates come back (plan task S8.8). The release version is read where a release
PR sets it (`docs/RELEASES.md`, "Cutting a release"): the newest dated `## [X.Y.Z]` heading of `CHANGELOG.md` and the chart's `version` and
`appVersion` (`deploy/helm/smo/Chart.yaml`), which are raised together in that PR; the tags are not used, because CI's checkouts often have none.
No fixtures. Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_rewrite_mode.py -q
"""

import importlib.util
import json
import re
from pathlib import Path

import yaml

SMO_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SMO_ROOT.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

_spec = importlib.util.spec_from_file_location("rewrite_mode", SMO_ROOT / "scripts" / "rewrite_mode.py")
rewrite_mode = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rewrite_mode)

# (workflow file, job id) of every gate the switch pauses; S8.8 switches each back on
PAUSED_GATES = [
    ("smo-tests.yml", "breaking-changes"),
    ("smo-tests.yml", "previous-release-compat"),
    ("smo-tests.yml", "upgrade-from-previous-release"),
    ("smo-upgrade-kind.yml", "upgrade"),
]


def _version(text: str) -> tuple[int, int, int]:
    """`X.Y.Z` or `X.Y.Z-rc.N` as a tuple; a release candidate of X.Y.Z counts as X.Y.Z, so 1.0.0-rc.1 already closes rewrite mode."""
    major, minor, patch = re.match(r"(\d+)\.(\d+)\.(\d+)", text).groups()
    return int(major), int(minor), int(patch)


def release_versions() -> dict[str, tuple[int, int, int]]:
    """The release version from each place a release PR sets it: the newest dated CHANGELOG heading and the chart's version and appVersion."""
    changelog = (SMO_ROOT / "CHANGELOG.md").read_text()
    newest = re.search(r"^## \[(\d+\.\d+\.\d+(?:-rc\.\d+)?)\] - \d{4}-\d{2}-\d{2}$", changelog, re.M)
    chart = yaml.safe_load((SMO_ROOT / "deploy" / "helm" / "smo" / "Chart.yaml").read_text())
    return {
        "CHANGELOG.md newest release": _version(newest.group(1)),
        "Chart.yaml version": _version(str(chart["version"])),
        "Chart.yaml appVersion": _version(str(chart["appVersion"])),
    }


def leaks_past_1_0(switch: dict, versions: dict[str, tuple[int, int, int]]) -> list[str]:
    """The places whose version is 1.0.0 or later while the switch is still on: empty means rewrite mode has not leaked past 1.0."""
    if not switch["active"]:
        return []
    return [f"{where} is {'.'.join(map(str, v))}" for where, v in versions.items() if v >= (1, 0, 0)]


def test_rewrite_mode_is_off_once_the_release_is_1_0_or_later():
    """The fail-safe: with a release version of 1.0.0 or later in CHANGELOG.md or Chart.yaml, `scripts/rewrite_mode.json` must have `active: false`,
    or the gates that hold 1.0 to its upgrade promise would stay paused (plan task S8.8)."""
    leaks = leaks_past_1_0(rewrite_mode.load(), release_versions())
    assert leaks == [], f"rewrite mode is still on at release 1.0 or later ({'; '.join(leaks)}): set active to false (plan task S8.8)"


def test_the_fail_safe_would_catch_a_1_0_release_or_candidate():
    """The check above can fail: a 1.0.0 release, a 1.0.0 release candidate or a later version with the switch on is reported, and 0.x or a switch
    that is off is not."""
    on, off = {"active": True}, {"active": False}
    assert leaks_past_1_0(on, {"CHANGELOG.md newest release": _version("1.0.0")})
    assert leaks_past_1_0(on, {"Chart.yaml appVersion": _version("1.0.0-rc.1")})
    assert leaks_past_1_0(on, {"Chart.yaml version": _version("1.2.3")})
    assert leaks_past_1_0(on, {"CHANGELOG.md newest release": _version("0.99.0")}) == []
    assert leaks_past_1_0(off, {"CHANGELOG.md newest release": _version("1.0.0")}) == []


def test_the_release_versions_are_found():
    """Each place the fail-safe reads has a version (a renamed heading or chart key would otherwise blind it), and today they agree."""
    versions = release_versions()
    assert len(versions) == 3
    assert len(set(versions.values())) == 1, versions


def test_the_switch_is_well_formed():
    """The switch names when it started, the release that ends it and why, and `active` is a boolean (a broken file runs the gates, which is safe,
    but would surprise the next PR)."""
    switch = rewrite_mode.load()
    assert set(switch) == {"active", "since", "until", "reason"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", switch["since"])
    assert switch["until"] == "smo-v1.0.0"
    assert "docs/plan/CONVERGED_REWRITE_PLAN.md" in switch["reason"]
    assert (SMO_ROOT / "docs" / "plan" / "CONVERGED_REWRITE_PLAN.md").is_file()


def test_every_paused_gate_reads_the_switch_and_keeps_its_check():
    """Each paused job calls `scripts/rewrite_mode.py notice` and still has the step that runs its real check, so S8.8 only has to turn the switch off."""
    checks = {
        "breaking-changes": "check_breaking_changes.py",
        "previous-release-compat": "check_previous_release_code.sh",
        "upgrade-from-previous-release": "check_upgrade_from_previous_release.sh",
        "upgrade": "helm upgrade smo",
    }
    for workflow, job_id in PAUSED_GATES:
        job = yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"][job_id]
        runs = "\n".join(step.get("run", "") for step in job["steps"])
        assert "rewrite_mode.py notice" in runs, f"{workflow} {job_id} does not read the switch"
        assert checks[job_id] in runs, f"{workflow} {job_id} lost its check"


def test_the_notice_names_the_file_and_a_broken_switch_runs_the_gate(tmp_path, capsys):
    """On: exit 0 and a GitHub notice naming `smo/scripts/rewrite_mode.json`. Off: exit 1, nothing printed. Missing, not JSON, or no boolean `active`:
    exit 1 with a warning, so the gate runs (fail-safe)."""
    switch = tmp_path / "rewrite_mode.json"
    switch.write_text(json.dumps({"active": True, "since": "2026-10-31", "until": "smo-v1.0.0", "reason": "r"}))
    assert rewrite_mode.notice("Gate", switch) == 0
    out = capsys.readouterr().out
    assert out.startswith("::notice file=smo/scripts/rewrite_mode.json,") and "Gate: paused" in out
    switch.write_text(json.dumps({"active": False}))
    assert rewrite_mode.notice("Gate", switch) == 1
    assert capsys.readouterr().out == ""
    for broken in ("", "{not json", json.dumps({"active": "yes"}), json.dumps([True])):
        switch.write_text(broken)
        assert rewrite_mode.notice("Gate", switch) == 1
        assert capsys.readouterr().out.startswith("::warning file=smo/scripts/rewrite_mode.json::")
    assert rewrite_mode.notice("Gate", tmp_path / "missing.json") == 1
    assert "::warning" in capsys.readouterr().out
