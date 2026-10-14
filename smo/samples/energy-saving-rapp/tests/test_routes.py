"""The EnergySaving rApp's HTTP routes (instance binding, lifecycle, the evaluate loop, override, audit views, the Digital
Twin producer) through FastAPI's TestClient on SQLite, with the AI Runtime SDK and R1 replaced by an in-memory platform double
(`FakePlatform`): a cell-configuration store the double's O1 actions change and its read-back reports, scripted autonomy
dispatches, and the real model and decision engine on top. Run with: pytest samples/energy-saving-rapp/tests -q"""

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
from app.producer import samples

ME = "gnb"
CELL = "101"
T0 = datetime.datetime(2026, 9, 2, 0, 0, tzinfo=datetime.UTC)
HISTORY = [{"managedElementRef": ME, **s} for s in samples([CELL], T0, 72, 60)[CELL]]
NIGHT = [{"managedElementRef": ME, "cellId": CELL, "value": 2.0, "timestamp": (T0 + datetime.timedelta(minutes=5 * i)).isoformat()}
         for i in range(14)]
BUSY = [{**r, "value": 40.0} for r in NIGHT]


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
    """rapp-mgmt's instance record, as the R1 proxy returns it."""
    def __init__(self):
        self.instances = {}

    def get(self, path, **kw):
        info = self.instances.get(path.rsplit("/", 1)[-1])
        return FakeResponse(200, info) if info else FakeResponse(404, {})


class FakePlatform:
    """Just enough of the SDK's surface for this rApp: `sdk.<area>.<call>` is routed to a method here."""
    def __init__(self):
        self.config = {}              # cell -> administrativeState, what O1 read-back reports (unset = UNLOCKED)
        self.records = {"TRAINING": HISTORY, "INFERENCE": NIGHT, "EMULATION": HISTORY}
        self.guards, self.alarms, self.prediction = [], [], None
        self.dispatch = {"status": "SHADOWED"}
        self.action_status = "COMPLETED"   # what an enacted LOCK reports
        self.stuck = False                 # O1 acknowledges a write but the cell's configuration does not change
        self.actions, self.dispatches, self.training = [], [], []
        self.artifact = b""
        self.data = types.SimpleNamespace(
            get_dataset=self.get_dataset, read_config=self.read_config, query_cell_guards=lambda: self.guards,
            query_critical_alarms=lambda me: AlarmScope(self.alarms), register_type=lambda *a, **k: {"dmeTypeId": "t-1"}, own_invoker_id=lambda: "invoker-1",
            discover_types=lambda ns: [{"dmeTypeId": "t-sim", "dmeTypeIdStruct": {"name": "PRB_UTILIZATION_SIM"}}],
            list_data_jobs=lambda dme_type_id: [{"dataJobId": "j-1"}, {"dataJobId": "j-2"}],
            ingest_data_record=lambda job, record: None)
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
        self.analytics = types.SimpleNamespace(get_prediction=self.get_prediction)

    def get_dataset(self, name, consumer, lifecycle_stage=None, max_records=2000):
        return {"dmeTypeId": "t", "dataJobId": f"job-{lifecycle_stage}", "sourceDomain": "RAN", "records": self.records[lifecycle_stage]}

    def read_config(self, me, mfr):
        return {"attributes": {"administrativeState": self.config.get(mfr.split("=")[1], "UNLOCKED")}}

    def store_model(self, model_type, version, artifact, filename):
        self.artifact = artifact
        return {"artifactVersion": 3}

    def complete_training(self, job, ok, metrics, **kw):
        self.training.append((ok, metrics))
        return {"status": "TRAINED" if ok else "FAILED"}

    def get_prediction(self, key, pm_name):
        if self.prediction == "error":
            raise SdkError(404, {})
        return self.prediction

    def execute_action(self, consumer, changes, action_id, source_context, decision=None):
        self.actions.append({"changes": changes, "actionId": action_id, "context": source_context, "decision": decision})
        if not self.stuck:
            for change in changes:
                self.config[change["managedFunctionRef"].split("=")[1]] = change["attributeChanges"]["administrativeState"]
        locking = changes[0]["attributeChanges"]["administrativeState"] == "LOCKED"
        return {"actionId": action_id, "status": self.action_status if locking else "COMPLETED", "forwardedJobId": "fj-1"}

    def request_dispatch(self, instance_id, expectations, rmih_id, **kw):
        self.dispatches.append((instance_id, expectations, kw))
        if self.dispatch["status"] == "DISPATCHED" and not self.stuck:
            for cell in expectations[0]["expectationObject"]["objectContexts"][0]["contextValueRange"]:
                self.config[cell] = "LOCKED"
        return self.dispatch

    def get_dispatch(self, dispatch_id):
        if self.dispatch["status"] == "DISPATCHED" and not self.stuck:
            self.config[CELL] = "LOCKED"          # the operator's approval enacted the lock meanwhile
        return self.dispatch

    def list_intent_reports(self, intent_id):
        self.report_reads = getattr(self, "report_reads", 0) + 1
        if self.report_reads <= self.dispatch.get("lateReport", 0) or self.dispatch.get("silentReport"):
            # the handler has not published yet: only the RECEIVED report Intent Service wrote when it stored the intent
            return [{"attributes": {"intentFulfilmentReport": {"intentFulfilmentInfo": {"fulfilmentStatus": "NOT_FULFILLED", "notFullfilledState": "RECEIVED"}}}}]
        if self.dispatch.get("noReport"):
            return [{"attributes": {}}, {"attributes": {"intentFulfilmentReport": {"additionalFulfilmentInfo": "{not json"}}}]
        info = json.dumps({"actions": [{"actionId": "a-1", "status": self.action_status}]})
        return [{"attributes": {}}, {"attributes": {"intentFulfilmentReport": {"additionalFulfilmentInfo": info}}}]


