"""Coverage Optimization rApp — the Wave 10.3 reference rApp
(HISTORY.md Wave 10.3, W10.3-01..10). A Non-RT RIC rApp that
tunes a cluster of cells' digital tilt and transmit power, jointly, from
weak-coverage, overshoot and pilot-pollution PM, through O1. Like the
other reference rApps it uses O1 PM data only, and the R1 interface only
through the AI Runtime SDK; there is no A1, Near-RT RIC, xApp or E2.

The loop (POST /instances/{id}/evaluate), over the whole cluster:

    Input (COVERAGE_PERFORMANCE per cell) → KPI check of the last change set
          → Safety per cell → Bounds → Joint plan (CoverageSensitivityModel)
          → O1 execution → Verification → Rollback / Revert → Audit

How each kind of write reaches O1:

  * A **change set** (up to two cells, one tilt or power step each) is
    governed by the instance's autonomy mode, through one AutonomyDispatch
    with one expectation per cell:
      - SHADOW only recommends;
      - ASSIST waits for the operator to resolve or reject;
      - AUTONOMOUS goes through an Intent, the SA SMOS O1-CM handler, DME
        and RAN NF OAM. It writes CommonBeamformingFunction.digitalTilt or
        NRSectorCarrier.configuredMaxTxPower.
  * A **revert** (the cluster got worse) or **rollback** (a write failed or
    didn't verify) restores the previous settings, so it goes straight to
    DME `/actions`, with its own `actionId`.
"""

import datetime
import json
import time
import uuid
from typing import cast

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_sdk import AiRuntimeSdk, SdkError
from smo_sdk.data import AlarmScope
from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import install_health
from smo_shared.correlation import apply_correlation_id, get_correlation_id
from smo_shared.db import get_session
from smo_shared.r1_client import R1Client
from smo_shared.timeutil import as_utc

from . import engine, producer  # producer: the sample data generator, also used by tests and demo.py
from .model import EmulationLogic, InferenceLogic, TrainingLogic, ValidationLogic
from .model.CoverageModel import MODEL_TYPE, CoverageModel, excess
from .model.series import by_cell, counters, parse_time, shares, snapshot
from .models import CoverageCell, CoverageDecision, CoverageInstance
from .producer import SimPublishRequest, publish_sim, register_sim_type, router as producer_router

app = FastAPI(title="Coverage Optimization rApp")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
apply_correlation_id(app)

_r1 = R1Client()
sdk = AiRuntimeSdk(_r1)

RAPP_ID = "coverage-optimization-rapp"

# The fulfilment report of a dispatched Intent is not there when the dispatch answers: Intent Service queues the push to the handler (an outbox row, sent
# right after the commit with a 2 s timeout, then retried by the delivery sweep every few seconds), and the handler enacts the change and publishes the
# report afterwards. `_intent_actions` therefore polls for it, at most this long, every this often, before it counts the change as not enacted.
INTENT_REPORT_WAIT_SECONDS = 30.0
INTENT_REPORT_POLL_SECONDS = 0.5
DATASET, SIM_DATASET = "COVERAGE_PERFORMANCE", "COVERAGE_PERFORMANCE_SIM"
NODE_GROUPS = ["coverage-optimization"]
TILT_TARGET = "CommonBeamformingFunction.digitalTilt"
POWER_TARGET = "NRSectorCarrier.configuredMaxTxPower"
KNOBS = {"digitalTilt": ("CommonBeamformingFunction", TILT_TARGET),
         "configuredMaxTxPower": ("NRSectorCarrier", POWER_TARGET)}


class RappError(Exception):
    """An error this rApp raises on purpose; `_rapp_error` answers it as `{detail: {title, status, detail}}` with `status` as the HTTP status."""
    def __init__(self, status: int, title: str, detail: str):
        self.status, self.title, self.detail = status, title, detail


@app.exception_handler(RappError)
def _rapp_error(_, exc: RappError):
    return JSONResponse(status_code=exc.status, content={"detail": {"title": exc.title, "status": exc.status, "detail": exc.detail}})


@app.exception_handler(SdkError)
def _sdk_error(_, exc: SdkError):
    body = exc.body if isinstance(exc.body, dict) else {"detail": exc.body}
    detail = body.get("detail", body)
    return JSONResponse(status_code=exc.status_code if exc.status_code < 500 else 502,
                        content={"detail": detail if isinstance(detail, dict) else {"title": "PLATFORM_ERROR", "detail": detail}})


# ---------------------------------------------------------------- helpers

def _instance(db: Session, instance_id: uuid.UUID) -> CoverageInstance:
    inst = db.get(CoverageInstance, instance_id)
    if inst is None:
        raise RappError(404, "INSTANCE_NOT_STARTED", f"instance {instance_id} is not started on this rApp")
    return inst


def _cells(db: Session, inst: CoverageInstance) -> dict[str, CoverageCell]:
    """Returns the instance's cell rows by cell id, adding (not committing) a row for any configured cell that has none yet; the caller commits."""
    rows = {r.cell_id: r for r in db.scalars(select(CoverageCell).where(CoverageCell.instance_id == inst.instance_id))}
    for cell in inst.cells:
        if cell not in rows:
            rows[cell] = CoverageCell(instance_id=inst.instance_id, cell_id=cell)
            db.add(rows[cell])
    return rows


