"""The Traffic Steering rApp's HTTP routes (instance binding, lifecycle, the evaluate loop with its KPI-verified revert, the relations
view the Mobility rApp reads, coordination with the other rApps, audit views, the Digital Twin producer) through FastAPI's
TestClient on SQLite, with the AI Runtime SDK and R1 replaced by an in-memory platform double (`FakePlatform`): a configuration
store the double's O1 actions change and its read-back reports, scripted autonomy dispatches, and the real model and decision
engine on top. Run with: pytest samples/traffic-steering-rapp/tests -q"""

import datetime
import json
import types
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from smo_sdk import SdkError
from smo_sdk import data as smo_data
from smo_sdk.data import DataClient
from smo_sdk.data import AlarmScope
from smo_shared.db import Base, get_session
from smo_shared.r1_client import acting_for
from smo_shared.testing import FakeDmeR1, make_test_engine

from app import main
from app.main import app
from app.producer import register_sim_type
from app.producer import CELLS, LAYERS, NEIGHBOURS, baseline_settings, history, measurements, sim_cluster

ME = "gnb"
T0 = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=datetime.UTC)
HOTSPOT = {"401": "HOTSPOT"}
EVERY_HOTSPOT = {c: "HOTSPOT" for c in CELLS}
CELL_LIST = [{"cellId": c, "layer": LAYERS[c]} for c in CELLS]


def _window(hour, faults):
    return [{"managedElementRef": ME, **m}
            for m in measurements(NEIGHBOURS, LAYERS, None, faults, T0 + datetime.timedelta(hours=hour))]


def _sim(scenario, hot="a", hours=4):
    topology, layers = sim_cluster("dt1")
    return [{"managedElementRef": ME, **m} for h in range(hours) for m in measurements(
        topology, layers, None, {f"dt1-{hot}": "HOTSPOT"} if scenario == "HOTSPOT" else {}, T0 + datetime.timedelta(hours=h),
        extra=lambda c: {"cluster": "dt1", "scenario": scenario, "hotCell": f"dt1-{hot}", "layer": layers[c]})]


def _raise(error):
    def call(*a, **k):
        raise error
    return call


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code, self._payload = status_code, payload

    def json(self):
        return self._payload


class FakeR1:
    """rapp-mgmt's instance records and the other rApps' published state, by path."""
    def __init__(self):
        self.paths = {}

    def get(self, path, **kw):
        status, payload = self.paths.get(path, (404, {}))
        return FakeResponse(status, payload)