@pytest.fixture
def platform(monkeypatch):
    """The in-memory platform double that replaces the AI Runtime SDK, installed in the app for one test."""
    fake = FakePlatform()
    monkeypatch.setattr(main, "sdk", fake)
    return fake


@pytest.fixture
def r1(monkeypatch):
    """The R1 double (rapp-mgmt's instance record), installed in the app for one test."""
    fake = FakeR1()
    monkeypatch.setattr(main, "_r1", fake)
    return fake


@pytest.fixture
def client():
    """A TestClient on a fresh SQLite database with the rApp's three tables created and the session dependency overridden; the override is removed
    after the test.
    """
    engine = make_test_engine()
    Base.metadata.create_all(engine, tables=[m.__table__ for m in (main.EnergySavingInstance, main.EnergySavingCell, main.EnergySavingDecision)])
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
    r1.instances[instance_id] = {"packageId": str(uuid.uuid4()), "autonomyMode": mode,
                                 "configuration": {"managedElementRef": ME, "cells": [CELL], **config}}
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


# ---------------------------------------------------------------- instance binding


def _assert_decision(action, rapp, reason):
    """PR-AI-13: a direct write says why it is made, so RAN NF OAM's decision record (and an approver) has more than the job: the execution it came from, the
    version of the model that decided (when the instance has one) and the reason in words."""
    decision = action["decision"]
    assert decision["rationale"] == f"Restoring service: {reason}"
    assert decision["inputsRef"].startswith(f"{rapp}:") and ":execution:" in decision["inputsRef"] and decision["inputsRef"].endswith(action["context"]["correlationId"])
    assert set(decision) <= {"inputsRef", "modelVersion", "rationale"}

def test_start_binds_the_instance_and_discovers_a_dataset_per_stage(client, platform, r1):
    """`start` stores the configuration (cells, actuator, mode, default RMIH) with a dataset per lifecycle stage, lists the instance and its cells,
    and a second start re-binds without a duplicate.
    """
    instance_id = _start(client, r1, "ASSIST", operatorNotificationUri="http://ops/notify")
    view = client.get(f"/instances/{instance_id}").json()
    assert (view["cells"], view["actuator"], view["autonomyMode"], view["rmihId"]) == ([CELL], "ADMINISTRATIVE_STATE", "ASSIST", "sa-smos")
    assert set(view["datasets"]) == {"TRAINING", "INFERENCE", "EMULATION"}
    assert [i["instanceId"] for i in client.get("/instances").json()["items"]] == [instance_id]
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["state"] == "SERVING"
    # starting again re-binds the same instance rather than adding one
    assert client.post(f"/instances/{instance_id}/start").status_code == 200
    assert len(client.get("/instances").json()["items"]) == 1