def _consumer(inst: CoverageInstance) -> str:
    return f"{RAPP_ID}:{inst.instance_id}"


def _read(inst: CoverageInstance, mfr: str) -> dict | None:
    try:
        return sdk.data.read_config(inst.managed_element_ref, mfr)["attributes"]
    except SdkError:
        return None


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _read_setting(inst: CoverageInstance, cell: str) -> dict:
    """The cell's live {digitalTilt, configuredMaxTxPower}, read over O1."""
    beam = _read(inst, f"CommonBeamformingFunction={cell}") or {}
    carrier = _read(inst, f"NRSectorCarrier={cell}") or {}
    return {"digitalTilt": _int(beam.get("digitalTilt")), "configuredMaxTxPower": _int(carrier.get("configuredMaxTxPower"))}


def _o1_asleep(inst: CoverageInstance, cell: str) -> bool:
    du = _read(inst, f"NRCellDU={cell}") or {}
    ces = _read(inst, f"CESManagementFunction={cell}") or {}
    return du.get("administrativeState") == "LOCKED" or ces.get("energySavingState") == "IS_ENERGY_SAVING"


def _verify(inst: CoverageInstance, settings: dict[str, dict]) -> dict:
    observed = {cell: _read_setting(inst, cell) for cell in settings}
    ok = all(observed[c][k] == v for c, s in settings.items() for k, v in s.items())
    return {"result": "VERIFIED" if ok else "VERIFY_FAILED", "expected": settings, "observed": observed}


def _changes(inst: CoverageInstance, settings: dict[str, dict]) -> list[dict]:
    return [{"managedElementRef": inst.managed_element_ref, "className": KNOBS[k][0],
             "managedFunctionRef": f"{KNOBS[k][0]}={cell}", "attributeChanges": {k: v}}
            for cell, s in settings.items() for k, v in s.items()]


def _decision(inst, execution_id: str, reason: str) -> dict:
    """PR-AI-13: why this write is made, kept by RAN NF OAM as the decision record of the config job (and shown to a person who is asked to approve it): a
    reference to this execution (its inputs are on the instance's operator page, not in the record), the version of the model that decided, and the reason in words.
    The direct path of this rApp is a service-restoring write (a wake, a revert, a rollback, an operator override)."""
    wanted = {"inputsRef": f"{RAPP_ID}:{inst.instance_id}:execution:{execution_id}", "modelVersion": inst.model_version,
              "rationale": f"Restoring service: {reason}"}
    return {k: v for k, v in wanted.items() if v}


def _execute_direct(inst: CoverageInstance, settings: dict[str, dict], execution_id: str, reason: str) -> dict:
    """Sends one write straight to DME `/actions` with a fresh actionId and returns {path, actionId, status, ...}.

    A platform error is returned as status REJECTED with the error body, not raised, so the caller's verification read decides what happened.
    """
    action_id = str(uuid.uuid4())
    try:
        result = sdk.platform.execute_action(
            f"{RAPP_ID}:{inst.instance_id}", _changes(inst, settings), action_id=action_id,
            source_context={"rApp": RAPP_ID, "correlationId": execution_id, "reason": reason},
            decision=_decision(inst, execution_id, reason))
        return {"path": "DME_DIRECT", "actionId": result["actionId"], "forwardedJobId": result.get("forwardedJobId"),
                "status": result["status"]}
    except SdkError as e:
        return {"path": "DME_DIRECT", "actionId": action_id, "status": "REJECTED", "error": e.body}


def _restore(inst: CoverageInstance, settings: dict[str, dict], execution_id: str, reason: str) -> dict:
    """Write the given settings straight through DME, verify, and re-send
    once on a mismatch (used for reverts and rollbacks)."""
    pending = {}
    for cell, s in settings.items():
        live = _read_setting(inst, cell)
        diff = {k: v for k, v in s.items() if live.get(k) != v}
        if diff:
            pending[cell] = diff
    if not pending:
        return {"performed": False, "result": "ALREADY_RESTORED", "verification": _verify(inst, settings)}
    attempts = []
    for _ in range(2):
        action = _execute_direct(inst, pending, execution_id, reason)
        verification = _verify(inst, pending)
        attempts.append({"action": action, "verification": verification})
        if verification["result"] == "VERIFIED":
            break
    return {"performed": True, "result": attempts[-1]["verification"]["result"], "attempts": attempts}


def _intent_actions(intent_id: str) -> dict[str, dict]:
    """{expectationId: DME action} from the Intent's fulfilment report.
    Waits for it: it polls the reports until one carries `additionalFulfilmentInfo`, for at most `INTENT_REPORT_WAIT_SECONDS`; only then is the
    answer empty (the change counts as not enacted)."""
    deadline = time.monotonic() + INTENT_REPORT_WAIT_SECONDS
    while True:
        for report in sdk.intent.list_intent_reports(intent_id):
            info = (report["attributes"].get("intentFulfilmentReport") or {}).get("additionalFulfilmentInfo")
            if info:
                try:
                    return {a["expectationId"]: a for a in json.loads(info).get("actions", [])}
                except ValueError:
                    return {}
        if time.monotonic() >= deadline:
            return {}
        time.sleep(INTENT_REPORT_POLL_SECONDS)