class FakePlatform:
    def __init__(self):
        self.config = {}                   # managed function ref -> attributes, what O1 read-back reports
        for cell, s in baseline_settings(NEIGHBOURS, LAYERS).items():
            for target, cio in s["cio"].items():
                self.config[f"NRCellRelation={cell}-{target}"] = {"cellIndividualOffset": [cio] * 6}
            for layer, prio in s["prio"].items():
                self.config[f"NRFreqRelation={cell}-{layer}"] = {"cellReselectionPriority": prio}
        self.records = {"TRAINING": history(NEIGHBOURS, LAYERS, T0, 72), "INFERENCE": _window(0, HOTSPOT), "EMULATION": _sim("HOTSPOT")}
        self.guards, self.alarms = [], []
        self.alarms_fail = False
        self.dispatch = {"status": "SHADOWED"}
        self.action_status = "COMPLETED"   # what the intent handler reports for an enacted step
        self.stuck = False                 # O1 acknowledges a write but the configuration does not change
        self.actions, self.dispatches, self.training, self.expectations = [], [], [], []
        self.artifact = b""
        self.data = types.SimpleNamespace(
            get_dataset=self.get_dataset, read_config=self.read_config, query_cell_guards=lambda managed_element_ref: self.guards,
            query_critical_alarms=self.query_alarms, register_type=lambda *a, **k: {"dmeTypeId": "t-1"}, own_invoker_id=lambda: "invoker-1",
            discover_types=lambda ns: [{"dmeTypeId": "t-sim", "dmeTypeIdStruct": {"name": "LOAD_PERFORMANCE_SIM"}}],
            list_data_jobs=lambda dme_type_id: [{"dataJobId": "j-1"}], ingest_data_record=lambda job, record: None)
        self.models = types.SimpleNamespace(
            register_model=lambda *a, **k: {"modelId": str(uuid.uuid4())}, store_model=self.store_model,
            download_artifact=lambda model_id, version: types.SimpleNamespace(content=self.artifact))
        self.lifecycle = types.SimpleNamespace(
            start_training=lambda *a, **k: {"trainingJobId": "tr-1"}, complete_training=self.complete_training,
            start_validation=lambda *a, **k: {"validationJobId": "va-1"},
            complete_validation=lambda job, ok, metrics: {"status": "VALIDATED" if ok else "FAILED"},
            start_emulation=lambda *a, **k: {"emulationJobId": "em-1"},
            complete_emulation=lambda job, ok, metrics: {"status": "EMULATED" if ok else "FAILED"},
            deploy_model=lambda *a, **k: {}, deploy_runtime=lambda *a, **k: {}, activate_runtime=lambda *a, **k: {},
            get_model_lifecycle=lambda model_id: {"state": "X"}, request_inference=lambda model_id: {"inferenceJobId": "inf-1"},
            resolve_inference=lambda job, ok, inference_outputs: {"aIMLInferenceReportId": "rep-1"})
        self.intent = types.SimpleNamespace(
            request_autonomy_dispatch=self.request_dispatch, get_autonomy_dispatch=self.get_dispatch,
            list_intent_reports=self.list_intent_reports)
        self.platform = types.SimpleNamespace(execute_action=self.execute_action)

    def get_dataset(self, name, consumer, lifecycle_stage=None, max_records=2000):
        return {"dmeTypeId": "t", "dataJobId": f"job-{lifecycle_stage}", "sourceDomain": "RAN", "records": self.records[lifecycle_stage]}

    def read_config(self, me, mfr):
        return {"attributes": self.config.get(mfr, {})}

    def query_alarms(self, me):
        if self.alarms_fail:
            raise SdkError(503, {"detail": "down"})
        return AlarmScope(self.alarms)

    def store_model(self, model_type, version, artifact, filename):
        self.artifact = artifact
        return {"artifactVersion": 6}

    def complete_training(self, job, ok, metrics, **kw):
        self.training.append((ok, metrics))
        return {"status": "TRAINED" if ok else "FAILED"}

    def execute_action(self, consumer, changes, action_id, source_context, decision=None):
        self.actions.append({"changes": changes, "actionId": action_id, "context": source_context, "decision": decision})
        if not self.stuck:
            for change in changes:
                self.config.setdefault(change["managedFunctionRef"], {}).update(change["attributeChanges"])
        return {"actionId": str(action_id), "status": "COMPLETED", "forwardedJobId": "fj-1"}

    def request_dispatch(self, instance_id, expectations, rmih_id, **kw):
        self.dispatches.append((instance_id, expectations, kw))
        self.expectations = expectations
        return self.get_dispatch(None)

    def get_dispatch(self, dispatch_id):
        if self.dispatch["status"] == "DISPATCHED" and not self.stuck:
            for e in self.expectations:
                name = e["expectationObject"]["objectContexts"][0]["contextValueRange"][0]
                target = e["expectationTargets"][0]
                ioc, attribute = target["targetName"].split(".")
                self.config.setdefault(f"{ioc}={name}", {})[attribute] = target["targetValueRange"]
        return self.dispatch

    def list_intent_reports(self, intent_id):
        self.report_reads = getattr(self, "report_reads", 0) + 1
        if self.report_reads <= self.dispatch.get("lateReport", 0) or self.dispatch.get("silentReport"):
            # the handler has not published yet: only the RECEIVED report Intent Service wrote when it stored the intent
            return [{"attributes": {"intentFulfilmentReport": {"intentFulfilmentInfo": {"fulfilmentStatus": "NOT_FULFILLED", "notFullfilledState": "RECEIVED"}}}}]
        if self.dispatch.get("noReport"):
            return [{"attributes": {}}, {"attributes": {"intentFulfilmentReport": {"additionalFulfilmentInfo": "{not json"}}}]
        info = json.dumps({"actions": [{"expectationId": e["expectationId"], "actionId": "a-1", "status": self.action_status}
                                       for e in self.expectations]})
        return [{"attributes": {}}, {"attributes": {"intentFulfilmentReport": {"additionalFulfilmentInfo": info}}}]


@pytest.fixture
def platform(monkeypatch):
    """The in-memory platform double that replaces the AI Runtime SDK, installed in the app for one test."""
    fake = FakePlatform()
    monkeypatch.setattr(main, "sdk", fake)
    return fake


@pytest.fixture
def r1(monkeypatch):
    """The R1 double (rapp-mgmt's instance records and the other rApps' published lists), installed in the app for one test."""
    fake = FakeR1()
    monkeypatch.setattr(main, "_r1", fake)
    return fake


@pytest.fixture
def client():
    """A TestClient on a fresh SQLite database with the rApp's three tables created and the session dependency overridden; the override is removed
    after the test.
    """
    engine = make_test_engine()
    Base.metadata.create_all(engine, tables=[m.__table__ for m in (main.TrafficInstance, main.TrafficCell, main.TrafficDecision)])
    factory = sessionmaker(bind=engine)

    def override_get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _start(client, r1, mode="AUTONOMOUS", **config):
    """Registers an instance with the R1 double, calls `start` and returns the instance id; `config` entries extend the instance configuration."""
    instance_id = str(uuid.uuid4())
    r1.paths[f"/rapp-mgmt/instances/{instance_id}"] = (200, {
        "packageId": str(uuid.uuid4()), "autonomyMode": mode, "configuration": {"managedElementRef": ME, "cells": CELL_LIST, **config}})
    resp = client.post(f"/instances/{instance_id}/start")
    assert resp.status_code == 200, resp.text
    return instance_id