def test_start_rejects_an_unknown_instance_and_an_invalid_config(client, platform, r1):
    """An instance rapp-mgmt does not know is 404 INSTANCE_NOT_FOUND; one without cells or with an unknown actuator is 422 INSTANCE_CONFIG_INVALID."""
    resp = client.post(f"/instances/{uuid.uuid4()}/start")
    assert (resp.status_code, resp.json()["detail"]["title"]) == (404, "INSTANCE_NOT_FOUND")
    for config in ({"cells": []}, {"actuator": "FAN"}):
        instance_id = str(uuid.uuid4())
        r1.instances[instance_id] = {"configuration": {"managedElementRef": ME, "cells": [CELL], **config}}
        resp = client.post(f"/instances/{instance_id}/start")
        assert (resp.status_code, resp.json()["detail"]["title"]) == (422, "INSTANCE_CONFIG_INVALID")


def test_an_instance_that_was_never_started_is_404_on_every_route(client, platform, r1):
    """Every route that takes an instance id answers 404 INSTANCE_NOT_STARTED for one that was never started."""
    missing = uuid.uuid4()
    for verb, path in (("get", ""), ("post", "/lifecycle/train"), ("post", "/evaluate"), ("post", "/reconcile"),
                       ("get", "/cells"), ("get", "/dashboard")):
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
    """The four lifecycle routes run in order, record their job ids on the instance, and training again keeps the registered model."""
    instance_id = _start(client, r1)
    trained = client.post(f"/instances/{instance_id}/lifecycle/train").json()
    assert (trained["status"], trained["metrics"]["artifactVersion"]) == ("TRAINED", 3)
    validated = client.post(f"/instances/{instance_id}/lifecycle/validate").json()
    assert (validated["validationJobId"], validated["status"], validated["passed"]) == ("va-1", "VALIDATED", True)
    emulated = client.post(f"/instances/{instance_id}/lifecycle/emulate").json()
    assert (emulated["emulationJobId"], emulated["passed"]) == ("em-1", True)
    deployed = client.post(f"/instances/{instance_id}/lifecycle/deploy").json()
    assert deployed["artifactVersion"] == 3 and deployed["model"]["profile"]
    view = client.get(f"/instances/{instance_id}").json()
    assert view["lifecycleJobs"] == {"training": "tr-1", "validation": "va-1", "emulation": "em-1"} and view["modelId"]
    # training again keeps the registered model
    assert client.post(f"/instances/{instance_id}/lifecycle/train").json()["modelId"] == view["modelId"]


def test_training_on_too_little_history_is_422_and_the_job_is_failed(client, platform, r1):
    """Too little history is 422 TRAINING_FAILED and the training job is completed as failed with the reason, not left running."""
    instance_id = _start(client, r1)
    platform.records["TRAINING"] = HISTORY[:2]
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


def test_evaluate_in_shadow_mode_recommends_a_lock_and_changes_nothing(client, platform, r1):
    """In SHADOW mode a LOCK is recorded as SHADOWED, the dispatch carries the LOCKED expectation, O1 is not written and the cell is left in
    PRE_SLEEP.
    """
    instance_id = _deployed(client, platform, r1, "SHADOW")
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}
    body = client.post(f"/instances/{instance_id}/evaluate", headers={"X-Correlation-ID": "exec-1"}).json()
    d = body["decisions"][0]
    assert (body["executionId"], d["decision"], d["outcome"], d["intent"]["status"]) == ("exec-1", "LOCK", "SHADOWED", "SHADOWED")
    assert platform.config == {} and platform.dispatches[0][1][0]["expectationTargets"][0]["targetValueRange"] == "LOCKED"
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["state"] == "PRE_SLEEP"


def test_evaluate_in_autonomous_mode_locks_the_cell_and_verifies_it(client, platform, r1):
    """An autonomous LOCK goes through the intent, is verified by an O1 read-back and leaves the cell asleep; the next pass keeps it asleep."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["outcome"], d["verification"]["result"], d["finalState"]) == ("EXECUTED", "VERIFIED", {"state": "SLEEP", "o1": "LOCKED"})
    assert d["action"]["path"] == "INTENT" and platform.config == {CELL: "LOCKED"}
    # a second pass finds nothing to do: the cell's low load keeps it asleep
    again = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (again["decision"], again["reason"]) == ("NO_CHANGE", "SLEEPING")


def test_a_lock_that_o1_reports_but_does_not_apply_is_rolled_back(client, platform, r1):
    """A lock whose read-back does not match is VERIFY_FAILED and the cell is rolled back to SERVING and UNLOCKED."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    platform.stuck = True
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["outcome"], d["verification"]["result"], d["rollback"]["trigger"]) == ("VERIFY_FAILED_ROLLED_BACK", "VERIFY_FAILED", "VERIFY_FAILED")
    assert d["finalState"] == {"state": "SERVING", "o1": "UNLOCKED"}


