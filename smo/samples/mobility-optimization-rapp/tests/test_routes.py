"""The Mobility Optimization rApp's HTTP routes (instance binding, lifecycle with the DMRO bounds, the evaluate loop with its
KPI-verified revert, coordination with the other rApps, audit views, the Digital Twin producer) through FastAPI's TestClient on
SQLite, with the AI Runtime SDK and R1 replaced by an in-memory platform double (`FakePlatform`): a configuration store the
double's O1 actions change and its read-back reports, scripted autonomy dispatches, and the real model and decision engine on
top. Run with: pytest samples/mobility-optimization-rapp/tests -q"""

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
from smo_shared.db import Base, get_session
from smo_shared.r1_client import acting_for
from smo_shared.testing import FakeDmeR1, make_test_engine

from app import main
from app.main import app
from app.producer import register_sim_type
from app.model.series import ATTEMPTS, PING_PONG, TOO_EARLY, TOO_LATE, WRONG_CELL
from app.producer import windows

ME = "gnb"
REL = "201-202"
T0 = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=datetime.UTC)
LATE = {ATTEMPTS: 200, TOO_LATE: 16, TOO_EARLY: 1, WRONG_CELL: 1, PING_PONG: 1}
WORSE = {ATTEMPTS: 200, TOO_LATE: 40, TOO_EARLY: 1, WRONG_CELL: 1, PING_PONG: 1}
FINE = {ATTEMPTS: 200, TOO_LATE: 1, TOO_EARLY: 0, WRONG_CELL: 0, PING_PONG: 0}


def _record(hour, values, relation=REL, scenario=None):
    return {"managedElementRef": ME, "cellId": relation.split("-")[0], "relation": relation, "values": values,
            "timestamp": (T0 + datetime.timedelta(hours=hour)).isoformat(), **({"scenario": scenario} if scenario else {})}


def _history(scenario="HEALTHY", hours=48):
    return [_record(0, v, scenario=scenario) | {"timestamp": t.isoformat()} for t, v in windows(REL, T0, hours, scenario)]


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
        self.records = {"TRAINING": _history(), "INFERENCE": [_record(0, LATE), _record(1, LATE)],
                        "EMULATION": _history("TOO_LATE") + _history("HEALTHY")}
        self.guards = []
        self.dispatch = {"status": "SHADOWED"}
        self.action_status = "COMPLETED"   # what the intent handler reports for an enacted change
        self.stuck = False                 # O1 acknowledges a write but the configuration does not change
        self.unreadable = set()
        self.actions, self.dispatches, self.training, self.expectations = [], [], [], []
        self.artifact = b""
        self.data = types.SimpleNamespace(
            get_dataset=self.get_dataset, read_config=self.read_config,
            query_cell_guards=lambda managed_element_ref: self.guards, register_type=lambda *a, **k: {"dmeTypeId": "t-1"}, own_invoker_id=lambda: "invoker-1",
            discover_types=lambda ns: [{"dmeTypeId": "t-sim", "dmeTypeIdStruct": {"name": "HO_PERFORMANCE_SIM"}}],
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
        if mfr in self.unreadable:
            raise SdkError(503, {"detail": "down"})
        return {"attributes": self.config.get(mfr, {})}

    def store_model(self, model_type, version, artifact, filename):
        self.artifact = artifact
        return {"artifactVersion": 4}

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
                relation = e["expectationObject"]["objectContexts"][0]["contextValueRange"][0]
                self.config.setdefault(f"NRCellRelation={relation}", {})["cellIndividualOffset"] = e["expectationTargets"][0]["targetValueRange"]
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
    Base.metadata.create_all(engine, tables=[m.__table__ for m in (main.MobilityInstance, main.MobilityRelation, main.MobilityDecision)])
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
        "packageId": str(uuid.uuid4()), "autonomyMode": mode,
        "configuration": {"managedElementRef": ME, "relations": [{"relation": REL, "source": "201", "target": "202"}], **config}})
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


def _cio(platform, relation=REL):
    return platform.config.get(f"NRCellRelation={relation}", {}).get("cellIndividualOffset")


# ---------------------------------------------------------------- instance binding