def _deployed(client, platform, r1, mode="AUTONOMOUS", **config):
    """Starts an instance and runs train, validate, emulate and deploy, so the test begins with a deployed model."""
    instance_id = _start(client, r1, mode, **config)
    for step in ("train", "validate", "emulate", "deploy"):
        resp = client.post(f"/instances/{instance_id}/lifecycle/{step}")
        assert resp.status_code == 200, resp.text
    return instance_id


def _dispatched(mode="AUTONOMOUS", **extra):
    return {"status": "DISPATCHED", "dispatchId": str(uuid.uuid4()), "autonomyMode": mode, "intentId": "i-1", **extra}


def _evaluate(client, instance_id, **headers):
    resp = client.post(f"/instances/{instance_id}/evaluate", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _steps(body):
    return [d for d in body["decisions"] if d["decision"].startswith(("STEER_", "RELEASE_"))]


def _force_connected(platform):
    """The idle priority towards the other layer is at its bound, so the planner steers with the CIO."""
    platform.config["NRFreqRelation=401-F2100"] = {"cellReselectionPriority": 7}


def _force_idle(platform):
    """The CIOs of the source are at their bound, so the planner steers with the idle priority."""
    for target in NEIGHBOURS["401"]:
        platform.config[f"NRCellRelation=401-{target}"] = {"cellIndividualOffset": [6] * 6}


# ---------------------------------------------------------------- instance binding


def _assert_decision(action, rapp, reason):
    """PR-AI-13: a direct write says why it is made, so RAN NF OAM's decision record (and an approver) has more than the job: the execution it came from, the
    version of the model that decided (when the instance has one) and the reason in words."""
    decision = action["decision"]
    assert decision["rationale"] == f"Restoring service: {reason}"
    assert decision["inputsRef"].startswith(f"{rapp}:") and ":execution:" in decision["inputsRef"] and decision["inputsRef"].endswith(action["context"]["correlationId"])
    assert set(decision) <= {"inputsRef", "modelVersion", "rationale"}

def test_start_binds_the_instance_and_discovers_a_dataset_per_stage(client, platform, r1):
    """`start` stores the configuration (cells with layers, baselines, peers) with a dataset per lifecycle stage, lists the instance and its cells,
    and a second start re-binds without a duplicate.
    """
    instance_id = _start(client, r1, "ASSIST", baselineCio=1, baselinePriority=4, coverageInstanceId="co-1",
                         mobilityInstanceId="mo-1", energySavingInstanceId="es-1")
    view = client.get(f"/instances/{instance_id}").json()
    assert (view["cells"], view["baselineCio"], view["baselinePriority"], view["autonomyMode"]) == (CELL_LIST, 1, 4, "ASSIST")
    assert (view["coverageInstanceId"], view["mobilityInstanceId"], view["energySavingInstanceId"]) == ("co-1", "mo-1", "es-1")
    assert set(view["datasets"]) == {"TRAINING", "INFERENCE", "EMULATION"} and view["steeringLog"] == []
    assert [i["instanceId"] for i in client.get("/instances").json()["items"]] == [instance_id]
    cells = client.get(f"/instances/{instance_id}/cells").json()["items"]
    assert [(c["cellId"], c["layer"], c["state"]) for c in cells] == [(c, LAYERS[c], "STEADY") for c in CELLS]
    assert client.post(f"/instances/{instance_id}/start").status_code == 200       # a second start re-binds
    assert len(client.get("/instances").json()["items"]) == 1


def test_start_rejects_an_unknown_instance_and_an_invalid_config(client, platform, r1):
    """An instance rapp-mgmt does not know is 404 INSTANCE_NOT_FOUND; one without cells, a cell without a layer or without a managed element is 422
    INSTANCE_CONFIG_INVALID.
    """
    resp = client.post(f"/instances/{uuid.uuid4()}/start")
    assert (resp.status_code, resp.json()["detail"]["title"]) == (404, "INSTANCE_NOT_FOUND")
    for config in ({"cells": []}, {"cells": [{"cellId": "401"}]}, {"managedElementRef": ""}):
        instance_id = str(uuid.uuid4())
        r1.paths[f"/rapp-mgmt/instances/{instance_id}"] = (200, {"configuration": {"managedElementRef": ME, "cells": CELL_LIST, **config}})
        resp = client.post(f"/instances/{instance_id}/start")
        assert (resp.status_code, resp.json()["detail"]["title"]) == (422, "INSTANCE_CONFIG_INVALID")


def test_an_instance_that_was_never_started_is_404_on_every_route(client, platform, r1):
    """Every route that takes an instance id answers 404 INSTANCE_NOT_STARTED for one that was never started."""
    missing = uuid.uuid4()
    for verb, path in (("get", ""), ("post", "/lifecycle/deploy"), ("post", "/evaluate"), ("post", "/reconcile"),
                       ("get", "/cells"), ("get", "/relations"), ("get", "/dashboard")):
        resp = getattr(client, verb)(f"/instances/{missing}{path}")
        assert (resp.status_code, resp.json()["detail"]["title"]) == (404, "INSTANCE_NOT_STARTED"), path


def test_a_platform_error_is_passed_through_and_a_server_error_becomes_502(client, platform, r1, monkeypatch):
    """An SDK error below 500 reaches the caller with its own status and title; a platform 5xx is answered 502 PLATFORM_ERROR."""
    instance_id = _start(client, r1)
    monkeypatch.setattr(platform.lifecycle, "start_training", _raise(SdkError(409, {"detail": {"title": "BUSY", "detail": "x"}})))
    resp = client.post(f"/instances/{instance_id}/lifecycle/train")
    assert (resp.status_code, resp.json()["detail"]["title"]) == (409, "BUSY")
    monkeypatch.setattr(platform.lifecycle, "start_training", _raise(SdkError(500, {"detail": "boom"})))
    resp = client.post(f"/instances/{instance_id}/lifecycle/train")
    assert (resp.status_code, resp.json()["detail"]["title"]) == (502, "PLATFORM_ERROR")


# ---------------------------------------------------------------- lifecycle

def test_the_model_lifecycle_trains_validates_emulates_and_deploys(client, platform, r1):
    """The four lifecycle routes run in order, record their job ids on the instance, and training again reuses the registered model."""
    instance_id = _start(client, r1)
    trained = client.post(f"/instances/{instance_id}/lifecycle/train").json()
    assert (trained["status"], trained["metrics"]["artifactVersion"]) == ("TRAINED", 6)
    validated = client.post(f"/instances/{instance_id}/lifecycle/validate").json()
    assert (validated["validationJobId"], validated["status"]) == ("va-1", "VALIDATED" if validated["passed"] else "FAILED")
    emulated = client.post(f"/instances/{instance_id}/lifecycle/emulate").json()
    assert (emulated["emulationJobId"], emulated["status"]) == ("em-1", "EMULATED" if emulated["passed"] else "FAILED")
    deployed = client.post(f"/instances/{instance_id}/lifecycle/deploy").json()
    assert deployed["artifactVersion"] == 6 and deployed["model"]
    view = client.get(f"/instances/{instance_id}").json()
    assert view["lifecycleJobs"] == {"training": "tr-1", "validation": "va-1", "emulation": "em-1"}
    assert client.post(f"/instances/{instance_id}/lifecycle/train").json()["modelId"] == view["modelId"]


def test_training_on_too_little_history_is_422_and_the_job_is_failed(client, platform, r1):
    """Too little history is 422 TRAINING_FAILED and the training job is completed as failed with the reason, not left running."""
    instance_id = _start(client, r1)
    platform.records["TRAINING"] = platform.records["TRAINING"][:2]
    resp = client.post(f"/instances/{instance_id}/lifecycle/train")
    assert (resp.status_code, resp.json()["detail"]["title"]) == (422, "TRAINING_FAILED")
    assert platform.training[0][0] is False and platform.training[0][1]["failureReason"]
    assert client.get(f"/instances/{instance_id}").json()["lifecycleJobs"] == {"training": "tr-1"}


# ---------------------------------------------------------------- evaluate

def test_evaluate_needs_a_deployed_model(client, platform, r1):
    """`evaluate` before a model is deployed is 409 MODEL_NOT_DEPLOYED."""
    instance_id = _start(client, r1)
    resp = client.post(f"/instances/{instance_id}/evaluate")
    assert (resp.status_code, resp.json()["detail"]["title"]) == (409, "MODEL_NOT_DEPLOYED")


def test_evaluate_without_pm_data_decides_nothing(client, platform, r1):
    """With no PM windows an evaluation returns no decisions."""
    instance_id = _deployed(client, platform, r1)
    platform.records["INFERENCE"] = []
    assert _evaluate(client, instance_id)["decisions"] == []


def test_a_cell_without_pm_data_is_left_alone(client, platform, r1):
    """A cell with no PM windows gets NO_CHANGE with reason NO_DATA while the others are evaluated."""
    instance_id = _deployed(client, platform, r1, "SHADOW")
    platform.records["INFERENCE"] = [r for r in platform.records["INFERENCE"] if r["cellId"] != "412"]
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}
    by_cell = {d["cellId"]: d for d in _evaluate(client, instance_id)["decisions"]}
    assert (by_cell["412"]["decision"], by_cell["412"]["reason"]) == ("NO_CHANGE", "NO_DATA")