def _changed_knob(d: CoverageDecision) -> tuple[str, int]:
    """The one knob a move changes, and its new value."""
    to_setting, from_setting = cast(dict, d.to_setting), cast(dict, d.from_setting)      # a move has both
    return next((k, v) for k, v in to_setting.items() if from_setting.get(k) != v)


def _expectation(inst: CoverageInstance, d: CoverageDecision) -> dict:
    """Builds the one TS 28.312 DELIVER expectation for a decision: the cell, and the knob that changes with its new value as an IS_EQUAL_TO
    target. Its id (`cco-<cell>-<first 8 of the execution id>`) is how the Intent's fulfilment report is matched back to the cell.
    """
    knob, value = _changed_knob(d)
    return {"expectationId": f"cco-{d.cell_id}-{d.execution_id[:8]}", "expectationVerb": "DELIVER",
            "expectationObject": {"objectType": "RAN_SUBNETWORK", "objectInstance": inst.managed_element_ref,
                                  "objectContexts": [{"contextAttribute": "Cell", "contextCondition": "IS_ALL_OF",
                                                      "contextValueRange": [d.cell_id]}]},
            "expectationTargets": [{"targetName": KNOBS[knob][1], "targetCondition": "IS_EQUAL_TO", "targetValueRange": value}]}


def _final(row: CoverageCell) -> dict:
    return {"state": row.state, "digitalTilt": row.tilt, "configuredMaxTxPower": row.power}


def _follow_dispatch(db: Session, inst: CoverageInstance, rows: dict[str, CoverageCell], decisions: dict[str, CoverageDecision],
                     dispatch: dict, execution_id: str, objective: dict) -> None:
    """Record what the dispatch did for each cell; verify, roll back, and
    put the verified cells under KPI observation as one change set."""
    intent = {"dispatchId": dispatch["dispatchId"], "autonomyMode": dispatch["autonomyMode"], "status": dispatch["status"],
              "intentId": dispatch.get("intentId"), "rejectedBy": dispatch.get("rejectedBy")}
    for d in decisions.values():
        d.intent = intent
    if dispatch["status"] in ("SHADOWED", "REJECTED", "AWAITING_SCOPE"):
        for cell, d in decisions.items():
            d.outcome = {"SHADOWED": "SHADOWED", "REJECTED": "REJECTED", "AWAITING_SCOPE": "AWAITING_APPROVAL"}[dispatch["status"]]
            d.final_state = _final(rows[cell])
        if dispatch["status"] == "AWAITING_SCOPE":
            inst.pending_dispatch = {"dispatchId": dispatch["dispatchId"], "objective": objective,
                                     "decisionIds": {c: str(d.decision_id) for c, d in decisions.items()}}
        return
    actions = _intent_actions(dispatch["intentId"])
    executed = {}
    for cell, d in decisions.items():
        row = rows[cell]
        action = actions.get(_expectation(inst, d)["expectationId"])
        d.action = {"path": "INTENT", **action} if action else {"path": "INTENT", "status": "NOT_ENACTED"}
        status = d.action.get("status")
        to_setting, from_setting = cast(dict, d.to_setting), cast(dict, d.from_setting)      # a move has both
        d.verification = _verify(inst, {cell: to_setting}) if status == "COMPLETED" else None
        if status == "COMPLETED" and cast(dict, d.verification)["result"] == "VERIFIED":      # verified just above
            row.state, row.tilt, row.power = engine.OBSERVING, to_setting["digitalTilt"], to_setting["configuredMaxTxPower"]
            row.last_changed_at = d.observed_at
            d.outcome = "EXECUTED"
            executed[cell] = {"from": d.from_setting, "to": d.to_setting, "move": d.decision, "decisionId": str(d.decision_id)}
        else:
            trigger = "VERIFY_FAILED" if status == "COMPLETED" else "PARTIAL_SUCCESS" if status == "PARTIAL_SUCCESS" else "ACTION_FAILED"
            d.rollback = {"trigger": trigger, **_restore(inst, {cell: from_setting}, execution_id, f"ROLLBACK:{trigger}")}
            row.state, row.tilt, row.power = engine.STEADY, from_setting["digitalTilt"], from_setting["configuredMaxTxPower"]
            d.outcome = f"{trigger}_ROLLED_BACK" if d.rollback["result"] in ("VERIFIED", "ALREADY_RESTORED") \
                else f"{trigger}_ROLLBACK_FAILED"
        d.final_state = _final(row)
        d.updated_at = datetime.datetime.now(datetime.UTC)
    if executed:
        at = max(d.observed_at for d in decisions.values() if d.observed_at)
        inst.observing = {"changeSetId": str(uuid.uuid4()), "executionId": execution_id, "at": at.isoformat(),
                          "cells": executed, **objective}