def test_a_partially_applied_lock_is_rolled_back_through_dme(client, platform, r1):
    """A PARTIAL_SUCCESS action is rolled back through DME without a verification read."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    platform.action_status = "PARTIAL_SUCCESS"
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["outcome"] == "PARTIAL_SUCCESS_ROLLED_BACK" and d["rollback"]["trigger"] == "PARTIAL_SUCCESS"
    assert d["verification"] is None and platform.config[CELL] == "UNLOCKED"


def test_a_lock_the_intent_never_enacted_is_rolled_back_as_failed(client, platform, r1):
    """An intent report with no usable action counts as NOT_ENACTED and is rolled back as ACTION_FAILED."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched(noReport=True)
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["outcome"], d["action"]["status"], d["rollback"]["trigger"]) == ("ACTION_FAILED_ROLLED_BACK", "NOT_ENACTED", "ACTION_FAILED")


def test_assist_mode_waits_for_the_operator_then_enacts_on_reconcile(client, platform, r1):
    """In ASSIST mode the lock waits for approval and the cell is skipped meanwhile; `reconcile` enacts it once approved, and a pending dispatch
    settles nothing.
    """
    instance_id = _deployed(client, platform, r1, "ASSIST")
    dispatch_id = str(uuid.uuid4())
    platform.dispatch = {"status": "AWAITING_SCOPE", "dispatchId": dispatch_id, "autonomyMode": "ASSIST"}
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["outcome"], d["intent"]["status"]) == ("AWAITING_APPROVAL", "AWAITING_SCOPE")
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["pendingDispatchId"] == dispatch_id
    # nothing is decided for the cell while it waits, and a still-pending dispatch settles nothing
    assert client.post(f"/instances/{instance_id}/evaluate").json()["decisions"] == []
    assert client.post(f"/instances/{instance_id}/reconcile").json() == {"settled": []}
    # the operator resolves it: the next reconcile enacts the lock
    platform.dispatch = {**_dispatched("ASSIST"), "dispatchId": dispatch_id}
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert settled[0]["status"] == "DISPATCHED" and settled[0]["outcomes"][CELL] == "EXECUTED"
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["state"] == "SLEEP"


def test_assist_mode_rejection_returns_the_cell_to_service(client, platform, r1):
    """A rejected ASSIST dispatch ends as REJECTED and the cell goes back to SERVING."""
    instance_id = _deployed(client, platform, r1, "ASSIST")
    dispatch_id = str(uuid.uuid4())
    platform.dispatch = {"status": "AWAITING_SCOPE", "dispatchId": dispatch_id, "autonomyMode": "ASSIST"}
    client.post(f"/instances/{instance_id}/evaluate")
    platform.dispatch = {"status": "REJECTED", "dispatchId": dispatch_id, "autonomyMode": "ASSIST", "rejectedBy": "op"}
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert settled[0]["outcomes"][CELL] == "REJECTED"
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["state"] == "SERVING"


def test_a_lock_on_a_cell_already_locked_is_not_repeated(client, platform, r1):
    """A LOCK for a cell O1 already reports locked is recorded as NO_ACTION_ALREADY_IN_STATE and no dispatch is made."""
    instance_id = _deployed(client, platform, r1)
    platform.config[CELL] = "LOCKED"
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["decision"], d["outcome"]) == ("LOCK", "NO_ACTION_ALREADY_IN_STATE")
    assert platform.dispatches == []


def test_a_guard_blocks_the_lock_and_the_audit_trail_says_why(client, platform, r1):
    """An EMERGENCY cell with a neighbour's critical alarm is not locked, the reason names the guard and the MDAF prediction is in the audit."""
    instance_id = _deployed(client, platform, r1)
    platform.guards = [{"managedElementRef": ME, "cellId": CELL, "cellClass": "EMERGENCY", "neighbourRefs": [f"{ME}/102"]}]
    platform.alarms = [{"alarmId": "al-1", "severity": "critical", "probableCause": "coverage hole", "managedFunctionRef": "NRCellDU=102"}]
    platform.prediction = {"pmPredictedValue": 1.0}
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["decision"] == "NO_CHANGE" and d["reason"].startswith("SAFETY_BLOCKED:EMERGENCY_CELL")
    assert d["prediction"]["mdafFuturePrb"] == 1.0
    assert platform.dispatches == []