def test_evaluate_in_shadow_mode_plans_a_step_and_writes_nothing(client, platform, r1):
    """In SHADOW mode the hotspot's step is recorded as SHADOWED with its KPI baseline, the dispatch carries one expectation, no action reaches DME
    and the cell is not put under observation.
    """
    instance_id = _deployed(client, platform, r1, "SHADOW")
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}
    body = _evaluate(client, instance_id, **{"X-Correlation-ID": "exec-1"})
    steps = _steps(body)
    assert body["executionId"] == "exec-1" and [d["cellId"] for d in steps] == ["401"]
    assert steps[0]["outcome"] == "SHADOWED" and steps[0]["intent"]["status"] == "SHADOWED" and steps[0]["prediction"]["kpiBaseline"]
    assert platform.actions == [] and len(platform.dispatches[0][1]) == 1
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["state"] == "STEADY"


# One row per knob: the helper that forces the planner to choose it, and the knob expected.
@pytest.mark.parametrize("force,knob", [(_force_idle, "IDLE"), (_force_connected, "CONNECTED")])
def test_an_enacted_step_is_verified_and_then_confirmed_by_the_kpi(client, platform, r1, force, knob):
    """A step is verified by an O1 read-back, goes under observation and into the steering log, nothing else happens to the cell for an hour, and a
    calm cluster then confirms it.
    """
    instance_id = _deployed(client, platform, r1)
    force(platform)
    platform.dispatch = _dispatched()
    d = _steps(_evaluate(client, instance_id))[0]
    assert (d["cellId"], d["knob"], d["outcome"], d["verification"]["result"]) == ("401", knob, "EXECUTED", "VERIFIED")
    cell = client.get(f"/instances/{instance_id}/cells").json()["items"][0]
    assert cell["state"] == "OBSERVING" and cell["lastChange"]["knob"] == knob and cell["steering"] != {"cio": {}, "prio": {}}
    assert client.get(f"/instances/{instance_id}").json()["steeringLog"][0]["source"] == "401"
    # until an hour of post-change PM exists nothing else happens on the cell
    assert {x["cellId"]: x["reason"] for x in _evaluate(client, instance_id)["decisions"]}["401"] == "OBSERVING"
    # the cluster is calm an hour later: the step is confirmed
    platform.records["INFERENCE"] += _window(1, {})
    confirmed = {x["cellId"]: x for x in _evaluate(client, instance_id)["decisions"]}["401"]
    assert (confirmed["reason"], confirmed["outcome"], confirmed["kpi"]["verdict"]) == ("CHANGE_CONFIRMED", "CONFIRMED", "IMPROVED_OR_EQUAL")
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["state"] == "STEADY"


