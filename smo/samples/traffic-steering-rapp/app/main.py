"""Traffic Steering rApp — the Wave 10.4 reference rApp
(HISTORY.md Wave 10.4, W10.4-01..10). A Non-RT RIC rApp that
moves load off congested cells. Idle UEs are steered with each cell's
reselection priority towards another frequency layer; connected UEs with the
cell individual offset of a neighbour relation. Like the other reference
rApps it uses O1 PM data only, and the R1 interface only through the AI
Runtime SDK; there is no A1, Near-RT RIC, xApp or E2.

The loop (POST /instances/{id}/evaluate), per source cell:

    Input (LOAD_PERFORMANCE) → Score + forecast → KPI check of the last change
          → Safety → Knob / target options → Pairwise plan
          → O1 execution → Verification → Rollback / Revert → Audit

How each kind of write reaches O1:

  * A **steering or release step** is governed by the instance's autonomy
    mode, through one AutonomyDispatch per pass, with one expectation per
    step:
      - SHADOW only recommends;
      - ASSIST waits for the operator to resolve or reject;
      - AUTONOMOUS goes through an Intent, the SA SMOS O1-CM handler, DME
        and RAN NF OAM. It writes NRFreqRelation.cellReselectionPriority or
        NRCellRelation.cellIndividualOffset.
  * A **revert** (the KPI check failed) or **rollback** (a write failed or
    didn't verify) restores the previous value, so it goes straight to DME
    `/actions`, with its own `actionId`.

The CIO is shared with the Mobility rApp (D10.4-1). Both keep it inside
baseline ± 6 dB, and neither changes a relation the other is observing. This
rApp publishes its own observed relations at GET /instances/{id}/relations.
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

from smo_sdk import AiRuntimeSdk, SdkError, instances
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
from .model.SteeringModel import MODEL_TYPE, SteeringModel
from .model.series import SAMPLES, by_cell, counters, ho_fail_rate, neighbours, parse_time, score
from .models import TrafficCell, TrafficDecision, TrafficInstance
from .producer import SimPublishRequest, publish_sim, register_sim_type, router as producer_router

app = FastAPI(title="Traffic Steering rApp")
instances.install(app)  # the credentials route rApp Management pushes to, and acting as the instance of each /instances/{id}/... request (call flow 01c)
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
apply_correlation_id(app)

_r1 = R1Client()
sdk = AiRuntimeSdk(_r1)

RAPP_ID = "traffic-steering-rapp"

# The fulfilment report of a dispatched Intent is not there when the dispatch answers: Intent Service queues the push to the handler (an outbox row, sent
# right after the commit with a 2 s timeout, then retried by the delivery sweep every few seconds), and the handler enacts the change and publishes the
# report afterwards. `_intent_actions` therefore polls for it, at most this long, every this often, before it counts the change as not enacted.
INTENT_REPORT_WAIT_SECONDS = 30.0
INTENT_REPORT_POLL_SECONDS = 0.5
DATASET, SIM_DATASET = "LOAD_PERFORMANCE", "LOAD_PERFORMANCE_SIM"
NODE_GROUPS = ["traffic-steering"]
CIO_TARGET = "NRCellRelation.cellIndividualOffset"
PRIO_TARGET = "NRFreqRelation.cellReselectionPriority"


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

def _instance(db: Session, instance_id: uuid.UUID) -> TrafficInstance:
    inst = db.get(TrafficInstance, instance_id)
    if inst is None:
        raise RappError(404, "INSTANCE_NOT_STARTED", f"instance {instance_id} is not started on this rApp")
    return inst


def _layers(inst: TrafficInstance) -> dict[str, str]:
    return {c["cellId"]: c["layer"] for c in inst.cells}


def _cells(db: Session, inst: TrafficInstance) -> dict[str, TrafficCell]:
    """Returns the instance's cell rows by cell id, adding (not committing) a row with no steering for any configured cell that has none yet; the
    caller commits.
    """
    rows = {r.cell_id: r for r in db.scalars(select(TrafficCell).where(TrafficCell.instance_id == inst.instance_id))}
    for cell in _layers(inst):
        if cell not in rows:
            rows[cell] = TrafficCell(instance_id=inst.instance_id, cell_id=cell, steering={"cio": {}, "prio": {}})
            db.add(rows[cell])
    return rows


def _consumer(inst: TrafficInstance) -> str:
    return f"{RAPP_ID}:{inst.instance_id}"


def _read(inst: TrafficInstance, mfr: str) -> dict | None:
    try:
        return sdk.data.read_config(inst.managed_element_ref, mfr)["attributes"]
    except SdkError:
        return None


def _parse_cio(raw) -> int | None:
    """The live CIO, if all six QOffsetRange entries agree."""
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if isinstance(values, list) and values and all(v == values[0] for v in values):
        return int(values[0])
    return None


def _value(inst: TrafficInstance, ref: str) -> int | None:
    """The live value of a steering knob (`NRCellRelation=…` CIO or `NRFreqRelation=…` priority)."""
    attrs = _read(inst, ref) or {}
    if ref.startswith("NRCellRelation="):
        return _parse_cio(attrs.get("cellIndividualOffset"))
    try:
        return int(cast(str, attrs.get("cellReselectionPriority")))      # a missing one is a TypeError, caught below
    except (TypeError, ValueError):
        return None


def _flag(attrs: dict, name: str) -> bool:
    return str(attrs.get(name, "true")).lower() != "false"


def _o1_asleep(inst: TrafficInstance, cell: str) -> bool:
    du = _read(inst, f"NRCellDU={cell}") or {}
    ces = _read(inst, f"CESManagementFunction={cell}") or {}
    return du.get("administrativeState") == "LOCKED" or ces.get("energySavingState") == "IS_ENERGY_SAVING"


def _wire(ref: str, value: int):
    return [value] * 6 if ref.startswith("NRCellRelation=") else value   # TS 28.541: six QOffsetRange entries, alike


def _change(inst: TrafficInstance, ref: str, value: int) -> dict:
    ioc = ref.split("=", 1)[0]
    attr = "cellIndividualOffset" if ioc == "NRCellRelation" else "cellReselectionPriority"
    return {"managedElementRef": inst.managed_element_ref, "className": ioc, "managedFunctionRef": ref,
            "attributeChanges": {attr: _wire(ref, value)}}


def _verify(inst: TrafficInstance, values: dict[str, int]) -> dict:
    observed = {ref: _value(inst, ref) for ref in values}
    return {"result": "VERIFIED" if all(observed[r] == v for r, v in values.items()) else "VERIFY_FAILED",
            "expected": values, "observed": observed}


def _decision(inst, execution_id: str, reason: str) -> dict:
    """PR-AI-13: why this write is made, kept by RAN NF OAM as the decision record of the config job (and shown to a person who is asked to approve it): a
    reference to this execution (its inputs are on the instance's operator page, not in the record), the version of the model that decided, and the reason in words.
    The direct path of this rApp is a service-restoring write (a wake, a revert, a rollback, an operator override)."""
    wanted = {"inputsRef": f"{RAPP_ID}:{inst.instance_id}:execution:{execution_id}", "modelVersion": inst.model_version,
              "rationale": f"Restoring service: {reason}"}
    return {k: v for k, v in wanted.items() if v}


def _execute_direct(inst: TrafficInstance, values: dict[str, int], execution_id: str, reason: str) -> dict:
    """Sends writes ({managed ref: value}) straight to DME `/actions` with a fresh actionId and returns {path, actionId, status, ...}.

    A platform error is returned as status REJECTED with the error body, not raised, so the caller's verification read decides what happened.
    """
    action_id = str(uuid.uuid4())
    try:
        result = sdk.platform.execute_action(
            f"{RAPP_ID}:{inst.instance_id}", [_change(inst, ref, v) for ref, v in values.items()], action_id=action_id,
            source_context={"rApp": RAPP_ID, "correlationId": execution_id, "reason": reason},
            decision=_decision(inst, execution_id, reason))
        return {"path": "DME_DIRECT", "actionId": result["actionId"], "forwardedJobId": result.get("forwardedJobId"),
                "status": result["status"]}
    except SdkError as e:
        return {"path": "DME_DIRECT", "actionId": action_id, "status": "REJECTED", "error": e.body}


def _restore(inst: TrafficInstance, values: dict[str, int], execution_id: str, reason: str) -> dict:
    """Write the given values straight through DME, verify, and re-send once
    on a mismatch (used for reverts and rollbacks)."""
    pending = {r: v for r, v in values.items() if _value(inst, r) != v}
    if not pending:
        return {"performed": False, "result": "ALREADY_RESTORED", "verification": _verify(inst, values)}
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


def _expectation(inst: TrafficInstance, d: TrafficDecision) -> dict:
    """Builds the one TS 28.312 DELIVER expectation for a steering step: the source cell's relation, the CIO or the idle priority as the target
    with its new value. Its id (`mlb-<cell>-<first 8 of the execution id>`) is how the Intent's fulfilment report is matched back to the cell.
    """
    ref, to_value = cast(str, d.managed_ref), cast(int, d.to_value)      # a step has both
    ioc, name = ref.split("=", 1)
    return {"expectationId": f"mlb-{d.cell_id}-{d.execution_id[:8]}", "expectationVerb": "DELIVER",
            "expectationObject": {"objectType": "RAN_SUBNETWORK", "objectInstance": inst.managed_element_ref,
                                  "objectContexts": [{"contextAttribute": "Cell", "contextCondition": "IS_ALL_OF",
                                                      "contextValueRange": [name]}]},
            "expectationTargets": [{"targetName": CIO_TARGET if ioc == "NRCellRelation" else PRIO_TARGET,
                                    "targetCondition": "IS_EQUAL_TO", "targetValueRange": _wire(ref, to_value)}]}


def _final(row: TrafficCell) -> dict:
    return {"state": row.state, "steering": row.steering}


def _bias(row: TrafficCell, d: TrafficDecision, sign: int) -> None:
    """Add (sign +1) or remove (sign −1) this decision's step from the
    steering this rApp has in force from the source cell."""
    steering = {"cio": dict((row.steering or {}).get("cio") or {}), "prio": dict((row.steering or {}).get("prio") or {})}
    targets, ref = cast(list, d.targets), cast(str, d.managed_ref)      # a step has both
    key, bucket = (targets[0], "cio") if d.knob == "CONNECTED" else (ref.rsplit("-", 1)[1], "prio")
    steering[bucket][key] = steering[bucket].get(key, 0) + sign * abs(cast(int, d.to_value) - cast(int, d.from_value))
    steering[bucket] = {k: v for k, v in steering[bucket].items() if v > 0}
    row.steering = steering


def _settle(inst: TrafficInstance, row: TrafficCell, d: TrafficDecision, ok: bool, execution_id: str) -> None:
    """Records the end state of one cell's step on its row and audit row.

    When `ok`, the step's bias is added to (steering) or removed from (release) the steering in force, the change time is kept for pacing, and a
    steering step puts the cell under observation and into the steering log (the anti-oscillation source). Otherwise the outcome names the
    rollback trigger and whether the rollback was verified, and the row's steering is left as it was.
    """
    steering = d.decision.startswith("STEER_")
    if ok:
        _bias(row, d, +1 if steering else -1)
        observed_at = cast(datetime.datetime, d.observed_at)      # a step was decided on an observation
        row.last_changed_at = observed_at
        d.outcome = "EXECUTED"
        if steering:
            row.state = engine.OBSERVING
            row.last_change = {"at": observed_at.isoformat(), "source": d.cell_id, "knob": d.knob, "ref": d.managed_ref,
                               "targets": d.targets, "from": d.from_value, "to": d.to_value, "decisionId": str(d.decision_id),
                               **(d.prediction or {}).get("kpiBaseline", {})}
            inst.steering_log = [*(inst.steering_log or []),
                                 {"source": d.cell_id, "targets": d.targets, "at": observed_at.isoformat()}]
    else:
        trigger = (d.rollback or {}).get("trigger", "ACTION_FAILED")
        d.outcome = f"{trigger}_ROLLED_BACK" if d.rollback and d.rollback["result"] in ("VERIFIED", "ALREADY_RESTORED") \
            else f"{trigger}_ROLLBACK_FAILED"
    d.final_state = _final(row)
    d.updated_at = datetime.datetime.now(datetime.UTC)


def _follow_dispatch(inst: TrafficInstance, rows: dict[str, TrafficCell], decisions: dict[str, TrafficDecision],
                     dispatch: dict, execution_id: str) -> None:
    """Settles the cells of one AutonomyDispatch according to where it is.

    SHADOWED, REJECTED and AWAITING_SCOPE only record the outcome (ASSIST remembers the pending dispatch on the instance); a dispatched step is
    matched to the Intent's action report, verified by an O1 read-back, and rolled back through DME when the action did not complete or the
    read-back differs.
    """
    intent = {"dispatchId": dispatch["dispatchId"], "autonomyMode": dispatch["autonomyMode"], "status": dispatch["status"],
              "intentId": dispatch.get("intentId"), "rejectedBy": dispatch.get("rejectedBy")}
    for d in decisions.values():
        d.intent = intent
    if dispatch["status"] in ("SHADOWED", "REJECTED", "AWAITING_SCOPE"):
        for cell, d in decisions.items():
            d.outcome = {"SHADOWED": "SHADOWED", "REJECTED": "REJECTED", "AWAITING_SCOPE": "AWAITING_APPROVAL"}[dispatch["status"]]
            d.final_state = _final(rows[cell])
        if dispatch["status"] == "AWAITING_SCOPE":
            inst.pending_dispatch = {"dispatchId": dispatch["dispatchId"],
                                     "decisionIds": {c: str(d.decision_id) for c, d in decisions.items()}}
        return
    actions = _intent_actions(dispatch["intentId"])
    for cell, d in decisions.items():
        action = actions.get(_expectation(inst, d)["expectationId"])
        d.action = {"path": "INTENT", **action} if action else {"path": "INTENT", "status": "NOT_ENACTED"}
        status = d.action.get("status")
        ref, to_value, from_value = cast(str, d.managed_ref), cast(int, d.to_value), cast(int, d.from_value)      # a step has all three
        d.verification = _verify(inst, {ref: to_value}) if status == "COMPLETED" else None
        ok = status == "COMPLETED" and cast(dict, d.verification)["result"] == "VERIFIED"      # verified just above
        if not ok:
            trigger = "VERIFY_FAILED" if status == "COMPLETED" else "PARTIAL_SUCCESS" if status == "PARTIAL_SUCCESS" else "ACTION_FAILED"
            d.rollback = {"trigger": trigger, **_restore(inst, {ref: from_value}, execution_id, f"ROLLBACK:{trigger}")}
        _settle(inst, rows[cell], d, ok, execution_id)


def _reconcile(db: Session, inst: TrafficInstance) -> list[dict]:
    """Settles the instance's pending ASSIST dispatch once the operator has resolved it.

    Returns [] when nothing is pending or the dispatch is still AWAITING_SCOPE; otherwise follows the dispatch, commits, and returns one summary
    with each cell's outcome.
    """
    pending = inst.pending_dispatch
    if not pending:
        return []
    dispatch = sdk.intent.get_autonomy_dispatch(pending["dispatchId"])
    if dispatch["status"] == "AWAITING_SCOPE":
        return []
    rows = _cells(db, inst)
    decisions = {c: db.get_one(TrafficDecision, uuid.UUID(i)) for c, i in pending["decisionIds"].items()}
    inst.pending_dispatch = None
    _follow_dispatch(inst, rows, decisions, dispatch, next(iter(decisions.values())).execution_id)
    db.commit()
    return [{"dispatchId": pending["dispatchId"], "status": dispatch["status"],
             "outcomes": {c: d.outcome for c, d in decisions.items()}}]


# ---------------------------------------------------------------- instance + datasets

install_health(app)  # /live, /ready and the /health alias (PR-ST-7)


@app.post("/instances/{instance_id}/start")
def start_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Binds this rApp to its rapp-mgmt instance and discovers its datasets.
    The instance config carries the managed element, the cells and their
    frequency layers ([{cellId, layer}]), the CIO / priority baselines, and
    optionally the EnergySaving, Mobility and Coverage instances to
    coordinate with."""
    resp = _r1.get(f"/rapp-mgmt/instances/{instance_id}")
    if resp.status_code != 200:
        raise RappError(404, "INSTANCE_NOT_FOUND", f"rapp-mgmt has no instance {instance_id}")
    info = resp.json()
    config = info.get("configuration") or {}
    cells = config.get("cells") or []
    if not config.get("managedElementRef") or not cells or not all({"cellId", "layer"} <= set(c) for c in cells):
        raise RappError(422, "INSTANCE_CONFIG_INVALID", "instance config needs managedElementRef and cells [{cellId, layer}]")
    inst = db.get(TrafficInstance, instance_id) or TrafficInstance(instance_id=instance_id)
    inst.package_id = uuid.UUID(info["packageId"]) if info.get("packageId") else None
    inst.managed_element_ref = config["managedElementRef"]
    inst.cells = [{"cellId": str(c["cellId"]), "layer": c["layer"]} for c in cells]
    inst.baseline_cio = int(config.get("baselineCio", producer.BASELINE_CIO))
    inst.baseline_priority = int(config.get("baselinePriority", producer.BASELINE_PRIORITY))
    inst.autonomy_mode, inst.rmih_id = info.get("autonomyMode", "SHADOW"), config.get("rmihId", "sa-smos")
    inst.energy_saving_instance_id = config.get("energySavingInstanceId")
    inst.mobility_instance_id = config.get("mobilityInstanceId")
    inst.coverage_instance_id = config.get("coverageInstanceId")
    inst.operator_notification_uri = config.get("operatorNotificationUri")
    inst.steering_log = inst.steering_log or []
    datasets = {}
    for stage, name in (("TRAINING", DATASET), ("INFERENCE", DATASET), ("EMULATION", SIM_DATASET)):
        found = sdk.data.get_dataset(name, _consumer(inst), lifecycle_stage=stage, max_records=1)
        datasets[stage] = {"dataset": name, "dmeTypeId": found["dmeTypeId"], "dataJobId": found["dataJobId"],
                           "sourceDomain": found["sourceDomain"]}
    inst.data_jobs = datasets
    db.add(inst)
    _cells(db, inst)
    db.commit()
    # call flow 01d: the rApp reports the instance up itself, as the instance (its credentials were pushed at CreateInstance)
    return {**_instance_view(inst), "bootstrap": instances.bootstrap(sdk, str(instance_id))}


@app.get("/instances")
def list_instances(db: Session = Depends(get_session)):
    # Lists every started instance, oldest first.
    return {"items": [_instance_view(i) for i in db.scalars(select(TrafficInstance).order_by(TrafficInstance.created_at))]}


@app.get("/instances/{instance_id}")
def get_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # Returns one instance; 404 INSTANCE_NOT_STARTED when `start` was never called for it.
    return _instance_view(_instance(db, instance_id))


def _instance_view(i: TrafficInstance) -> dict:
    """The JSON shape of an instance on every route that returns one."""
    return {"instanceId": str(i.instance_id), "packageId": str(i.package_id) if i.package_id else None,
            "managedElementRef": i.managed_element_ref, "cells": i.cells, "baselineCio": i.baseline_cio,
            "baselinePriority": i.baseline_priority, "autonomyMode": i.autonomy_mode, "rmihId": i.rmih_id,
            "energySavingInstanceId": i.energy_saving_instance_id, "mobilityInstanceId": i.mobility_instance_id,
            "coverageInstanceId": i.coverage_instance_id, "steeringLog": i.steering_log,
            "pendingDispatchId": (i.pending_dispatch or {}).get("dispatchId"), "datasets": i.data_jobs,
            "modelId": str(i.model_id) if i.model_id else None, "modelVersion": i.model_version,
            "artifactVersion": i.artifact_version, "model": i.model_params, "lifecycleJobs": i.lifecycle_jobs}


def _dataset(inst: TrafficInstance, stage: str) -> list[dict]:
    return sdk.data.get_dataset(SIM_DATASET if stage == "EMULATION" else DATASET, _consumer(inst), lifecycle_stage=stage)["records"]


def _jobs(inst: TrafficInstance, **updates) -> None:
    inst.lifecycle_jobs = {**(inst.lifecycle_jobs or {}), **updates}


# ---------------------------------------------------------------- lifecycle

@app.post("/instances/{instance_id}/lifecycle/train")
def train(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """TRAINING on MLTF: registered in MLMR, trained on load history in which
    the biases varied, stored as an artifact."""
    inst = _instance(db, instance_id)
    version = inst.model_version or f"1.0.0-{str(inst.instance_id)[:8]}"
    if inst.model_id is None:
        inst.model_id = uuid.UUID(sdk.models.register_model(
            MODEL_TYPE, version, description="Congestion-score forecast + learned steering transfer, pairwise planner",
            input_data_type=DATASET, output_data_type="cellReselectionPriority,cellIndividualOffset", domain="CUSTOM",
            custom_domain="RAN_LOAD_BALANCING")["modelId"])
        inst.model_version = version
    job = sdk.lifecycle.start_training(inst.model_id, RAPP_ID, package_id=inst.package_id,
                                       dme_data_job_ids=[inst.data_jobs["TRAINING"]["dataJobId"]])
    try:
        model, metrics = TrainingLogic.train(_dataset(inst, "TRAINING"), _layers(inst), version=version)
    except ValueError as e:
        sdk.lifecycle.complete_training(job["trainingJobId"], False, metrics={"failureReason": str(e)})
        _jobs(inst, training=job["trainingJobId"])
        db.commit()
        raise RappError(422, "TRAINING_FAILED", str(e)) from e
    stored = sdk.models.store_model(MODEL_TYPE, version, model.to_artifact(), filename="steering_model.zip")
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
                                         validation_criteria={"minScore": ValidationLogic.PASS_THRESHOLD})
    model = SteeringModel.from_dict(cast(dict, inst.model_params))      # set by training
    passed, metrics = ValidationLogic.validate(model, _dataset(inst, "TRAINING"), _layers(inst))
    completed = sdk.lifecycle.complete_validation(job["validationJobId"], passed, metrics=metrics)
    _jobs(inst, validation=job["validationJobId"])
    db.commit()
    return {"validationJobId": job["validationJobId"], "passed": passed, "status": completed["status"], "metrics": metrics,
            "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


@app.post("/instances/{instance_id}/lifecycle/emulate")
def emulate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """EMULATION on MLEF against the Digital Twin's clusters with injected hotspots."""
    inst = _instance(db, instance_id)
    job = sdk.lifecycle.start_emulation(inst.model_id, RAPP_ID, package_id=inst.package_id,
                                        emulation_criteria={"dataset": SIM_DATASET, "minSteeringAccuracy": EmulationLogic.PASS_RATE})
    model = SteeringModel.from_dict(cast(dict, inst.model_params))      # set by training
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
    inst.model_params = SteeringModel.from_artifact(artifact.content).to_dict()
    db.commit()
    return {"modelId": str(inst.model_id), "artifactVersion": inst.artifact_version, "model": inst.model_params,
            "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


# ---------------------------------------------------------------- the closed loop

def _peer(instance_id, resource: str) -> str:
    """The route, through the gateway, of a list another rApp publishes for its instance (coordination): `/rapps/{instanceId}/operator/...` reaches the
    operator API that instance registered at rApp Management (GUI-8.3). A peer that has not registered one answers 404, which is read as "publishes nothing"."""
    return f"/rapps/{instance_id}/operator/instances/{instance_id}/{resource}"



def _items(path: str | None) -> list[dict]:
    if not path:
        return []
    resp = _r1.get(path)
    return resp.json().get("items", []) if resp.status_code == 200 else []


def _coordination(inst: TrafficInstance) -> dict:
    """What the other rApps publish (D10.4-4c), read over R1."""
    es = {c["cellId"]: c for c in _items(inst.energy_saving_instance_id
                                         and _peer(inst.energy_saving_instance_id, "cells"))}
    mro = {r["relation"] for r in _items(inst.mobility_instance_id
                                         and _peer(inst.mobility_instance_id, "relations"))
           if r.get("state") == "OBSERVING"}
    cco = {c["cellId"] for c in _items(inst.coverage_instance_id
                                       and _peer(inst.coverage_instance_id, "cells"))
           if c.get("state") == "OBSERVING"}
    return {"es": es, "mroObserving": mro, "ccoObserving": cco}


def _critical_alarms(inst: TrafficInstance) -> AlarmScope:
    """W10-alarm-cellref: the element's critical alarms by the cells they hold.
    An unreadable alarm list holds nothing, as before."""
    try:
        return sdk.data.query_critical_alarms(inst.managed_element_ref)
    except SdkError:
        return AlarmScope([])


def _record(db, inst, execution_id, cell, state, **kw) -> TrafficDecision:
    """Adds one audit row (TrafficDecision) for a cell, taking the observed time, score and forecast from its latest inference; `outcome` starts as
    NONE and the caller sets it once the write is known. Not committed here.
    """
    s = state.get(cell) or {}
    d = TrafficDecision(execution_id=execution_id, instance_id=inst.instance_id, cell_id=cell,
                        observed_at=parse_time(s["observedAt"]) if s.get("observedAt") else None,
                        score=s.get("score"), forecast=s.get("forecast"), outcome="NONE", **kw)
    db.add(d)
    return d


@app.post("/instances/{instance_id}/evaluate")
def evaluate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """One pass of the closed loop over the instance's cells. Its
    X-Correlation-ID is the execution id in the audit trail."""
    inst = _instance(db, instance_id)
    if not inst.model_id or not inst.model_params:
        raise RappError(409, "MODEL_NOT_DEPLOYED", "train, certify and deploy the model first")
    reconciled = _reconcile(db, inst)
    execution_id = get_correlation_id() or str(uuid.uuid4())
    rows = _cells(db, inst)
    model = SteeringModel.from_dict(inst.model_params)
    series = by_cell(_dataset(inst, "INFERENCE"))
    layers = _layers(inst)
    state = {c: out for c in layers if (out := InferenceLogic.infer(model, c, series.get(c, [])))}
    latest = {c: counters(series[c][-1][1]) for c in state}
    out = {"executionId": execution_id, "instanceId": str(inst.instance_id), "autonomyMode": inst.autonomy_mode,
           "reconciled": reconciled}
    if not state:
        db.commit()
        return {**out, "decisions": []}
    now = max(parse_time(s["observedAt"]) for s in state.values())
    decisions: dict[str, TrafficDecision] = {}

    if inst.pending_dispatch:
        # ASSIST: steps are waiting for the operator — nothing else moves
        for cell in layers:
            decisions[cell] = _record(db, inst, execution_id, cell, state, decision=engine.NO_CHANGE,
                                      reason="AWAITING_APPROVAL", final_state=_final(rows[cell]))
    else:
        planning = []
        for cell in layers:
            row = rows[cell]
            if row.state == engine.OBSERVING and row.last_change:
                decisions[cell] = _kpi(db, inst, row, state, latest, now, execution_id)
            elif cell in state:
                planning.append(cell)
            else:
                decisions[cell] = _record(db, inst, execution_id, cell, state, decision=engine.NO_CHANGE, reason="NO_DATA",
                                          final_state=_final(row))
        decisions.update(_plan(db, inst, rows, model, state, latest, planning, now, execution_id))

    for row in rows.values():
        row.updated_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    return {**out, "decisions": [_decision_view(decisions[c]) for c in layers if c in decisions]}


def _kpi(db, inst, row, state, latest, now, execution_id) -> TrafficDecision:
    """KPI-verified revert of a cell's last steering step (D10.4-4b)."""
    change = {**row.last_change, "at": parse_time(row.last_change["at"])}
    ho = ho_fail_rate(latest.get(row.cell_id, {}), change["targets"][0]) if change["knob"] == "CONNECTED" else None
    kpi = engine.kpi_check(change, {c: s["score"] for c, s in state.items()}, ho, now)
    common = dict(knob=change["knob"], managed_ref=change["ref"], targets=change["targets"])
    if kpi is None:
        return _record(db, inst, execution_id, row.cell_id, state, decision=engine.NO_CHANGE, reason="OBSERVING",
                       final_state=_final(row), **common)
    if kpi["verdict"] == "DEGRADED":
        d = _record(db, inst, execution_id, row.cell_id, state, decision=engine.REVERT,
                    reason="KPI_DEGRADED:" + ",".join(kpi["causes"]), kpi=kpi, from_value=change["to"],
                    to_value=change["from"], **common)
        restore = _restore(inst, {change["ref"]: change["from"]}, execution_id, "REVERT:KPI_DEGRADED")
        d.action = restore["attempts"][0]["action"] if restore["performed"] else None
        d.verification = restore["attempts"][-1]["verification"] if restore["performed"] else restore["verification"]
        ok = restore["result"] in ("VERIFIED", "ALREADY_RESTORED")
        d.outcome = "REVERTED" if ok else "REVERT_FAILED"
        if ok:
            stepped = TrafficDecision(cell_id=row.cell_id, knob=change["knob"], managed_ref=change["ref"],
                                      targets=change["targets"], from_value=change["from"], to_value=change["to"])
            _bias(row, stepped, -1)
            row.last_changed_at = now
    else:
        d = _record(db, inst, execution_id, row.cell_id, state, decision=engine.NO_CHANGE, reason="CHANGE_CONFIRMED",
                    kpi=kpi, from_value=change["from"], to_value=change["to"], **common)
        d.outcome = "CONFIRMED"
    row.state, row.last_change = engine.STEADY, None
    d.final_state = _final(row)
    return d


def _plan(db, inst, rows, model, state, latest, planning, now, execution_id) -> dict[str, TrafficDecision]:
    """Guards, options and the pairwise plan; the steps go through the autonomy dispatch."""
    if not planning:
        return {}
    layers = _layers(inst)
    guards = {g["cellId"]: g for g in sdk.data.query_cell_guards(managed_element_ref=inst.managed_element_ref)}
    coord, alarms = _coordination(inst), _critical_alarms(inst)
    asleep = {c for c in layers if _o1_asleep(inst, c)} | {
        c for c, e in coord["es"].items() if e.get("state") in ("SLEEP", "PRE_SLEEP")}
    woken = {c: parse_time(e["lastUnlockedAt"]) for c, e in coord["es"].items() if e.get("lastUnlockedAt")}
    horizon = now - engine.ANTI_OSCILLATION
    inst.steering_log = [e for e in (inst.steering_log or []) if parse_time(e["at"]) > horizon]
    protected = lambda c: guards.get(c, {}).get("cellClass") == "EMERGENCY" or bool(guards.get(c, {}).get("incidentZone"))  # noqa: E731

    sources, safety, opts = {}, {}, {}
    for cell in planning:
        row = rows[cell]
        nbrs = []
        for t in neighbours(latest[cell]):
            rel = _read(inst, f"NRCellRelation={cell}-{t}") or {}
            nbrs.append(engine.Neighbour(
                cell=t, layer=layers.get(t, "?"), protected=protected(t), asleep=t in asleep, last_woken=woken.get(t),
                coverage_observing=t in coord["ccoObserving"], critical_alarm=bool(alarms.holding([t])),
                cio=next((v for v in (_parse_cio(rel.get("cellIndividualOffset")),) if v is not None), inst.baseline_cio),
                ho_allowed=_flag(rel, "isHOAllowed"), mlb_allowed=_flag(rel, "isMLBAllowed"),
                mro_observing=f"{cell}-{t}" in coord["mroObserving"]))
        other_layers = sorted({n.layer for n in nbrs if n.layer != layers[cell]})
        steered_to_me: dict[str, datetime.datetime] = {}
        for e in inst.steering_log:
            if cell in e["targets"]:
                steered_to_me[e["source"]] = max(parse_time(e["at"]), steered_to_me.get(e["source"], horizon))
        s = engine.SourceInput(
            cell=cell, layer=layers[cell], samples=float(latest[cell].get(SAMPLES, 0)), baseline_cio=inst.baseline_cio,
            baseline_priority=inst.baseline_priority, neighbours=nbrs,
            priorities={L: v for L in other_layers if (v := _value(inst, f"NRFreqRelation={cell}-{L}")) is not None},
            steering=row.steering or {"cio": {}, "prio": {}}, guard=guards.get(cell, {}),
            critical_alarm=bool(alarms.holding([cell])),
            asleep=cell in asleep, coverage_observing=cell in coord["ccoObserving"],
            last_changed_at=as_utc(row.last_changed_at) if row.last_changed_at else None, steered_to_me=steered_to_me)
        sources[cell] = s
        safety[cell] = {**engine.source_guards(s, now), "criticalAlarmIds": alarms.ids_holding([cell]),
                        "esState": (coord["es"].get(cell) or {}).get("state")}
        opts[cell] = engine.options(s, now)

    candidates = {c: opts[c][0] for c in planning if safety[c]["passed"]}
    releases = {c: opts[c][1] for c in planning if safety[c]["passed"]}
    job = sdk.lifecycle.request_inference(inst.model_id)
    plan = model.plan(state, candidates, releases)
    resolved = sdk.lifecycle.resolve_inference(job["inferenceJobId"], True, inference_outputs=[
        {"aIMLInferenceName": MODEL_TYPE, "outputResult": {"cellId": c, **state[c], "plan": plan.get(c)}} for c in planning])

    decisions, changes = {}, {}
    for cell in planning:
        p = plan.get(cell) or {}
        prediction = {"model": state[cell], "plan": p, "inferenceJobId": job["inferenceJobId"],
                      "aimlInferenceReportId": resolved.get("aIMLInferenceReportId")}
        sfty = {**safety[cell], "excluded": opts[cell][2], "candidates": opts[cell][0]}
        if not safety[cell]["passed"]:
            decisions[cell] = _record(db, inst, execution_id, cell, state, decision=engine.NO_CHANGE, prediction=prediction,
                                      reason="SAFETY_BLOCKED:" + ",".join(b["guard"] for b in safety[cell]["blocks"]),
                                      safety=sfty, final_state=_final(rows[cell]))
            continue
        if p.get("decision", engine.NO_CHANGE) == engine.NO_CHANGE:
            decisions[cell] = _record(db, inst, execution_id, cell, state, decision=engine.NO_CHANGE, reason=p.get("reason", "NO_PLAN"),
                                      prediction=prediction, safety=sfty, final_state=_final(rows[cell]))
            continue
        move = p["move"]
        targets = [move["target"]] if move["knob"] == "CONNECTED" else move.get("targets") or [
            n.cell for n in sources[cell].neighbours if n.layer == move["layer"]]
        if p["decision"].startswith("STEER_"):
            prediction["kpiBaseline"] = {
                "preScore": state[cell]["score"], "preForecast": state[cell]["forecast"],
                "preTargets": {t: (state.get(t) or {}).get("score") for t in targets},
                "preHoFail": ho_fail_rate(latest[cell], targets[0]) if move["knob"] == "CONNECTED" else None}
        d = decisions[cell] = _record(db, inst, execution_id, cell, state, decision=p["decision"], reason=p["reason"],
                                      prediction=prediction, safety=sfty, knob=move["knob"], managed_ref=move["ref"],
                                      targets=targets, from_value=move["from"], to_value=move["to"])
        changes[cell] = d
    db.flush()
    if changes:
        dispatch = sdk.intent.request_autonomy_dispatch(
            inst.instance_id, [_expectation(inst, d) for d in changes.values()], inst.rmih_id, model_id=inst.model_id,
            notification_destination=inst.operator_notification_uri, user_label=f"traffic-steering {execution_id}")
        _follow_dispatch(inst, rows, changes, dispatch, execution_id)
    return decisions


@app.post("/instances/{instance_id}/reconcile")
def reconcile(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # Settles a pending ASSIST dispatch now (the same step `evaluate` runs first); returns {settled: [...]}, empty when there is nothing to
    # settle.
    return {"settled": _reconcile(db, _instance(db, instance_id))}


# ---------------------------------------------------------------- audit + dashboard

def _decision_view(d: TrafficDecision) -> dict:
    """The JSON shape of one audit row, as the decisions list, the evaluate answer and the dashboard return it."""
    return {"decisionId": str(d.decision_id), "executionId": d.execution_id, "cellId": d.cell_id,
            "observedAt": d.observed_at.isoformat() if d.observed_at else None, "score": d.score, "forecast": d.forecast,
            "prediction": d.prediction, "safety": d.safety, "decision": d.decision, "reason": d.reason, "knob": d.knob,
            "managedRef": d.managed_ref, "targets": d.targets, "fromValue": d.from_value, "toValue": d.to_value,
            "kpi": d.kpi, "intent": d.intent, "action": d.action, "verification": d.verification, "rollback": d.rollback,
            "outcome": d.outcome, "finalState": d.final_state, "createdAt": d.created_at.isoformat()}


@app.get("/instances/{instance_id}/decisions")
def list_decisions(instance_id: uuid.UUID, cell_id: str | None = None, execution_id: str | None = None,
                   limit: int = 100, db: Session = Depends(get_session)):
    # Audit rows of an instance, newest first; filtered by cell_id and execution_id when given; `limit` is capped at 500.
    stmt = select(TrafficDecision).where(TrafficDecision.instance_id == instance_id)
    if cell_id:
        stmt = stmt.where(TrafficDecision.cell_id == cell_id)
    if execution_id:
        stmt = stmt.where(TrafficDecision.execution_id == execution_id)
    rows = db.scalars(stmt.order_by(TrafficDecision.created_at.desc()).limit(min(limit, 500))).all()
    return {"items": [_decision_view(d) for d in rows]}


def _cell_view(inst: TrafficInstance, r: TrafficCell) -> dict:
    return {"cellId": r.cell_id, "layer": _layers(inst).get(r.cell_id), "state": r.state, "steering": r.steering,
            "lastChange": r.last_change, "lastChangedAt": r.last_changed_at.isoformat() if r.last_changed_at else None}


@app.get("/instances/{instance_id}/cells")
def list_cells(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # The instance's cells with their layer, state and steering in force; creates the missing rows and commits, so a freshly started instance
    # lists all its cells.
    inst = _instance(db, instance_id)
    rows = _cells(db, inst)
    db.commit()
    return {"items": [_cell_view(inst, rows[c]) for c in _layers(inst)]}


@app.get("/instances/{instance_id}/relations")
def list_relations(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """The neighbour relations this rApp has CIO steering on, and whether a
    change on each is under observation. The Mobility rApp reads this for
    the shared-CIO arbitration (D10.4-1)."""
    inst = _instance(db, instance_id)
    items = []
    for cell, row in _cells(db, inst).items():
        observing = row.state == engine.OBSERVING and (row.last_change or {}).get("knob") == "CONNECTED"
        observed_target = (row.last_change or {}).get("targets", [None])[0] if observing else None
        targets = set((row.steering or {}).get("cio", {})) | ({observed_target} if observed_target else set())
        for t in sorted(targets):
            items.append({"relation": f"{cell}-{t}", "source": cell, "target": t,
                          "cioBias": (row.steering or {}).get("cio", {}).get(t, 0),
                          "state": engine.OBSERVING if t == observed_target else engine.STEADY})
    db.commit()
    return {"items": items}


@app.get("/instances/{instance_id}/dashboard")
def dashboard(instance_id: uuid.UUID, points: int = 48, db: Session = Depends(get_session)):
    """Per cell: the congestion-score trend, the latest forecast and plan, the
    safety evaluation, decision, intent, action, verification, KPI check and
    the steering in force."""
    inst = _instance(db, instance_id)
    rows = _cells(db, inst)
    series = by_cell(_dataset(inst, "INFERENCE"))
    out = []
    for cell in _layers(inst):
        latest = db.scalars(select(TrafficDecision).where(TrafficDecision.instance_id == instance_id,
                                                          TrafficDecision.cell_id == cell)
                            .order_by(TrafficDecision.created_at.desc()).limit(1)).first()
        trend = [{"t": t.isoformat(), "v": score(counters(p))} for t, p in series.get(cell, [])[-points:]]
        out.append({**_cell_view(inst, rows[cell]), "scoreTrend": trend,
                    "latestDecision": _decision_view(latest) if latest else None})
    db.commit()
    return {"instance": _instance_view(inst), "cells": out}


# ---------------------------------------------------------------- Digital Twin producer

@app.post("/sim-producer/register", status_code=201)
def register_sim_producer():
    # Registers the LOAD_PERFORMANCE_SIM data type at DME with this rApp as the Digital Twin producer (201).
    return register_sim_type(sdk)


@app.post("/sim-producer/publish")
def publish_sim_data(body: SimPublishRequest):
    # Generates the requested clusters' windows (a hotspot or healthy) and delivers them to every data job of the sim type.
    return publish_sim(sdk, body)


# ---------------------------------------------------------------- demo / simulation (docs/GUI_DEMO_GUIDE.md)
# What demo.py does around the rApp, as routes the operator page declares as admin-only buttons (`role: admin` in manifest.yaml), so the demo runs from
# the console: the rApp plays the network (load PM into RAN NF OAM, as the NF would send it, made by the package's own load model from the steering
# live on O1) and the Digital Twin. Simulation time, as demo.py's: history from 2026-09-01, live PM from noon on the 4th, a stadium hotspot next to
# the cell in 401's place.

DEMO_HISTORY_START = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
DEMO_LIVE_START = DEMO_HISTORY_START + datetime.timedelta(days=3, hours=12)
DEMO_HOTSPOT = "401"
DEMO_TWIN = SimPublishRequest(managedElementRef="digital-twin-mlb", start=DEMO_HISTORY_START, hours=24, clusters={
    "dt1": {"scenario": "HOTSPOT", "hotCell": "a"}, "dt2": {"scenario": "HOTSPOT", "hotCell": "c"}, "dt3": {"scenario": "HEALTHY"}})


def _demo_topology(inst: TrafficInstance) -> tuple[dict[str, list[str]], dict[str, str], str]:
    """The sample cluster (producer.NEIGHBOURS) with the instance's first four configured cells in the places of 401, 402, 411 and 412, in that
    order, each with its configured layer; and the cell in 401's place, where the hotspot is. 422 DEMO_TOPOLOGY for fewer than four cells."""
    layers = _layers(inst)
    if len(layers) < len(producer.CELLS):
        raise RappError(422, "DEMO_TOPOLOGY", f"the demo plays the sample's {len(producer.CELLS)}-cell cluster; the instance has {len(layers)} cells")
    rename = dict(zip(producer.CELLS, layers))
    return ({rename[c]: [rename[t] for t in nbrs] for c, nbrs in producer.NEIGHBOURS.items()},
            {rename[c]: layers[rename[c]] for c in producer.CELLS}, rename[DEMO_HOTSPOT])


def _live_settings(inst: TrafficInstance, nbrs: dict[str, list[str]], layers: dict[str, str]) -> dict[str, dict]:
    """Each cell's steering live on O1 (read back through RAN NF OAM, as demo.py's `_settings`): CIO towards each neighbour, priority towards the
    other layer. A knob that cannot be read is left out, so the load model takes its baseline."""
    out = {}
    for c, targets in nbrs.items():
        cio = {t: v for t in targets if (v := _value(inst, f"NRCellRelation={c}-{t}")) is not None}
        prio = {layers[t]: v for t in targets if layers[t] != layers[c]
                and (v := _value(inst, f"NRFreqRelation={c}-{layers[t]}")) is not None}
        out[c] = {"cio": cio, "prio": prio}
    return out


# How many measurements one demo PM report carries (`_report_pm`).
PM_REPORT_CHUNK = 96


def _report_pm(inst: TrafficInstance, measurements: list[dict]) -> int:
    """Sends `measurements` to RAN NF OAM as LOAD_PERFORMANCE reports of the instance's element; returns how many went. A refusal (no PM
    subscription, an element outside the rApp's scope) is answered as RAN NF OAM gave it. The caller's read transaction is ended first
    (`db.commit()` by the caller), as RAN NF OAM does before it fans out: nothing of ours is held while others write."""
    # in reports of at most PM_REPORT_CHUNK measurements: RAN NF OAM delivers each one to every open data job, one DME record at a time, so a whole
    # history in one report outlasts the 30 s this rApp's R1 call allows (call it at least once, as a single report did)
    for first in range(0, max(len(measurements), 1), PM_REPORT_CHUNK):
        chunk = measurements[first:first + PM_REPORT_CHUNK]
        resp = _r1.post("/ran-nf-oam/pm-reports", json={"managedElementRef": inst.managed_element_ref, "counterType": DATASET, "measurements": chunk})
        if resp.status_code != 201:
            raise SdkError(resp.status_code, resp.json() if resp.content else None)
    return len(measurements)


def _demo_hour(instance_id: uuid.UUID, db: Session, hour: int) -> dict:
    """One live hour of PM (demo.py's `_live_hour`): the window `hour` hours after the live start, from the steering live on O1 and the hotspot."""
    inst = _instance(db, instance_id)
    db.commit()                                            # end the read transaction before RAN NF OAM writes to DME
    nbrs, layers, hot = _demo_topology(inst)
    t = DEMO_LIVE_START + datetime.timedelta(hours=hour)
    sent = _report_pm(inst, producer.measurements(nbrs, layers, _live_settings(inst, nbrs, layers), {hot: "HOTSPOT"}, t))
    return {"measurements": sent, "at": t.isoformat(), "hotspot": hot}


@app.post("/instances/{instance_id}/demo/prepare")
def demo_prepare(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 00-01, the rApp's part: registers the Digital Twin producer at DME (DME takes the same registration again as it is) and starts the
    instance (`/start`), which discovers its datasets."""
    registered = register_sim_type(sdk)
    return {"simProducer": registered.get("dmeTypeId"), "instance": start_instance(instance_id, db)}


@app.post("/instances/{instance_id}/demo/history")
def demo_history(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 02: three days of hourly load PM in which each cell's CIO or priority was stepped in turn, reported to RAN NF OAM, which delivers it to
    DME for the training dataset."""
    inst = _instance(db, instance_id)
    db.commit()                                            # end the read transaction before RAN NF OAM writes to DME
    nbrs, layers, _ = _demo_topology(inst)
    sent = _report_pm(inst, producer.history(nbrs, layers, DEMO_HISTORY_START, 72))
    return {"measurements": sent, "from": DEMO_HISTORY_START.isoformat(), "datasets": {k: v["dataset"] for k, v in (inst.data_jobs or {}).items()}}


@app.post("/instances/{instance_id}/demo/emulate")
def demo_emulate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 05: the Digital Twin publishes a day of three clusters (two with a hotspot, one healthy), then the model is emulated against them
    (`lifecycle/emulate`)."""
    published = publish_sim(sdk, DEMO_TWIN)
    return {"digitalTwin": published, **emulate(instance_id, db)}


@app.post("/instances/{instance_id}/demo/live")
def demo_live(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 08's PM: the busy afternoon's first hour with the hotspot next to the cell in 401's place. Then press Evaluate now: the hotspot cell is
    forecast congested and one step moves load to its least-loaded neighbour."""
    return _demo_hour(instance_id, db, 0)


@app.post("/instances/{instance_id}/demo/kpi-hour")
def demo_kpi_hour(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 10's PM: the next hour, made from the steering now live on O1. Then press Evaluate now: the KPI check of the step (CONFIRMED, or
    reverted)."""
    return _demo_hour(instance_id, db, 1)


app.include_router(producer_router)