def _assert_decision(action, rapp, reason):
    """PR-AI-13: a direct write says why it is made, so RAN NF OAM's decision record (and an approver) has more than the job: the execution it came from, the
    version of the model that decided (when the instance has one) and the reason in words."""
    decision = action["decision"]
    assert decision["rationale"] == f"Restoring service: {reason}"
    assert decision["inputsRef"].startswith(f"{rapp}:") and ":execution:" in decision["inputsRef"] and decision["inputsRef"].endswith(action["context"]["correlationId"])
    assert set(decision) <= {"inputsRef", "modelVersion", "rationale"}

def test_start_binds_the_instance_and_discovers_a_dataset_per_stage(client, platform, r1):
    """`start` stores the configuration (baseline, merged DMRO bounds, peers, default RMIH) with a dataset per lifecycle stage, lists the instance
    and its relations, and a second start re-binds without a duplicate.
    """
    instance_id = _start(client, r1, "ASSIST", baselineCio=2, dmroBounds={"maximumDeviationHoTriggerHigh": 4},
                         energySavingInstanceId="es-1", trafficSteeringInstanceId="ts-1")
    view = client.get(f"/instances/{instance_id}").json()
    assert (view["baselineCio"], view["autonomyMode"], view["rmihId"], view["energySavingInstanceId"]) == (2, "ASSIST", "sa-smos", "es-1")
    assert view["dmroBounds"]["maximumDeviationHoTriggerHigh"] == 4 and view["dmroBounds"]["dmroControl"] is True
    assert set(view["datasets"]) == {"TRAINING", "INFERENCE", "EMULATION"}
    assert [i["instanceId"] for i in client.get("/instances").json()["items"]] == [instance_id]
    assert client.get(f"/instances/{instance_id}/relations").json()["items"][0]["state"] == "STEADY"
    assert client.post(f"/instances/{instance_id}/start").status_code == 200       # a second start re-binds
    assert len(client.get("/instances").json()["items"]) == 1


def test_start_rejects_an_unknown_instance_and_an_invalid_config(client, platform, r1):
    """An instance rapp-mgmt does not know is 404 INSTANCE_NOT_FOUND; one without relations, with an incomplete relation or without a managed
    element is 422 INSTANCE_CONFIG_INVALID.
    """
    resp = client.post(f"/instances/{uuid.uuid4()}/start")
    assert (resp.status_code, resp.json()["detail"]["title"]) == (404, "INSTANCE_NOT_FOUND")
    bad = ({"relations": []}, {"relations": [{"relation": REL, "source": "201"}]}, {"managedElementRef": ""})
    for config in bad:
        instance_id = str(uuid.uuid4())
        r1.paths[f"/rapp-mgmt/instances/{instance_id}"] = (200, {"configuration": {"managedElementRef": ME, "relations": [
            {"relation": REL, "source": "201", "target": "202"}], **config}})
        resp = client.post(f"/instances/{instance_id}/start")
        assert (resp.status_code, resp.json()["detail"]["title"]) == (422, "INSTANCE_CONFIG_INVALID")


def test_an_instance_that_was_never_started_is_404_on_every_route(client, platform, r1):
    """Every route that takes an instance id answers 404 INSTANCE_NOT_STARTED for one that was never started."""
    missing = uuid.uuid4()
    for verb, path in (("get", ""), ("post", "/lifecycle/deploy"), ("post", "/evaluate"), ("post", "/reconcile"),
                       ("get", "/relations"), ("get", "/dashboard")):
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