# One row per knob: the helper that forces the planner to choose it.
@pytest.mark.parametrize("force", [_force_idle, _force_connected])
def test_a_step_that_degrades_the_kpi_is_reverted_through_dme(client, platform, r1, force):
    """When the cluster is worse an hour after a step, the step is reverted through DME with the reason on the action and the decision record, and
    the steering in force is cleared.
    """
    instance_id = _deployed(client, platform, r1)
    force(platform)
    platform.dispatch = _dispatched()
    step = _steps(_evaluate(client, instance_id))[0]
    platform.records["INFERENCE"] += _window(1, EVERY_HOTSPOT)
    body = _evaluate(client, instance_id)
    reverted = {d["cellId"]: d for d in body["decisions"]}["401"]
    assert (reverted["decision"], reverted["outcome"], reverted["kpi"]["verdict"]) == ("REVERT", "REVERTED", "DEGRADED")
    assert reverted["reason"].startswith("KPI_DEGRADED:") and reverted["toValue"] == step["fromValue"]
    assert platform.actions[-1]["context"]["reason"] == "REVERT:KPI_DEGRADED"
    _assert_decision(platform.actions[-1], "traffic-steering-rapp", "REVERT:KPI_DEGRADED")
    cell = client.get(f"/instances/{instance_id}/cells").json()["items"][0]
    assert cell["state"] == "STEADY" and cell["steering"] == {"cio": {}, "prio": {}}