def test_an_mdaf_outage_does_not_stop_the_loop(client, platform, r1):
    """When MDAF errors the evaluation still decides, with no MDAF prediction recorded."""
    instance_id = _deployed(client, platform, r1, "SHADOW")
    platform.prediction = "error"
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["prediction"]["mdafFuturePrb"] is None and d["outcome"] == "SHADOWED"


def _asleep(client, platform, r1, mode):
    """Deploys a model, evaluates once so the cell is locked, then makes load return (busy PRB and a high MDAF prediction), so the next evaluation
    must wake it.
    """
    instance_id = _deployed(client, platform, r1, mode)
    platform.dispatch = _dispatched(mode)
    client.post(f"/instances/{instance_id}/evaluate")
    assert platform.config == {CELL: "LOCKED"}
    platform.records["INFERENCE"] = BUSY      # load returns
    platform.prediction = {"pmPredictedValue": 30.0}
    return instance_id


def test_returning_load_wakes_a_sleeping_cell_straight_through_dme(client, platform, r1):
    """A sleeping cell whose load returns is unlocked directly through DME (not through an intent) with the reason and a decision record on the
    action.
    """
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["decision"], d["reason"], d["outcome"], d["finalState"]) == ("UNLOCK", "PREDICTED_LOAD", "EXECUTED", {"state": "SERVING", "o1": "UNLOCKED"})
    assert platform.config[CELL] == "UNLOCKED" and platform.actions[-1]["context"]["reason"] == "WAKE:PREDICTED_LOAD"
    _assert_decision(platform.actions[-1], "energy-saving-rapp", "WAKE:PREDICTED_LOAD")


def test_a_wake_that_will_not_stick_is_resent_once_and_reported(client, platform, r1):
    """A wake that O1 does not apply is sent twice and reported as VERIFY_FAILED with the cell still in SLEEP."""
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    platform.stuck = True
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["outcome"] == "VERIFY_FAILED" and d["rollback"]["trigger"] == "VERIFY_FAILED" and len(d["rollback"]["attempts"]) == 2
    assert d["finalState"]["state"] == "SLEEP"


def test_in_shadow_mode_a_wake_is_only_recommended(client, platform, r1):
    """In SHADOW mode an UNLOCK is only recommended and the cell stays locked."""
    instance_id = _asleep(client, platform, r1, "SHADOW")
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["decision"], d["outcome"]) == ("UNLOCK", "SHADOWED") and platform.config[CELL] == "LOCKED"


def test_a_wake_for_a_cell_already_awake_performs_no_action(client, platform, r1):
    """A wake for a cell O1 already reports unlocked is recorded as NO_ACTION_ALREADY_IN_STATE."""
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    platform.config[CELL] = "UNLOCKED"
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["outcome"] == "NO_ACTION_ALREADY_IN_STATE"


# ---------------------------------------------------------------- override, audit, dashboard

def test_an_override_wakes_the_cell_and_suppresses_the_ai_until_cleared(client, platform, r1):
    """An operator override wakes the cell, keeps the AI from locking it until the override is cleared, is idempotent for an awake cell, and
    clearing an unknown cell is still 204.
    """
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    resp = client.post(f"/instances/{instance_id}/cells/{CELL}/override", json={"operator": "alice", "reason": "event"})
    d = resp.json()
    assert (resp.status_code, d["reason"], d["outcome"], d["safety"]["note"]) == (200, "OPERATOR_OVERRIDE:alice", "EXECUTED", "event")
    assert platform.config[CELL] == "UNLOCKED"
    cell = client.get(f"/instances/{instance_id}/cells").json()["items"][0]
    assert (cell["state"], cell["overrideBy"]) == ("SERVING", "alice")
    platform.records["INFERENCE"] = NIGHT
    held = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert held["reason"] == "OPERATOR_OVERRIDE" and held["decision"] == "NO_CHANGE"
    # overriding a cell that is already awake performs nothing; clearing releases the cell
    again = client.post(f"/instances/{instance_id}/cells/{CELL}/override", json={"operator": "bob"}).json()
    assert again["outcome"] == "NO_ACTION_ALREADY_IN_STATE"
    assert client.delete(f"/instances/{instance_id}/cells/{CELL}/override").status_code == 204
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["overrideBy"] is None
    assert client.delete(f"/instances/{instance_id}/cells/nope/override").status_code == 204