def test_the_model_lifecycle_trains_validates_emulates_and_deploys_with_the_dmro_bounds(client, platform, r1):
    """The four lifecycle routes run in order and record their job ids; deploy also writes the DMRO bounds through DME with a decision record and
    verifies them; training again keeps the registered model.
    """
    instance_id = _start(client, r1)
    trained = client.post(f"/instances/{instance_id}/lifecycle/train").json()
    assert (trained["status"], trained["metrics"]["artifactVersion"]) == ("TRAINED", 4)
    validated = client.post(f"/instances/{instance_id}/lifecycle/validate").json()
    assert (validated["validationJobId"], validated["status"], validated["passed"]) == ("va-1", "VALIDATED", True)
    emulated = client.post(f"/instances/{instance_id}/lifecycle/emulate").json()
    assert (emulated["emulationJobId"], emulated["passed"]) == ("em-1", True)
    deployed = client.post(f"/instances/{instance_id}/lifecycle/deploy").json()
    assert deployed["artifactVersion"] == 4 and deployed["dmro"]["verification"] == "VERIFIED"
    assert platform.config[f"DMROFunction={ME}"]["maximumDeviationHoTriggerHigh"] == 6
    assert "DMRO bounds" in platform.actions[0]["decision"]["rationale"]               # PR-AI-13: the bounds write says why it is made
    view = client.get(f"/instances/{instance_id}").json()
    assert view["lifecycleJobs"] == {"training": "tr-1", "validation": "va-1", "emulation": "em-1"}
    assert client.post(f"/instances/{instance_id}/lifecycle/train").json()["modelId"] == view["modelId"]


def test_a_dmro_write_that_does_not_stick_is_reported_unverified(client, platform, r1):
    """A DMRO bounds write that O1 does not apply is reported as VERIFY_FAILED on deploy."""
    instance_id = _start(client, r1)
    for step in ("train", "validate", "emulate"):
        client.post(f"/instances/{instance_id}/lifecycle/{step}")
    platform.stuck = True
    assert client.post(f"/instances/{instance_id}/lifecycle/deploy").json()["dmro"]["verification"] == "VERIFY_FAILED"


def test_training_on_too_little_history_is_422_and_the_job_is_failed(client, platform, r1):
    """Too little history is 422 TRAINING_FAILED and the training job is completed as failed with the reason, not left running."""
    instance_id = _start(client, r1)
    platform.records["TRAINING"] = platform.records["TRAINING"][:2]
    resp = client.post(f"/instances/{instance_id}/lifecycle/train")
    assert (resp.status_code, resp.json()["detail"]["title"]) == (422, "TRAINING_FAILED")
    assert platform.training[0][0] is False and "not enough" in platform.training[0][1]["failureReason"]
    assert client.get(f"/instances/{instance_id}").json()["lifecycleJobs"] == {"training": "tr-1"}


# ---------------------------------------------------------------- evaluate

def test_evaluate_needs_a_deployed_model(client, platform, r1):
    """`evaluate` before a model is deployed is 409 MODEL_NOT_DEPLOYED."""
    instance_id = _start(client, r1)
    resp = client.post(f"/instances/{instance_id}/evaluate")
    assert (resp.status_code, resp.json()["detail"]["title"]) == (409, "MODEL_NOT_DEPLOYED")


def test_evaluate_in_shadow_mode_recommends_a_change_and_writes_nothing(client, platform, r1):
    """In SHADOW mode a CIO raise is recorded as SHADOWED, the dispatch carries the six-entry CIO expectation, and O1 is not written."""
    instance_id = _deployed(client, platform, r1, "SHADOW")
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}
    body = client.post(f"/instances/{instance_id}/evaluate", headers={"X-Correlation-ID": "exec-1"}).json()
    d = body["decisions"][0]
    assert (body["executionId"], d["decision"], d["toCio"], d["outcome"]) == ("exec-1", "RAISE_CIO", 2, "SHADOWED")
    assert _cio(platform) is None and platform.dispatches[0][1][0]["expectationTargets"][0]["targetValueRange"] == [2] * 6


def test_evaluate_in_autonomous_mode_raises_the_cio_and_verifies_it(client, platform, r1):
    """An autonomous change goes through the intent, is verified by an O1 read-back, puts the relation under observation, and nothing else happens
    to it while observed.
    """
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    d = client.post(f"/instances/{instance_id}/evaluate", headers={"X-Correlation-ID": "exec-abcdef"}).json()["decisions"][0]
    assert (d["outcome"], d["verification"]["result"], d["finalState"]) == ("EXECUTED", "VERIFIED", {"state": "OBSERVING", "cio": 2})
    assert d["action"]["path"] == "INTENT" and _cio(platform) == [2] * 6
    rel = client.get(f"/instances/{instance_id}/relations").json()["items"][0]
    assert (rel["state"], rel["cio"], rel["lastChange"]["to"]) == ("OBSERVING", 2, 2)
    # while the change is observed nothing more happens
    again = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (again["decision"], again["reason"]) == ("NO_CHANGE", "OBSERVING")


