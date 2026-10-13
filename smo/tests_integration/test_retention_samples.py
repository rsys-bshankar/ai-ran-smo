"""DB-3.10: the retention periods proposed in docs/RETENTION.md ship in a production sample values file (Helm) and in .env.example (compose);
the code and the chart defaults keep every row, so an upgrade deletes nothing.

Plain YAML and text, no `helm` needed; the rendered checks are skipped without it (CI's `helm` job has it).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

SMO_ROOT = Path(__file__).resolve().parent.parent
CHART = SMO_ROOT / "deploy" / "helm" / "smo"
PRODUCTION = yaml.safe_load((CHART / "values-production.yaml").read_text())
DEFAULTS = yaml.safe_load((CHART / "values.yaml").read_text())
GITOPS_PROD = yaml.safe_load((SMO_ROOT / "deploy" / "gitops" / "overlays" / "prod" / "values.yaml").read_text())
ENV_EXAMPLE = (SMO_ROOT / ".env.example").read_text()
COMPOSE = (SMO_ROOT / "docker-compose.yml").read_text()
RETENTION_MD = (SMO_ROOT / "docs" / "RETENTION.md").read_text()
helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")

# the settings in days that docs/RETENTION.md proposes a value for, and where the chart puts each (module env; "gui" is gui.env)
PROPOSED = {"SMO_RETENTION_ALARMS_DAYS": "90", "SMO_RETENTION_PM_FILES_DAYS": "14", "SAFEGUARD_REFUSAL_RETENTION_DAYS": "90",
            "SMO_RETENTION_MDAF_REPORTS_DAYS": "30", "GUI_AUDIT_RETENTION_DAYS": "365"}
# settings with a column in the table but no proposal ("keep"): the code and the chart keep every row, the production sample does not set them
KEPT = {"SMO_RETENTION_APPROVALS_DAYS": "keep", "SMO_RETENTION_DECISION_RECORDS_DAYS": "keep"}
WHERE = {"SMO_RETENTION_ALARMS_DAYS": ["ran-nf-oam-worker"], "SMO_RETENTION_PM_FILES_DAYS": ["ran-nf-oam-worker"],
         "SAFEGUARD_REFUSAL_RETENTION_DAYS": ["ran-nf-oam", "ran-nf-oam-worker"], "SMO_RETENTION_MDAF_REPORTS_DAYS": ["mdaf-worker"],
         "GUI_AUDIT_RETENTION_DAYS": ["gui"]}


def _table() -> dict[str, str]:
    """The days-settings of the table in docs/RETENTION.md with their 'Proposed value' (a row names one setting in backticks in its fifth column)."""
    found = {}
    for line in RETENTION_MD.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 8:
            continue
        match = re.match(r"`([A-Z_]+_DAYS)`", cells[4])
        if match:
            found[match.group(1)] = cells[5]
    return found


def _env(values: dict, where: str) -> dict:
    return values["gui"]["env"] if where == "gui" else values["modules"][where]["env"]


def test_the_document_proposes_the_values_the_samples_carry():
    """The table in docs/RETENTION.md names the same days-settings, with the same proposed values, as the production sample and the kept-everything
    exceptions.
    """
    assert _table() == {**PROPOSED, **KEPT}, "docs/RETENTION.md and the production sample disagree about the days-settings or their proposed values"


def test_the_production_sample_sets_every_days_setting_of_the_document_to_the_proposed_value():
    """The production values file sets every days-setting of the document, on every module that reads it, to the proposed value."""
    for variable, value in PROPOSED.items():
        for where in WHERE[variable]:
            assert _env(PRODUCTION, where)[variable] == value, f"{where}: {variable}"


def test_a_setting_with_no_proposal_is_in_the_chart_and_compose_at_zero_and_in_no_sample_that_would_delete_anything():
    """Each setting kept by default is 0 in the chart, absent from the production sample and the GitOps prod overlay, and written as `# NAME=0` in `.env.example` and defaulting to 0 in compose."""
    for variable in KEPT:
        assert DEFAULTS["modules"]["ran-nf-oam-worker"]["env"][variable] == "0", variable
        assert variable not in _env(PRODUCTION, "ran-nf-oam-worker") and variable not in _env(GITOPS_PROD, "ran-nf-oam-worker"), variable
        assert re.search(rf"^# {variable}=0\b", ENV_EXAMPLE, re.M), f".env.example: {variable}=0"
        assert re.search(rf"{variable}: \$\{{{variable}:-0\}}", COMPOSE), variable


def test_the_production_sample_only_names_modules_the_chart_has_and_sets_nothing_but_retention():
    """The production sample only names chart modules and sets only retention variables in their `env` (and the GUI's), so it cannot change
    anything else.
    """
    assert set(PRODUCTION["modules"]) <= set(DEFAULTS["modules"])
    for name, module in PRODUCTION["modules"].items():
        assert set(module) == {"env"}, f"{name}: the retention sample sets only env"
        assert all("RETENTION" in key for key in module["env"]), name
    assert set(PRODUCTION) == {"modules", "gui"} and set(PRODUCTION["gui"]) == {"env"}


def test_the_chart_defaults_keep_every_row():
    """Every retention setting has the default 0 in the chart, so an upgrade deletes no row, and the GUI's default env sets no retention."""
    seen = 0
    for name, module in DEFAULTS["modules"].items():
        for key, value in (module.get("env") or {}).items():
            if "RETENTION" in key:
                seen += 1
                assert str(value) == "0", f"{name}: {key} defaults to {value}: an upgrade would delete rows"
    for key in DEFAULTS["gui"]["env"] or {}:
        assert "RETENTION" not in key, f"gui.env defaults set {key}"
    assert seen >= 5


def test_the_gitops_production_overlay_carries_the_same_periods():
    """The GitOps production overlay carries the same proposed periods as the production sample."""
    for variable, value in PROPOSED.items():
        for where in WHERE[variable]:
            assert _env(GITOPS_PROD, where)[variable] == value, f"overlays/prod, {where}: {variable}"


def test_the_gui_audit_log_is_not_given_a_cronjob_it_could_not_run_safely():
    """gui-bff keeps SQLite on a volume one pod may hold (Recreate): a second pod from a CronJob must not open it. The sample says how to exec instead."""
    assert not (CHART / "templates" / "cronjob.yaml").exists()
    assert not any("CronJob" in p.read_text() for p in (CHART / "templates").glob("*.yaml"))
    assert DEFAULTS["modules"]["gui-bff"]["persistence"]
    assert "kubectl -n smo exec deploy/gui-bff -- python -m app.retention" in (CHART / "values-production.yaml").read_text()


def test_env_example_has_every_retention_variable_of_the_document_with_the_proposed_value():
    """`.env.example` lists every retention variable with the proposed value, commented out, so copying it deletes nothing."""
    for variable, value in PROPOSED.items():
        assert re.search(rf"^# {variable}={value}\b", ENV_EXAMPLE, re.M), f".env.example: {variable}={value}"
    assert re.search(r"^# SMO_RETENTION_WARN_ROWS=1000000\b", ENV_EXAMPLE, re.M)
    assert not re.search(r"^[A-Z_]*RETENTION[A-Z_]*=", ENV_EXAMPLE, re.M), "the retention variables are commented out: .env.example deletes nothing"


def test_compose_passes_every_retention_variable_through_with_a_default_that_keeps_everything():
    """Compose passes every retention variable through with a default of 0, and the warning row count with its default."""
    for variable in PROPOSED:
        assert re.search(rf"{variable}: \$\{{{variable}:-0\}}", COMPOSE), variable
    assert re.search(r"SMO_RETENTION_WARN_ROWS: \$\{SMO_RETENTION_WARN_ROWS:-1000000\}", COMPOSE)


def _render(*args: str) -> list[dict]:
    result = subprocess.run(["helm", "template", "smo", str(CHART), "-n", "smo", "--kube-version", "1.30.0", *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _deployment_env(docs: list[dict], name: str) -> dict:
    deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == name)
    return {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0].get("env") or []}


@helm
def test_the_production_sample_renders_and_sets_the_retention_of_the_workers_and_the_gui_backend():
    """Rendered with the production values, each module's Deployment (the GUI backend included) carries the proposed retention values, and no
    CronJob is added. Needs helm.
    """
    docs = _render("-f", str(CHART / "values-production.yaml"), "--set", "rappSigning.trustStoreConfigMap=rapp-publishers")
    for variable, value in PROPOSED.items():
        for where in WHERE[variable]:
            name = "gui-bff" if where == "gui" else where
            assert _deployment_env(docs, name)[variable] == value, f"{name}: {variable}"
    assert not [d for d in docs if d["kind"] == "CronJob"]


@helm
def test_the_default_render_sets_no_retention_other_than_zero():
    """In a default render every retention variable on every Deployment is 0. Needs helm."""
    docs = _render()
    for doc in docs:
        if doc["kind"] != "Deployment":
            continue
        for key, value in _deployment_env(docs, doc["metadata"]["name"]).items():
            if "RETENTION" in key:
                assert value == "0", f"{doc['metadata']['name']}: {key}={value}"