def _reconcile(db: Session, inst: CoverageInstance) -> list[dict]:
    """Settles the instance's pending ASSIST dispatch once the operator has resolved it.

    Returns [] when nothing is pending or the dispatch is still AWAITING_SCOPE; otherwise follows the dispatch (verify, roll back, start
    observing), commits, and returns one summary with each cell's outcome.
    """
    pending = inst.pending_dispatch
    if not pending:
        return []
    dispatch = sdk.intent.get_autonomy_dispatch(pending["dispatchId"])
    if dispatch["status"] == "AWAITING_SCOPE":
        return []
    rows = _cells(db, inst)
    decisions = {c: db.get_one(CoverageDecision, uuid.UUID(i)) for c, i in pending["decisionIds"].items()}
    inst.pending_dispatch = None
    _follow_dispatch(db, inst, rows, decisions, dispatch, next(iter(decisions.values())).execution_id, pending["objective"])
    db.commit()
    return [{"dispatchId": pending["dispatchId"], "status": dispatch["status"],
             "outcomes": {c: d.outcome for c, d in decisions.items()}}]


# ---------------------------------------------------------------- instance + datasets

install_health(app)  # /live, /ready and the /health alias (PR-ST-7)


@app.post("/instances/{instance_id}/start")
def start_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Binds this rApp to its rapp-mgmt instance and discovers its datasets.
    The instance config carries the managed element, the cluster's cells,
    the tilt/power baselines, and optionally the EnergySaving and Mobility
    instances to coordinate with."""
    resp = _r1.get(f"/rapp-mgmt/instances/{instance_id}")
    if resp.status_code != 200:
        raise RappError(404, "INSTANCE_NOT_FOUND", f"rapp-mgmt has no instance {instance_id}")
    info = resp.json()
    config = info.get("configuration") or {}
    if not config.get("managedElementRef") or not config.get("cells"):
        raise RappError(422, "INSTANCE_CONFIG_INVALID", "instance config needs managedElementRef and cells")
    inst = db.get(CoverageInstance, instance_id) or CoverageInstance(instance_id=instance_id)
    inst.package_id = uuid.UUID(info["packageId"]) if info.get("packageId") else None
    inst.managed_element_ref, inst.cells = config["managedElementRef"], [str(c) for c in config["cells"]]
    inst.baseline_tilt = int(config.get("baselineTilt", producer.BASELINE_TILT))
    inst.baseline_power = int(config.get("baselinePower", producer.BASELINE_POWER))
    inst.autonomy_mode, inst.rmih_id = info.get("autonomyMode", "SHADOW"), config.get("rmihId", "sa-smos")
    inst.energy_saving_instance_id = config.get("energySavingInstanceId")
    inst.mobility_instance_id = config.get("mobilityInstanceId")
    inst.operator_notification_uri = config.get("operatorNotificationUri")
    datasets = {}
    for stage, name in (("TRAINING", DATASET), ("INFERENCE", DATASET), ("EMULATION", SIM_DATASET)):
        found = sdk.data.get_dataset(name, _consumer(inst), lifecycle_stage=stage, max_records=1)
        datasets[stage] = {"dataset": name, "dmeTypeId": found["dmeTypeId"], "dataJobId": found["dataJobId"],
                           "sourceDomain": found["sourceDomain"]}
    inst.data_jobs = datasets
    db.add(inst)
    _cells(db, inst)
    db.commit()
    return _instance_view(inst)


@app.get("/instances")
def list_instances(db: Session = Depends(get_session)):
    # Lists every started instance, oldest first.
    return {"items": [_instance_view(i) for i in db.scalars(select(CoverageInstance).order_by(CoverageInstance.created_at))]}


@app.get("/instances/{instance_id}")
def get_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # Returns one instance; 404 INSTANCE_NOT_STARTED when `start` was never called for it.
    return _instance_view(_instance(db, instance_id))


def _instance_view(i: CoverageInstance) -> dict:
    """The JSON shape of an instance on every route that returns one."""
    return {"instanceId": str(i.instance_id), "packageId": str(i.package_id) if i.package_id else None,
            "managedElementRef": i.managed_element_ref, "cells": i.cells, "baselineTilt": i.baseline_tilt,
            "baselinePower": i.baseline_power, "autonomyMode": i.autonomy_mode, "rmihId": i.rmih_id,
            "energySavingInstanceId": i.energy_saving_instance_id, "mobilityInstanceId": i.mobility_instance_id,
            "observing": i.observing, "pendingDispatchId": (i.pending_dispatch or {}).get("dispatchId"),
            "datasets": i.data_jobs, "modelId": str(i.model_id) if i.model_id else None, "modelVersion": i.model_version,
            "artifactVersion": i.artifact_version, "model": i.model_params, "lifecycleJobs": i.lifecycle_jobs}


def _dataset(inst: CoverageInstance, stage: str) -> list[dict]:
    return sdk.data.get_dataset(SIM_DATASET if stage == "EMULATION" else DATASET, _consumer(inst), lifecycle_stage=stage)["records"]


def _jobs(inst: CoverageInstance, **updates) -> None:
    inst.lifecycle_jobs = {**(inst.lifecycle_jobs or {}), **updates}


# ---------------------------------------------------------------- lifecycle

@app.post("/instances/{instance_id}/lifecycle/train")
def train(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """TRAINING on MLTF: registered in MLMR, trained on history in which tilt
    and power varied, stored as an artifact."""
    inst = _instance(db, instance_id)
    version = inst.model_version or f"1.0.0-{str(inst.instance_id)[:8]}"
    if inst.model_id is None:
        inst.model_id = uuid.UUID(sdk.models.register_model(
            MODEL_TYPE, version, description="Joint tilt/power sensitivity model + cluster optimiser",
            input_data_type=DATASET, output_data_type="digitalTilt,configuredMaxTxPower", domain="CUSTOM",
            custom_domain="RAN_COVERAGE_CAPACITY")["modelId"])
        inst.model_version = version
    job = sdk.lifecycle.start_training(inst.model_id, RAPP_ID, package_id=inst.package_id,
                                       dme_data_job_ids=[inst.data_jobs["TRAINING"]["dataJobId"]])
    try:
        model, metrics = TrainingLogic.train(_dataset(inst, "TRAINING"), version=version)
    except ValueError as e:
        sdk.lifecycle.complete_training(job["trainingJobId"], False, metrics={"failureReason": str(e)})
        _jobs(inst, training=job["trainingJobId"])
        db.commit()
        raise RappError(422, "TRAINING_FAILED", str(e)) from e
    stored = sdk.models.store_model(MODEL_TYPE, version, model.to_artifact(), filename="coverage_model.zip")
    inst.artifact_version, inst.model_params = stored["artifactVersion"], model.to_dict()
    metrics["artifactVersion"] = stored["artifactVersion"]
    completed = sdk.lifecycle.complete_training(job["trainingJobId"], True, metrics=metrics,
                                                modelConfidenceIndication=int(model.confidence() * 100))
    _jobs(inst, training=job["trainingJobId"])
    db.commit()
    return {"modelId": str(inst.model_id), "trainingJobId": job["trainingJobId"], "status": completed["status"],
            "metrics": metrics, "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


@app.post("/instances/{instance_id}/lifecycle/validate")
def validate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # VALIDATION on MLVF: scores the stored model on the held-out part of the TRAINING dataset and records the job. Needs a trained model
    # (`inst.model_params`); 404 INSTANCE_NOT_STARTED for an unknown instance.
    inst = _instance(db, instance_id)
    job = sdk.lifecycle.start_validation(inst.model_id, RAPP_ID, package_id=inst.package_id,
                                         training_job_id=(inst.lifecycle_jobs or {}).get("training"),
                                         validation_criteria={"maxRmse": ValidationLogic.MAX_RMSE,
                                                              "minDirectionAccuracy": ValidationLogic.MIN_DIRECTION})
    model = CoverageModel.from_dict(cast(dict, inst.model_params))      # set by training
    passed, metrics = ValidationLogic.validate(model, _dataset(inst, "TRAINING"))
    completed = sdk.lifecycle.complete_validation(job["validationJobId"], passed, metrics=metrics)
    _jobs(inst, validation=job["validationJobId"])
    db.commit()
    return {"validationJobId": job["validationJobId"], "passed": passed, "status": completed["status"], "metrics": metrics,
            "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


@app.post("/instances/{instance_id}/lifecycle/emulate")
def emulate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """EMULATION on MLEF against the Digital Twin's clusters with injected faults."""
    inst = _instance(db, instance_id)
    job = sdk.lifecycle.start_emulation(inst.model_id, RAPP_ID, package_id=inst.package_id,
                                        emulation_criteria={"dataset": SIM_DATASET, "minMoveAccuracy": EmulationLogic.PASS_RATE})
    model = CoverageModel.from_dict(cast(dict, inst.model_params))      # set by training
    passed, metrics = EmulationLogic.emulate(model, _dataset(inst, "EMULATION"))
    completed = sdk.lifecycle.complete_emulation(job["emulationJobId"], passed, metrics=metrics)
    _jobs(inst, emulation=job["emulationJobId"])
    db.commit()
    return {"emulationJobId": job["emulationJobId"], "passed": passed, "status": completed["status"], "metrics": metrics,
            "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


@app.post("/instances/{instance_id}/lifecycle/deploy")
def deploy(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """MLIF deployment once the model is certified; the serving model is
    loaded from MLMR."""
    inst = _instance(db, instance_id)
    sdk.lifecycle.deploy_model(inst.model_id, NODE_GROUPS)
    sdk.lifecycle.deploy_runtime(inst.model_id, package_id=inst.package_id)
    sdk.lifecycle.activate_runtime(inst.model_id)
    artifact = sdk.models.download_artifact(inst.model_id, inst.artifact_version)
    inst.model_params = CoverageModel.from_artifact(artifact.content).to_dict()
    db.commit()
    return {"modelId": str(inst.model_id), "artifactVersion": inst.artifact_version, "model": inst.model_params,
            "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


# ---------------------------------------------------------------- the closed loop

def _peer(instance_id, resource: str) -> str:
    """The route, through the gateway, of a list another rApp publishes for its instance (coordination): `/rapps/{instanceId}/operator/...` reaches the
    operator API that instance registered at rApp Management (GUI-8.3). A peer that has not registered one answers 404, which is read as "publishes nothing"."""
    return f"/rapps/{instance_id}/operator/instances/{instance_id}/{resource}"



def _es_cells(inst: CoverageInstance) -> dict[str, dict]:
    """The EnergySaving rApp's published cell states (coordination, D10.3-4c)."""
    if not inst.energy_saving_instance_id:
        return {}
    resp = _r1.get(_peer(inst.energy_saving_instance_id, "cells"))
    return {c["cellId"]: c for c in resp.json().get("items", [])} if resp.status_code == 200 else {}


def _mro_observing(inst: CoverageInstance) -> dict[str, list[str]]:
    """{cell: [relations the Mobility rApp is observing that touch it]}."""
    if not inst.mobility_instance_id:
        return {}
    resp = _r1.get(_peer(inst.mobility_instance_id, "relations"))
    out: dict[str, list[str]] = {}
    for r in resp.json().get("items", []) if resp.status_code == 200 else []:
        if r.get("state") == "OBSERVING":
            for cell in (r["source"], r["target"]):
                out.setdefault(cell, []).append(r["relation"])
    return out


def _critical_alarms(inst: CoverageInstance) -> AlarmScope:
    """W10-alarm-cellref: the element's critical alarms by the cells they hold.
    An unreadable alarm list holds nothing, as before."""
    try:
        return sdk.data.query_critical_alarms(inst.managed_element_ref)
    except SdkError:
        return AlarmScope([])


def _record(db, inst, execution_id, cell, state, **kw) -> CoverageDecision:
    """Adds one audit row (CoverageDecision) for a cell, taking the observed time, report count and shares from its latest window; `outcome` starts
    as NONE and the caller sets it once the write is known. Not committed here.
    """
    s = state.get(cell) or {}
    d = CoverageDecision(execution_id=execution_id, instance_id=inst.instance_id, cell_id=cell, observed_at=s.get("observedAt"),
                         reports=s.get("total"), shares=s.get("shares"), outcome="NONE", **kw)
    db.add(d)
    return d


@app.post("/instances/{instance_id}/evaluate")
def evaluate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """One pass of the closed loop over the cluster. Its X-Correlation-ID is
    the execution id in the audit trail."""
    inst = _instance(db, instance_id)
    if not inst.model_id or not inst.model_params:
        raise RappError(409, "MODEL_NOT_DEPLOYED", "train, certify and deploy the model first")
    reconciled = _reconcile(db, inst)
    execution_id = get_correlation_id() or str(uuid.uuid4())
    rows = _cells(db, inst)
    state = snapshot(by_cell(_dataset(inst, "INFERENCE")), inst.cells)
    model = CoverageModel.from_dict(inst.model_params)
    now = max((s["observedAt"] for s in state.values()), default=None)
    decisions: dict[str, CoverageDecision] = {}
    out = {"executionId": execution_id, "instanceId": str(inst.instance_id), "autonomyMode": inst.autonomy_mode,
           "reconciled": reconciled, "plan": None, "kpi": None}

    if now is None:
        db.commit()
        return {**out, "decisions": []}

    if inst.pending_dispatch:
        # ASSIST: a change set is waiting for the operator — nothing else moves
        for cell in inst.cells:
            decisions[cell] = _record(db, inst, execution_id, cell, state, decision=engine.NO_CHANGE,
                                      reason="AWAITING_APPROVAL", final_state=_final(rows[cell]))
    elif inst.observing:
        decisions = _kpi_pass(db, inst, rows, state, now, execution_id, out)
    else:
        decisions = _plan_pass(db, inst, rows, state, model, now, execution_id, out)

    for row in rows.values():
        row.updated_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    return {**out, "decisions": [_decision_view(decisions[c]) for c in inst.cells if c in decisions]}


def _kpi_pass(db, inst, rows, state, now, execution_id, out) -> dict[str, CoverageDecision]:
    """KPI-verified revert of the change set under observation (D10.3-4b)."""
    obs = inst.observing
    kpi = engine.kpi_check({**obs, "at": parse_time(obs["at"])}, state, now)
    out["kpi"] = kpi
    decisions = {}
    if kpi is None:
        for cell in inst.cells:
            decisions[cell] = _record(db, inst, execution_id, cell, state, decision=engine.NO_CHANGE, reason="OBSERVING",
                                      final_state=_final(rows[cell]))
        return decisions
    for cell in inst.cells:
        change = obs["cells"].get(cell)
        if change is None:
            decisions[cell] = _record(db, inst, execution_id, cell, state, decision=engine.NO_CHANGE,
                                      reason="CHANGE_SET_REVIEW", kpi=kpi, final_state=_final(rows[cell]))
            continue
        row = rows[cell]
        if kpi["verdict"] == "DEGRADED":
            d = decisions[cell] = _record(db, inst, execution_id, cell, state, decision=engine.REVERT, reason="KPI_DEGRADED",
                                          kpi=kpi, from_setting=change["to"], to_setting=change["from"])
            restore = _restore(inst, {cell: change["from"]}, execution_id, "REVERT:KPI_DEGRADED")
            d.action = restore["attempts"][0]["action"] if restore["performed"] else None
            d.verification = restore["attempts"][-1]["verification"] if restore["performed"] else restore["verification"]
            ok = restore["result"] in ("VERIFIED", "ALREADY_RESTORED")
            d.outcome = "REVERTED" if ok else "REVERT_FAILED"
            if ok:
                row.tilt, row.power = change["from"]["digitalTilt"], change["from"]["configuredMaxTxPower"]
                row.last_changed_at = now
        else:
            d = decisions[cell] = _record(db, inst, execution_id, cell, state, decision=engine.NO_CHANGE,
                                          reason="CHANGE_CONFIRMED", kpi=kpi, from_setting=change["to"])
            d.outcome = "CONFIRMED"
        row.state = engine.STEADY
        d.final_state = _final(row)
    inst.observing = None
    return decisions


def _plan_pass(db, inst, rows, state, model, now, execution_id, out) -> dict[str, CoverageDecision]:
    """Guards, bounds and the joint plan; the change set goes through the autonomy dispatch."""
    guards = {g["cellId"]: g for g in sdk.data.query_cell_guards(managed_element_ref=inst.managed_element_ref)}
    es_cells, mro = _es_cells(inst), _mro_observing(inst)
    alarms = _critical_alarms(inst)
    asleep = {c for c in inst.cells if _o1_asleep(inst, c)} | {
        c for c, e in es_cells.items() if e.get("state") in ("SLEEP", "PRE_SLEEP")}
    woken = {c: parse_time(e["lastUnlockedAt"]) for c, e in es_cells.items() if e.get("lastUnlockedAt")}

    inputs, safety, live = [], {}, {}
    for cell in inst.cells:
        row = rows[cell]
        setting = _read_setting(inst, cell)
        tilt = setting["digitalTilt"] if setting["digitalTilt"] is not None else (row.tilt or inst.baseline_tilt)
        power = setting["configuredMaxTxPower"] if setting["configuredMaxTxPower"] is not None else (row.power or inst.baseline_power)
        row.tilt, row.power = tilt, power
        neighbours = sorted((state.get(cell) or {}).get("overlaps", {}))
        wakes = [woken[c] for c in [cell, *neighbours] if c in woken]
        c = engine.CellInput(
            cell=cell, total=(state.get(cell) or {}).get("total", 0.0), tilt=tilt, power=power,
            baseline_tilt=inst.baseline_tilt, baseline_power=inst.baseline_power, neighbours=neighbours,
            last_changed_at=as_utc(row.last_changed_at) if row.last_changed_at else None, guard=guards.get(cell, {}),
            critical_alarm=bool(alarms.holding([cell, *neighbours])), asleep=cell in asleep,
            asleep_neighbours=[n for n in neighbours if n in asleep],
            last_woken=max(wakes) if wakes else None, mro_observing=mro.get(cell, []))
        inputs.append(c)
        safety[cell] = {**engine.evaluate_guards(c, now), "esState": (es_cells.get(cell) or {}).get("state"),
                        "criticalAlarmIds": alarms.ids_holding([cell, *neighbours])}
        live[cell] = c

    allowed = {c.cell: engine.allowed_moves(c) for c in inputs if safety[c.cell]["passed"] and c.cell in state}
    job = sdk.lifecycle.request_inference(inst.model_id)
    inference = InferenceLogic.infer(model, state, allowed)
    resolved = sdk.lifecycle.resolve_inference(job["inferenceJobId"], True, inference_outputs=[
        {"aIMLInferenceName": MODEL_TYPE, "outputResult": {k: v for k, v in inference.items() if k != "predicted"}}])
    out["plan"] = {"moves": inference["plan"], "drivers": inference["drivers"], "objectiveBefore": inference["objectiveBefore"],
                   "objectiveAfter": inference["objectiveAfter"], "gain": inference["gain"],
                   "inferenceJobId": job["inferenceJobId"], "aimlInferenceReportId": resolved.get("aIMLInferenceReportId")}
    cell_decisions = engine.plan_cells(inputs, state, inference, now, safety)

    decisions, changes = {}, {}
    for cell in inst.cells:
        cd = cell_decisions[cell]
        prediction = {"plan": out["plan"], "predictedShares": inference["predicted"].get(cell),
                      "problem": (inference["cells"].get(cell) or {}).get("problem"), "confidence": inference["confidence"]}
        d = decisions[cell] = _record(db, inst, execution_id, cell, state, decision=cd.decision, reason=cd.reason,
                                      prediction=prediction, safety={**cd.safety, "allowedMoves": cd.allowed},
                                      from_setting=cd.from_setting, to_setting=cd.to_setting)
        if cd.decision == engine.NO_CHANGE:
            d.final_state = _final(rows[cell])
        else:
            changes[cell] = d
    db.flush()
    if changes:
        objective = {"preObjective": inference["objectiveBefore"], "predictedObjective": inference["objectiveAfter"]}
        dispatch = sdk.intent.request_autonomy_dispatch(
            inst.instance_id, [_expectation(inst, d) for d in changes.values()], inst.rmih_id, model_id=inst.model_id,
            notification_destination=inst.operator_notification_uri, user_label=f"coverage-optimization {execution_id}")
        _follow_dispatch(db, inst, rows, changes, dispatch, execution_id, objective)
    return decisions


@app.post("/instances/{instance_id}/reconcile")
def reconcile(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # Settles a pending ASSIST dispatch now (the same step `evaluate` runs first); returns {settled: [...]}, empty when there is nothing to
    # settle.
    return {"settled": _reconcile(db, _instance(db, instance_id))}


# ---------------------------------------------------------------- audit + dashboard

def _decision_view(d: CoverageDecision) -> dict:
    """The JSON shape of one audit row, as the decisions list, the evaluate answer and the dashboard return it."""
    return {"decisionId": str(d.decision_id), "executionId": d.execution_id, "cellId": d.cell_id,
            "observedAt": d.observed_at.isoformat() if d.observed_at else None, "reports": d.reports, "shares": d.shares,
            "prediction": d.prediction, "safety": d.safety, "decision": d.decision, "reason": d.reason,
            "fromSetting": d.from_setting, "toSetting": d.to_setting, "kpi": d.kpi, "intent": d.intent, "action": d.action,
            "verification": d.verification, "rollback": d.rollback, "outcome": d.outcome, "finalState": d.final_state,
            "createdAt": d.created_at.isoformat()}


@app.get("/instances/{instance_id}/decisions")
def list_decisions(instance_id: uuid.UUID, cell_id: str | None = None, execution_id: str | None = None,
                   limit: int = 100, db: Session = Depends(get_session)):
    # Audit rows of an instance, newest first; filtered by cell_id and execution_id when given; `limit` is capped at 500.
    stmt = select(CoverageDecision).where(CoverageDecision.instance_id == instance_id)
    if cell_id:
        stmt = stmt.where(CoverageDecision.cell_id == cell_id)
    if execution_id:
        stmt = stmt.where(CoverageDecision.execution_id == execution_id)
    rows = db.scalars(stmt.order_by(CoverageDecision.created_at.desc()).limit(min(limit, 500))).all()
    return {"items": [_decision_view(d) for d in rows]}


def _cell_view(r: CoverageCell) -> dict:
    return {"cellId": r.cell_id, "state": r.state, "digitalTilt": r.tilt, "configuredMaxTxPower": r.power,
            "lastChangedAt": r.last_changed_at.isoformat() if r.last_changed_at else None}


@app.get("/instances/{instance_id}/cells")
def list_cells(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # The instance's cells with their state and tilt/power; creates the missing rows and commits, so a freshly started instance lists all its
    # cells.
    inst = _instance(db, instance_id)
    rows = _cells(db, inst)
    db.commit()
    return {"items": [_cell_view(rows[c]) for c in inst.cells]}


@app.get("/instances/{instance_id}/dashboard")
def dashboard(instance_id: uuid.UUID, points: int = 48, db: Session = Depends(get_session)):
    """Per cell: the three problem shares' trends, the latest joint plan and
    prediction, the safety evaluation, decision, intent, action,
    verification, KPI check and the resulting tilt and power."""
    inst = _instance(db, instance_id)
    rows = _cells(db, inst)
    series = by_cell(_dataset(inst, "INFERENCE"))
    out = []
    for cell in inst.cells:
        latest = db.scalars(select(CoverageDecision).where(CoverageDecision.instance_id == instance_id,
                                                           CoverageDecision.cell_id == cell)
                            .order_by(CoverageDecision.created_at.desc()).limit(1)).first()
        trend = [{"t": t.isoformat(), **shares(counters(p))} for t, p in series.get(cell, [])[-points:]]
        out.append({**_cell_view(rows[cell]), "shareTrend": trend,
                    "excessTrend": [{"t": x["t"], "v": round(excess(cast(dict[str, float], x)), 3)} for x in trend],      # excess reads only the shares
                    "latestDecision": _decision_view(latest) if latest else None})
    db.commit()
    return {"instance": _instance_view(inst), "cells": out}


# ---------------------------------------------------------------- Digital Twin producer

@app.post("/sim-producer/register", status_code=201)
def register_sim_producer():
    # Registers the COVERAGE_PERFORMANCE_SIM data type at DME with this rApp as the Digital Twin producer (201).
    return register_sim_type(sdk)


@app.post("/sim-producer/publish")
def publish_sim_data(body: SimPublishRequest):
    # Generates the requested clusters' windows and delivers them to every data job of the sim type.
    return publish_sim(sdk, body)


app.include_router(producer_router)