def test_a_change_that_degrades_the_kpi_is_reverted_straight_through_dme(client, platform, r1):
    """When the problem rate is worse an hour after a change, the CIO is reverted through DME with the reason on the action and the decision
    record, and the relation is back to STEADY.
    """
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    client.post(f"/instances/{instance_id}/evaluate")
    platform.records["INFERENCE"] += [_record(2, WORSE)]
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["decision"], d["reason"], d["outcome"], d["kpi"]["verdict"]) == ("REVERT_CIO", "KPI_DEGRADED", "REVERTED", "DEGRADED")
    assert _cio(platform) == [0] * 6 and platform.actions[-1]["context"]["reason"] == "REVERT:KPI_DEGRADED"
    _assert_decision(platform.actions[-1], "mobility-optimization-rapp", "REVERT:KPI_DEGRADED")
    assert d["finalState"] == {"state": "STEADY", "cio": 0}


def test_a_revert_that_will_not_stick_is_retried_once_and_reported(client, platform, r1):
    """A revert that O1 does not apply is sent twice and reported as REVERT_FAILED, not as success."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    client.post(f"/instances/{instance_id}/evaluate")
    platform.records["INFERENCE"] += [_record(2, WORSE)]
    platform.stuck = True
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["outcome"] == "REVERT_FAILED" and d["verification"]["result"] == "VERIFY_FAILED"
    assert len(platform.actions) == 2 + 1     # the DMRO bounds, then the revert and its one re-send


def test_a_change_that_holds_up_is_confirmed(client, platform, r1):
    """A change whose problem rate did not get worse is CONFIRMED and the relation returns to STEADY."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    client.post(f"/instances/{instance_id}/evaluate")
    platform.records["INFERENCE"] += [_record(2, FINE)]
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["reason"], d["outcome"], d["kpi"]["verdict"]) == ("CHANGE_CONFIRMED", "CONFIRMED", "IMPROVED_OR_EQUAL")
    assert client.get(f"/instances/{instance_id}/relations").json()["items"][0]["state"] == "STEADY"


def test_a_write_that_o1_reports_but_does_not_apply_is_rolled_back(client, platform, r1):
    """A completed action whose read-back does not match is VERIFY_FAILED and rolled back; the relation is already on its old CIO so the rollback
    performs nothing.
    """
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    platform.config["NRCellRelation=201-202"] = {"cellIndividualOffset": [0] * 6}
    platform.stuck = True
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["outcome"], d["verification"]["result"], d["rollback"]["trigger"]) == ("VERIFY_FAILED_ROLLED_BACK", "VERIFY_FAILED", "VERIFY_FAILED")
    assert d["rollback"]["result"] == "ALREADY_RESTORED" and d["finalState"] == {"state": "STEADY", "cio": 0}


def test_a_rollback_that_cannot_be_verified_is_reported(client, platform, r1):
    """When even the restore cannot be read back, the relation is reported as VERIFY_FAILED_ROLLBACK_FAILED after two attempts."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    platform.stuck = True          # the live CIO is unreadable, so the restore is written (twice) and never confirmed
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["outcome"] == "VERIFY_FAILED_ROLLBACK_FAILED" and len(d["rollback"]["attempts"]) == 2


# One row per non-completed action status the intent handler can report, and the rollback trigger it maps to.
@pytest.mark.parametrize("status,trigger", [("PARTIAL_SUCCESS", "PARTIAL_SUCCESS"), ("FAILED", "ACTION_FAILED")])
def test_a_change_the_intent_handler_did_not_complete_is_rolled_back(client, platform, r1, status, trigger):
    """A PARTIAL_SUCCESS or FAILED action is rolled back without a verification read."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    platform.action_status = status
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["outcome"] == f"{trigger}_ROLLED_BACK" and d["verification"] is None