def test_an_override_reports_a_wake_that_cannot_be_verified(client, platform, r1):
    """An override whose wake does not verify is reported as VERIFY_FAILED."""
    instance_id = _start(client, r1)
    platform.config[CELL] = "LOCKED"
    platform.stuck = True
    d = client.post(f"/instances/{instance_id}/cells/{CELL}/override", json={"operator": "alice"}).json()
    assert d["outcome"] == "VERIFY_FAILED"


def test_override_validates_the_request_and_the_cell(client, platform, r1):
    """An override without an operator is 422 and one for a cell the instance does not manage is 404 CELL_NOT_MANAGED."""
    instance_id = _start(client, r1)
    assert client.post(f"/instances/{instance_id}/cells/{CELL}/override", json={}).status_code == 422
    resp = client.post(f"/instances/{instance_id}/cells/nope/override", json={"operator": "alice"})
    assert (resp.status_code, resp.json()["detail"]["title"]) == (404, "CELL_NOT_MANAGED")


def test_decisions_are_listed_filtered_and_shown_on_the_dashboard(client, platform, r1):
    """The decision list filters by execution and cell, and the dashboard returns the PRB trend (limited by `points`) and the latest decision."""
    instance_id = _deployed(client, platform, r1, "SHADOW")
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}
    for execution in ("e-1", "e-2"):
        client.post(f"/instances/{instance_id}/evaluate", headers={"X-Correlation-ID": execution})
    assert len(client.get(f"/instances/{instance_id}/decisions").json()["items"]) == 2
    only = client.get(f"/instances/{instance_id}/decisions", params={"execution_id": "e-1", "cell_id": CELL, "limit": 5}).json()["items"]
    assert [d["executionId"] for d in only] == ["e-1"]
    assert client.get(f"/instances/{instance_id}/decisions", params={"cell_id": "other"}).json()["items"] == []
    board = client.get(f"/instances/{instance_id}/dashboard", params={"points": 3}).json()
    cell = board["cells"][0]
    assert board["instance"]["instanceId"] == instance_id and len(cell["prbTrend"]) == 3 and cell["latestDecision"]["outcome"] == "SHADOWED"


def test_the_dashboard_of_a_cell_without_decisions_has_no_latest_decision(client, platform, r1):
    """A started instance with no evaluation has a dashboard whose cell has no latest decision."""
    instance_id = _start(client, r1)
    assert client.get(f"/instances/{instance_id}/dashboard").json()["cells"][0]["latestDecision"] is None


# ---------------------------------------------------------------- cell-state history (GUI-9.8b)

def _age_decisions(hours: float) -> None:
    """Moves every stored decision row `hours` into the past (created and settled), through the test's own session."""
    session = next(app.dependency_overrides[get_session]())
    for d in session.query(main.EnergySavingDecision).all():
        d.created_at -= datetime.timedelta(hours=hours)
        d.updated_at -= datetime.timedelta(hours=hours)
    session.commit()
    session.close()


def test_the_cell_state_history_lists_each_change_and_steps_the_chart(client, platform, r1):
    """A sleep then a wake are two transitions, newest first, each naming the decision that made it; the chart points start at the state before the
    window, step at each change (before and after at the same time) and end at the current state; a pass that changes nothing adds nothing."""
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    client.post(f"/instances/{instance_id}/evaluate", headers={"X-Correlation-ID": "wake-1"})
    platform.records["INFERENCE"], platform.prediction = BUSY, {"pmPredictedValue": 30.0}
    client.post(f"/instances/{instance_id}/evaluate")                      # still busy: NO_CHANGE, no transition
    body = client.get(f"/instances/{instance_id}/cell-states").json()
    assert body["instanceId"] == instance_id and body["truncated"] is False
    moves = [(t["fromState"], t["toState"], t["decision"]) for t in body["transitions"]]
    assert moves == [("SLEEP", "SERVING", "UNLOCK"), ("SERVING", "SLEEP", "LOCK")]
    assert body["transitions"][0]["executionId"] == "wake-1" and body["transitions"][0]["o1Value"] == "UNLOCKED"
    assert [p["level"] for p in body["points"]] == [2, 2, 0, 0, 2, 2]
    assert body["points"][0]["t"] == body["since"] and body["points"][-1]["t"] == body["until"]
    assert body["points"][1]["t"] == body["points"][2]["t"] == body["transitions"][1]["at"]
    assert body["cells"] == [{"cellId": CELL, "state": "SERVING", "transitions": 2}]