def test_a_revert_that_will_not_stick_is_retried_once_and_reported(client, platform, r1):
    """A revert that O1 does not apply is sent twice and reported as REVERT_FAILED, not as success."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    _steps(_evaluate(client, instance_id))
    platform.records["INFERENCE"] += _window(1, EVERY_HOTSPOT)
    platform.stuck = True
    reverted = {d["cellId"]: d for d in _evaluate(client, instance_id)["decisions"]}["401"]
    assert reverted["outcome"] == "REVERT_FAILED" and reverted["verification"]["result"] == "VERIFY_FAILED"
    assert len(platform.actions) == 2


def test_a_write_that_o1_reports_but_does_not_apply_is_rolled_back(client, platform, r1):
    """A completed action whose read-back does not match is VERIFY_FAILED and rolled back; the value is already the old one so the rollback
    performs nothing, and the cell stays STEADY.
    """
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    platform.stuck = True
    d = _steps(_evaluate(client, instance_id))[0]
    assert (d["outcome"], d["verification"]["result"], d["rollback"]["trigger"]) == ("VERIFY_FAILED_ROLLED_BACK", "VERIFY_FAILED", "VERIFY_FAILED")
    assert d["rollback"]["result"] == "ALREADY_RESTORED"
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["state"] == "STEADY"


def test_a_rollback_that_cannot_be_verified_is_reported(client, platform, r1):
    """When even the restore cannot be read back, the cell is reported as VERIFY_FAILED_ROLLBACK_FAILED after two attempts."""
    instance_id = _deployed(client, platform, r1)
    _force_connected(platform)
    platform.dispatch = _dispatched()
    platform.stuck = True
    platform.config["NRCellRelation=401-402"] = {}        # the live CIO is unreadable, so the restore is never confirmed
    platform.config["NRCellRelation=401-411"] = {}
    d = _steps(_evaluate(client, instance_id))[0]
    assert d["outcome"] == "VERIFY_FAILED_ROLLBACK_FAILED" and len(d["rollback"]["attempts"]) == 2


# One row per non-completed action status the intent handler can report, and the rollback trigger it maps to.
@pytest.mark.parametrize("status,trigger", [("PARTIAL_SUCCESS", "PARTIAL_SUCCESS"), ("FAILED", "ACTION_FAILED")])
def test_a_step_the_intent_handler_did_not_complete_is_rolled_back(client, platform, r1, status, trigger):
    """A PARTIAL_SUCCESS or FAILED action is rolled back without a verification read."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    platform.action_status = status
    d = _steps(_evaluate(client, instance_id))[0]
    assert d["outcome"] == f"{trigger}_ROLLED_BACK" and d["verification"] is None