def test_a_change_the_intent_never_reported_is_rolled_back_as_failed(client, platform, r1):
    """An intent report with no action for the relation (or an unreadable one) counts as NOT_ENACTED and is rolled back as ACTION_FAILED."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched(noReport=True)
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["outcome"], d["action"]["status"]) == ("ACTION_FAILED_ROLLED_BACK", "NOT_ENACTED")


def test_assist_mode_waits_for_the_operator_then_enacts_on_reconcile(client, platform, r1):
    """In ASSIST mode the change waits for approval and the relation is skipped meanwhile; `reconcile` enacts it once approved, and a pending
    dispatch settles nothing.
    """
    instance_id = _deployed(client, platform, r1, "ASSIST")
    dispatch_id = str(uuid.uuid4())
    platform.dispatch = {"status": "AWAITING_SCOPE", "dispatchId": dispatch_id, "autonomyMode": "ASSIST"}
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["outcome"], d["intent"]["status"]) == ("AWAITING_APPROVAL", "AWAITING_SCOPE")
    assert client.get(f"/instances/{instance_id}/relations").json()["items"][0]["pendingDispatchId"] == dispatch_id
    assert client.post(f"/instances/{instance_id}/evaluate").json()["decisions"] == []      # nothing decided while it waits
    assert client.post(f"/instances/{instance_id}/reconcile").json() == {"settled": []}
    platform.dispatch = {**_dispatched("ASSIST"), "dispatchId": dispatch_id}
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert settled[0]["status"] == "DISPATCHED" and settled[0]["outcomes"][REL] == "EXECUTED"
    assert client.get(f"/instances/{instance_id}/relations").json()["items"][0]["state"] == "OBSERVING"


def test_assist_mode_rejection_leaves_the_relation_unchanged(client, platform, r1):
    """A rejected ASSIST dispatch ends as REJECTED and the relation stays STEADY."""
    instance_id = _deployed(client, platform, r1, "ASSIST")
    dispatch_id = str(uuid.uuid4())
    platform.dispatch = {"status": "AWAITING_SCOPE", "dispatchId": dispatch_id, "autonomyMode": "ASSIST"}
    client.post(f"/instances/{instance_id}/evaluate")
    platform.dispatch = {"status": "REJECTED", "dispatchId": dispatch_id, "autonomyMode": "ASSIST", "rejectedBy": "op"}
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"][REL] == "REJECTED"
    assert client.get(f"/instances/{instance_id}/relations").json()["items"][0]["state"] == "STEADY"


def test_the_guards_block_a_change_and_the_audit_trail_says_why(client, platform, r1):
    """Handover not allowed, an emergency target, a target asleep and a Traffic Steering relation under observation each block the change and are
    named in the audit; nothing is dispatched.
    """
    instance_id = _deployed(client, platform, r1, energySavingInstanceId="es-1", trafficSteeringInstanceId="ts-1")
    platform.guards = [{"cellId": "202", "cellClass": "EMERGENCY"}]
    platform.config["NRCellRelation=201-202"] = {"isHOAllowed": "false"}
    platform.config["NRCellDU=202"] = {"administrativeState": "LOCKED"}
    r1.paths["/rapps/es-1/operator/instances/es-1/cells"] = (200, {"items": [{"cellId": "202", "state": "SLEEP"}]})
    r1.paths["/rapps/ts-1/operator/instances/ts-1/relations"] = (200, {"items": [{"relation": REL, "state": "OBSERVING"}]})
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["decision"] == "NO_CHANGE"
    for guard in ("HO_NOT_ALLOWED", "PROTECTED_CELL", "TARGET_ASLEEP", "MLB_OBSERVING"):
        assert guard in d["reason"]
    assert d["safety"]["targetEnergySavingState"] == "SLEEP" and d["safety"]["hoAllowed"] is False
    assert platform.dispatches == []


def test_an_unreachable_coordination_peer_is_ignored(client, platform, r1):
    """A target whose O1 state cannot be read holds nothing, so a lost peer does not stop the loop."""
    instance_id = _deployed(client, platform, r1, "SHADOW", energySavingInstanceId="es-1", trafficSteeringInstanceId="ts-1")
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}
    platform.unreadable = {"NRCellDU=202"}
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["outcome"] == "SHADOWED" and d["safety"]["targetEnergySavingState"] is None


def test_a_relation_without_data_is_left_alone(client, platform, r1):
    """A relation with no PM windows gets NO_CHANGE with reason NO_DATA."""
    instance_id = _deployed(client, platform, r1)
    platform.records["INFERENCE"] = []
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["decision"], d["reason"]) == ("NO_CHANGE", "NO_DATA")


# ---------------------------------------------------------------- audit and dashboard

def test_decisions_are_listed_filtered_and_shown_on_the_dashboard(client, platform, r1):
    """The decision list filters by execution and relation, and the dashboard returns the rate trend (limited by `points`) and the latest decision."""
    instance_id = _deployed(client, platform, r1, "SHADOW")
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}
    for execution in ("e-1", "e-2"):
        client.post(f"/instances/{instance_id}/evaluate", headers={"X-Correlation-ID": execution})
    assert len(client.get(f"/instances/{instance_id}/decisions").json()["items"]) == 2
    only = client.get(f"/instances/{instance_id}/decisions", params={"execution_id": "e-1", "relation": REL, "limit": 5}).json()["items"]
    assert [d["executionId"] for d in only] == ["e-1"]
    assert client.get(f"/instances/{instance_id}/decisions", params={"relation": "other"}).json()["items"] == []
    board = client.get(f"/instances/{instance_id}/dashboard", params={"points": 1}).json()
    rel = board["relations"][0]
    assert board["instance"]["instanceId"] == instance_id and len(rel["rateTrend"]) == 1 and rel["latestDecision"]["outcome"] == "SHADOWED"


def test_the_dashboard_of_a_relation_without_decisions_has_no_latest_decision(client, platform, r1):
    """A started instance with no evaluation has a dashboard whose relation has no latest decision."""
    instance_id = _start(client, r1)
    assert client.get(f"/instances/{instance_id}/dashboard").json()["relations"][0]["latestDecision"] is None


# ---------------------------------------------------------------- the Digital Twin producer

def test_the_sim_producer_registers_publishes_and_answers_dme_callbacks(client, platform):
    """The Digital Twin producer registers its type (201), publishes windows to the data jobs, and answers DME's health, job and stop callbacks."""
    resp = client.post("/sim-producer/register")
    assert resp.status_code == 201 and resp.json()["dmeTypeId"] == "t-1"
    body = {"managedElementRef": ME, "relations": {REL: "TOO_LATE", "203-204": "PING_PONG"}, "start": T0.isoformat(), "hours": 3}
    assert client.post("/sim-producer/publish", json=body).json() == {"dmeTypeId": "t-sim", "dataJobs": 1, "recordsDelivered": 6}
    assert client.post("/sim-producer/publish", json={"relations": {}}).status_code == 422
    assert client.get("/sim-producer/health").json() == {"status": "healthy"}
    assert client.post("/sim-producer/jobs", json={"dataJobId": "j"}).json() == {"status": "accepted"}
    assert client.delete("/sim-producer/jobs/j-1").status_code == 204


def test_a_fulfilment_report_that_arrives_after_the_dispatch_is_waited_for(client, platform, r1, monkeypatch):
    """The handler publishes its report after the dispatch answers (the push is an outbox row, retried if the first send times out): the first reads
    find only the RECEIVED report, and the change is still enacted, verified and recorded once the report is there; it is not NOT_ENACTED."""
    monkeypatch.setattr(main, "INTENT_REPORT_POLL_SECONDS", 0.0)
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched(lateReport=3)
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert platform.report_reads == 4
    assert (d["outcome"], d["verification"]["result"]) == ("EXECUTED", "VERIFIED")


def test_the_wait_for_a_fulfilment_report_is_bounded(client, platform, r1, monkeypatch):
    """A handler that never publishes does not hang the evaluation: after INTENT_REPORT_WAIT_SECONDS the change counts as NOT_ENACTED and is rolled back."""
    monkeypatch.setattr(main, "INTENT_REPORT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(main, "INTENT_REPORT_POLL_SECONDS", 0.01)
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched(silentReport=True)
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
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