def test_a_change_before_the_window_sets_the_starting_state_but_is_not_listed(client, platform, r1):
    """Changes settled 48 hours ago are outside a 24-hour window: no transition, and the chart is flat at the state they left (SLEEP); a 72-hour
    window lists them again."""
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    _age_decisions(48)
    body = client.get(f"/instances/{instance_id}/cell-states", params={"hours": 24}).json()
    assert body["transitions"] == [] and [p["state"] for p in body["points"]] == ["SLEEP", "SLEEP"]
    assert len(client.get(f"/instances/{instance_id}/cell-states", params={"hours": 72}).json()["transitions"]) == 1


def test_a_cell_without_decisions_is_flat_at_serving(client, platform, r1):
    """A started instance with no decision yet: no transitions, and the cell is SERVING from `since` to `until`."""
    instance_id = _start(client, r1)
    body = client.get(f"/instances/{instance_id}/cell-states").json()
    assert body["transitions"] == [] and [(p["state"], p["level"]) for p in body["points"]] == [("SERVING", 2), ("SERVING", 2)]


def test_the_cell_state_history_filters_and_validates(client, platform, r1):
    """`cell_id` narrows the answer to that cell and is 404 for a cell the instance does not manage; `hours` must be 1 to 168; an instance that
    was never started is 404."""
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    assert [c["cellId"] for c in client.get(f"/instances/{instance_id}/cell-states", params={"cell_id": CELL}).json()["cells"]] == [CELL]
    resp = client.get(f"/instances/{instance_id}/cell-states", params={"cell_id": "nope"})
    assert (resp.status_code, resp.json()["detail"]["title"]) == (404, "CELL_NOT_MANAGED")
    for hours in (0, 169):
        assert client.get(f"/instances/{instance_id}/cell-states", params={"hours": hours}).status_code == 422
    assert client.get(f"/instances/{uuid.uuid4()}/cell-states").status_code == 404


def test_past_the_row_bound_the_oldest_changes_are_left_out_and_flagged(client, platform, r1, monkeypatch):
    """With a bound of one row only the newest change is listed, `truncated` is true, `since` moves up to it, and its `fromState` is still the state
    the earlier (left-out) row settled."""
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    client.post(f"/instances/{instance_id}/evaluate")                      # the wake
    monkeypatch.setattr(main, "CELL_STATE_MAX_ROWS", 1)
    body = client.get(f"/instances/{instance_id}/cell-states").json()
    assert body["truncated"] is True and [(t["fromState"], t["toState"]) for t in body["transitions"]] == [("SLEEP", "SERVING")]
    assert body["since"] == body["transitions"][0]["at"]


# ---------------------------------------------------------------- the Digital Twin producer

def test_the_sim_producer_registers_under_the_callers_invoker_id(client, platform):
    """DME takes an rApp's producer only under its own invoker id (SEC-15.10b): the registration names the id the SDK reports, not a fixed name."""
    seen = []
    platform.data.register_type = lambda *a, **k: seen.append(a[4]) or {"dmeTypeId": "t-1"}
    assert client.post("/sim-producer/register").status_code == 201
    assert seen == ["invoker-1"]


def test_the_sim_producer_registers_publishes_and_answers_dme_callbacks(client, platform):
    """The Digital Twin producer registers its type (201), publishes samples to the data jobs (a bad body is 422), and answers DME's health, job
    and stop callbacks.
    """
    resp = client.post("/sim-producer/register")
    assert resp.status_code == 201 and resp.json()["dmeTypeId"] == "t-1"
    body = {"managedElementRef": ME, "cells": [CELL], "start": T0.isoformat(), "hours": 3}
    assert client.post("/sim-producer/publish", json=body).json() == {"dmeTypeId": "t-sim", "dataJobs": 2, "recordsDelivered": 6}
    assert client.post("/sim-producer/publish", json={"cells": [CELL]}).status_code == 422
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