def test_a_step_the_intent_never_reported_is_rolled_back_as_failed(client, platform, r1):
    """An intent report with no action for the cell (or an unreadable one) counts as NOT_ENACTED and is rolled back as ACTION_FAILED."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched(noReport=True)
    d = _steps(_evaluate(client, instance_id))[0]
    assert (d["outcome"], d["action"]["status"]) == ("ACTION_FAILED_ROLLED_BACK", "NOT_ENACTED")


def test_assist_mode_waits_for_the_operator_then_enacts_on_reconcile(client, platform, r1):
    """In ASSIST mode the step waits for approval and every cell is recorded as waiting; `reconcile` enacts it once approved, and a second
    reconcile settles nothing.
    """
    instance_id = _deployed(client, platform, r1, "ASSIST")
    dispatch_id = str(uuid.uuid4())
    platform.dispatch = {"status": "AWAITING_SCOPE", "dispatchId": dispatch_id, "autonomyMode": "ASSIST"}
    d = _steps(_evaluate(client, instance_id))[0]
    assert (d["outcome"], d["intent"]["status"]) == ("AWAITING_APPROVAL", "AWAITING_SCOPE")
    assert client.get(f"/instances/{instance_id}").json()["pendingDispatchId"] == dispatch_id
    # while it waits every cell is recorded as waiting, and a still-pending dispatch settles nothing
    assert {x["reason"] for x in _evaluate(client, instance_id)["decisions"]} == {"AWAITING_APPROVAL"}
    assert client.post(f"/instances/{instance_id}/reconcile").json() == {"settled": []}
    platform.dispatch = {**_dispatched("ASSIST"), "dispatchId": dispatch_id}
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert settled[0]["status"] == "DISPATCHED" and settled[0]["outcomes"] == {"401": "EXECUTED"}
    view = client.get(f"/instances/{instance_id}").json()
    assert view["pendingDispatchId"] is None and view["steeringLog"]
    assert client.post(f"/instances/{instance_id}/reconcile").json() == {"settled": []}


def test_assist_mode_rejection_leaves_the_cluster_unchanged(client, platform, r1):
    """A rejected ASSIST dispatch ends as REJECTED, clears the pending dispatch and writes nothing."""
    instance_id = _deployed(client, platform, r1, "ASSIST")
    dispatch_id = str(uuid.uuid4())
    platform.dispatch = {"status": "AWAITING_SCOPE", "dispatchId": dispatch_id, "autonomyMode": "ASSIST"}
    _evaluate(client, instance_id)
    platform.dispatch = {"status": "REJECTED", "dispatchId": dispatch_id, "autonomyMode": "ASSIST", "rejectedBy": "op"}
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"] == {"401": "REJECTED"}
    assert client.get(f"/instances/{instance_id}").json()["pendingDispatchId"] is None and platform.actions == []


def test_the_source_guards_block_a_cell_and_the_audit_trail_says_why(client, platform, r1):
    """A protected cell, a cell the EnergySaving rApp has asleep and one the Coverage rApp is observing are each held, and the reasons appear in
    the audit; nothing is dispatched.
    """
    instance_id = _deployed(client, platform, r1, energySavingInstanceId="es-1", coverageInstanceId="co-1")
    platform.guards = [{"cellId": "401", "cellClass": "EMERGENCY"}]
    d = {x["cellId"]: x for x in _evaluate(client, instance_id)["decisions"]}["401"]
    assert d["decision"] == "NO_CHANGE" and d["reason"] == "SAFETY_BLOCKED:PROTECTED_CELL" and platform.dispatches == []
    # an asleep source (published by the EnergySaving rApp) is blocked too, as is one the Coverage rApp is observing
    platform.guards = []
    r1.paths["/rapps/es-1/operator/instances/es-1/cells"] = (200, {"items": [{"cellId": "401", "state": "SLEEP", "lastUnlockedAt": "2026-09-04T11:50:00Z"}]})
    r1.paths["/rapps/co-1/operator/instances/co-1/cells"] = (200, {"items": [{"cellId": "401", "state": "OBSERVING"}]})
    d = {x["cellId"]: x for x in _evaluate(client, instance_id)["decisions"]}["401"]
    assert "CELL_ASLEEP" in d["reason"] and "COVERAGE_OBSERVING" in d["reason"] and d["safety"]["esState"] == "SLEEP"


def test_unsuitable_targets_are_excluded_from_the_options(client, platform, r1):
    """Targets that are in an incident zone or hold a critical alarm are listed as excluded with their reasons in the audit."""
    instance_id = _deployed(client, platform, r1, "SHADOW", mobilityInstanceId="mo-1", energySavingInstanceId="es-1")
    platform.guards = [{"cellId": "402", "incidentZone": "flood-7"}]
    platform.alarms = [{"alarmId": "al-1", "severity": "critical", "probableCause": "x", "managedFunctionRef": "NRCellDU=411"}]
    platform.config["NRCellDU=412"] = {"administrativeState": "LOCKED"}
    r1.paths["/rapps/mo-1/operator/instances/mo-1/relations"] = (200, {"items": [
        {"relation": "401-402", "source": "401", "target": "402", "state": "OBSERVING"}]})
    d = {x["cellId"]: x for x in _evaluate(client, instance_id)["decisions"]}["401"]
    excluded = {(e["target"], e["reason"]) for e in d["safety"]["excluded"] if "target" in e}
    assert ("402", "TARGET_PROTECTED") in excluded and ("411", "TARGET_CRITICAL_ALARM") in excluded
    assert d["safety"]["criticalAlarmIds"] == []


def test_unreachable_peers_and_an_unreadable_alarm_list_hold_nothing(client, platform, r1):
    """A failing alarm query or an unreachable peer rApp holds no cell, so a lost peer does not stop the loop."""
    instance_id = _deployed(client, platform, r1, "SHADOW", energySavingInstanceId="es-1", mobilityInstanceId="mo-1",
                            coverageInstanceId="co-1")
    platform.alarms_fail = True
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}
    body = _evaluate(client, instance_id)
    assert _steps(body) and all(d["safety"]["criticalAlarmIds"] == [] for d in body["decisions"] if d["safety"])


# ---------------------------------------------------------------- relations, audit and dashboard

def test_the_relations_view_publishes_the_cio_steering_for_the_mobility_rapp(client, platform, r1):
    """The relations route lists the CIO steering in force with its bias and whether a change on it is under observation, which is what the
    Mobility rApp reads for the shared CIO.
    """
    instance_id = _deployed(client, platform, r1)
    assert client.get(f"/instances/{instance_id}/relations").json() == {"items": []}
    _force_connected(platform)
    platform.dispatch = _dispatched()
    _evaluate(client, instance_id)
    items = client.get(f"/instances/{instance_id}/relations").json()["items"]
    assert len(items) == 1 and items[0]["source"] == "401" and items[0]["state"] == "OBSERVING" and items[0]["cioBias"] > 0
    platform.records["INFERENCE"] += _window(1, {})
    _evaluate(client, instance_id)
    assert [(i["state"], i["cioBias"] > 0) for i in client.get(f"/instances/{instance_id}/relations").json()["items"]] == [("STEADY", True)]


def test_decisions_are_listed_filtered_and_shown_on_the_dashboard(client, platform, r1):
    """The decision list filters by execution and cell, and the dashboard returns the score trend (limited by `points`) and each cell's latest
    decision.
    """
    instance_id = _deployed(client, platform, r1, "SHADOW")
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}
    for execution in ("e-1", "e-2"):
        _evaluate(client, instance_id, **{"X-Correlation-ID": execution})
    assert len(client.get(f"/instances/{instance_id}/decisions").json()["items"]) == 2 * len(CELLS)
    only = client.get(f"/instances/{instance_id}/decisions", params={"execution_id": "e-1", "cell_id": "401", "limit": 5}).json()["items"]
    assert [(d["executionId"], d["cellId"]) for d in only] == [("e-1", "401")]
    assert client.get(f"/instances/{instance_id}/decisions", params={"cell_id": "other"}).json()["items"] == []
    board = client.get(f"/instances/{instance_id}/dashboard", params={"points": 1}).json()
    cell = board["cells"][0]
    assert board["instance"]["instanceId"] == instance_id and len(board["cells"]) == len(CELLS)
    assert len(cell["scoreTrend"]) == 1 and cell["latestDecision"]["executionId"] in ("e-1", "e-2")


def test_the_dashboard_of_a_cluster_without_decisions_has_no_latest_decision(client, platform, r1):
    """A started instance with no evaluation has a dashboard whose cells have no latest decision."""
    instance_id = _start(client, r1)
    assert {c["latestDecision"] for c in client.get(f"/instances/{instance_id}/dashboard").json()["cells"]} == {None}


# ---------------------------------------------------------------- the Digital Twin producer

def test_the_sim_producer_registers_publishes_and_answers_dme_callbacks(client, platform):
    """The Digital Twin producer registers its type (201), publishes windows to the data jobs (a bad body is 422), and answers DME's health, job
    and stop callbacks.
    """
    resp = client.post("/sim-producer/register")
    assert resp.status_code == 201 and resp.json()["dmeTypeId"] == "t-1"
    body = {"managedElementRef": ME, "clusters": {"dt1": {"scenario": "HOTSPOT", "hotCell": "b"}, "dt2": {}}, "start": T0.isoformat(), "hours": 2}
    assert client.post("/sim-producer/publish", json=body).json() == {"dmeTypeId": "t-sim", "dataJobs": 1, "recordsDelivered": 16}
    assert client.post("/sim-producer/publish", json={"clusters": {}}).status_code == 422
    assert client.get("/sim-producer/health").json() == {"status": "healthy"}
    assert client.post("/sim-producer/jobs", json={"dataJobId": "j"}).json() == {"status": "accepted"}
    assert client.delete("/sim-producer/jobs/j-1").status_code == 204


def test_a_fulfilment_report_that_arrives_after_the_dispatch_is_waited_for(client, platform, r1, monkeypatch):
    """The handler publishes its report after the dispatch answers (the push is an outbox row, retried if the first send times out): the first reads
    find only the RECEIVED report, and the change is still enacted, verified and recorded once the report is there; it is not NOT_ENACTED."""
    monkeypatch.setattr(main, "INTENT_REPORT_POLL_SECONDS", 0.0)
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched(lateReport=3)
    d = _steps(_evaluate(client, instance_id))[0]
    assert platform.report_reads == 4
    assert (d["outcome"], d["verification"]["result"]) == ("EXECUTED", "VERIFIED")


def test_the_wait_for_a_fulfilment_report_is_bounded(client, platform, r1, monkeypatch):
    """A handler that never publishes does not hang the evaluation: after INTENT_REPORT_WAIT_SECONDS the change counts as NOT_ENACTED and is rolled back."""
    monkeypatch.setattr(main, "INTENT_REPORT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(main, "INTENT_REPORT_POLL_SECONDS", 0.01)
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched(silentReport=True)
    d = _steps(_evaluate(client, instance_id))[0]
    assert platform.report_reads >= 2
    assert (d["outcome"], d["action"]["status"]) == ("ACTION_FAILED_ROLLED_BACK", "NOT_ENACTED")


# ---------------------------------------------------------------- one producer for the callbacks

def test_the_process_and_an_instance_registering_leave_dme_one_producer_for_the_callback():
    """The rApp registers its Digital Twin producer as the process (`POST /sim-producer/register`) and as an instance (`demo/prepare`); DME must end with one producer for
    the callback URL, not two (it would post each job start twice), whichever registers first and whatever the order repeats in. Runs the real SDK against a DME that holds SEC-15.10b.
    """
    smo_data._registered_as.clear()
    fake = FakeDmeR1()
    sdk = types.SimpleNamespace(data=DataClient(fake))
    try:
        register_sim_type(sdk)
        with acting_for("a"):
            register_sim_type(sdk)
        assert [ids for ids in fake.callbacks().values()] == [["instance-a"]]
        register_sim_type(sdk)
        with acting_for("b"):
            register_sim_type(sdk)
        with acting_for("b"):
            register_sim_type(sdk)
        callbacks = fake.callbacks()
        assert len(callbacks) == 1 and list(callbacks.values()) == [["instance-b"]], callbacks
    finally:
        smo_data._registered_as.clear()
