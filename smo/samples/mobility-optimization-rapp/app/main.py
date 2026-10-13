"""Mobility Optimization rApp — the Wave 10.2 reference rApp
(HISTORY.md Wave 10.2, W10.2-01..10). A Non-RT RIC rApp that
tunes each neighbour relation's Cell Individual Offset (CIO) from
handover failures, through O1. Like the EnergySaving rApp it uses O1 PM
data only, and the R1 interface only through the AI Runtime SDK; there is
no A1, Near-RT RIC, xApp or E2.

The loop (POST /instances/{id}/evaluate), per neighbour relation:

    Input (HO_PERFORMANCE counters) → Classification + Prediction
          → KPI check of the last change → Safety → Decision
          → O1 execution → Verification → Rollback / Revert → Audit

How each kind of write reaches O1:

  * A CIO **change** (RAISE / LOWER) is governed by the instance's
    autonomy mode, through AutonomyDispatch:
      - SHADOW only recommends;
      - ASSIST waits for the operator to resolve or reject;
      - AUTONOMOUS goes through an Intent, the SA SMOS O1-CM handler, DME
        and RAN NF OAM, writing NRCellRelation.cellIndividualOffset.
  * A **revert** (KPI degraded) or **rollback** (write failed or
    unverified) restores the previous CIO, so it goes straight to DME
    `/actions`, with its own `actionId`.
  * At deploy, the **DMRO bounds** (decision D10.2-1) are written to the
    gNB's DMROFunction and verified, so its own distributed MRO stays
    inside the rApp's guard rails.
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
from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import install_health
from smo_shared.correlation import apply_correlation_id, get_correlation_id
from smo_shared.db import get_session
from smo_shared.r1_client import R1Client
from smo_shared.timeutil import as_utc

from . import engine, producer  # producer: the sample data generator, also used by tests and demo.py
from .model import EmulationLogic, InferenceLogic, TrainingLogic, ValidationLogic
from .model.MobilityModel import MODEL_TYPE, MobilityModel
from .model.series import by_relation, counters, mro_rate, parse_time
from .models import MobilityDecision, MobilityInstance, MobilityRelation
from .producer import SimPublishRequest, publish_sim, register_sim_type, router as producer_router

app = FastAPI(title="Mobility Optimization rApp")
instances.install(app)  # the credentials route rApp Management pushes to, and acting as the instance of each /instances/{id}/... request (call flow 01c)
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
apply_correlation_id(app)

_r1 = R1Client()
sdk = AiRuntimeSdk(_r1)

RAPP_ID = "mobility-optimization-rapp"

# The fulfilment report of a dispatched Intent is not there when the dispatch answers: Intent Service queues the push to the handler (an outbox row, sent
# right after the commit with a 2 s timeout, then retried by the delivery sweep every few seconds), and the handler enacts the change and publishes the
# report afterwards. `_intent_actions` therefore polls for it, at most this long, every this often, before it counts the change as not enacted.
INTENT_REPORT_WAIT_SECONDS = 30.0
INTENT_REPORT_POLL_SECONDS = 0.5
DATASET, SIM_DATASET = "HO_PERFORMANCE", "HO_PERFORMANCE_SIM"
NODE_GROUPS = ["mobility-optimization"]
CIO_TARGET = "NRCellRelation.cellIndividualOffset"
# D10.2-1: the bounds imposed on the gNB's own distributed MRO
DEFAULT_DMRO_BOUNDS = {"dmroControl": True, "maximumDeviationHoTriggerLow": -6, "maximumDeviationHoTriggerHigh": 6,
                       "minimumTimeBetweenHoTriggerChange": 60}


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

def _instance(db: Session, instance_id: uuid.UUID) -> MobilityInstance:
    inst = db.get(MobilityInstance, instance_id)
    if inst is None:
        raise RappError(404, "INSTANCE_NOT_STARTED", f"instance {instance_id} is not started on this rApp")
    return inst


def _relations(db: Session, inst: MobilityInstance) -> dict[str, MobilityRelation]:
    """Returns the instance's relation rows by relation id, adding (not committing) a row for any configured relation that has none yet; the caller
    commits.
    """
    rows = {r.relation_id: r for r in db.scalars(select(MobilityRelation).where(MobilityRelation.instance_id == inst.instance_id))}
    for rel in inst.relations:
        if rel["relation"] not in rows:
            rows[rel["relation"]] = MobilityRelation(instance_id=inst.instance_id, relation_id=rel["relation"])
            db.add(rows[rel["relation"]])
    return rows


def _consumer(inst: MobilityInstance) -> str:
    return f"{RAPP_ID}:{inst.instance_id}"


def _cio_list(value: int) -> list[int]:
    return [value] * 6  # TS 28.541: six QOffsetRange entries, written alike


def _parse_cio(raw) -> int | None:
    """The live CIO, if all six entries agree."""
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if isinstance(values, list) and values and all(v == values[0] for v in values):
        return int(values[0])
    return None


def _read(inst: MobilityInstance, mfr: str) -> dict | None:
    try:
        return sdk.data.read_config(inst.managed_element_ref, mfr)["attributes"]
    except SdkError:
        return None


def _read_relation(inst: MobilityInstance, relation: str) -> tuple[int | None, bool]:
    attrs = _read(inst, f"NRCellRelation={relation}") or {}
    return _parse_cio(attrs.get("cellIndividualOffset")), str(attrs.get("isHOAllowed", "true")).lower() != "false"


def _target_o1_asleep(inst: MobilityInstance, cell: str) -> bool:
    du = _read(inst, f"NRCellDU={cell}") or {}
    ces = _read(inst, f"CESManagementFunction={cell}") or {}
    return du.get("administrativeState") == "LOCKED" or ces.get("energySavingState") == "IS_ENERGY_SAVING"


def _verify_cio(inst: MobilityInstance, relations: dict[str, int]) -> dict:
    observed = {rel: _read_relation(inst, rel)[0] for rel in relations}
    return {"result": "VERIFIED" if all(observed[r] == v for r, v in relations.items()) else "VERIFY_FAILED",
            "expected": relations, "observed": observed, "attribute": CIO_TARGET}


def _decision(inst, execution_id: str, reason: str) -> dict:
    """PR-AI-13: why this write is made, kept by RAN NF OAM as the decision record of the config job (and shown to a person who is asked to approve it): a
    reference to this execution (its inputs are on the instance's operator page, not in the record), the version of the model that decided, and the reason in words.
    The direct path of this rApp is a service-restoring write (a wake, a revert, a rollback, an operator override)."""
    wanted = {"inputsRef": f"{RAPP_ID}:{inst.instance_id}:execution:{execution_id}", "modelVersion": inst.model_version,
              "rationale": f"Restoring service: {reason}"}
    return {k: v for k, v in wanted.items() if v}


def _execute_direct(inst: MobilityInstance, changes: dict[str, int], execution_id: str, reason: str) -> dict:
    """Sends CIO writes straight to DME `/actions` with a fresh actionId and returns {path, actionId, status, ...}.

    A platform error is returned as status REJECTED with the error body, not raised, so the caller's verification read decides what happened.
    """
    action_id = str(uuid.uuid4())
    try:
        result = sdk.platform.execute_action(
            f"{RAPP_ID}:{inst.instance_id}",
            [{"managedElementRef": inst.managed_element_ref, "className": "NRCellRelation",
              "managedFunctionRef": f"NRCellRelation={rel}", "attributeChanges": {"cellIndividualOffset": _cio_list(v)}}
             for rel, v in changes.items()],
            action_id=action_id, source_context={"rApp": RAPP_ID, "correlationId": execution_id, "reason": reason},
            decision=_decision(inst, execution_id, reason))
        return {"path": "DME_DIRECT", "actionId": result["actionId"], "forwardedJobId": result.get("forwardedJobId"),
                "status": result["status"]}
    except SdkError as e:
        return {"path": "DME_DIRECT", "actionId": action_id, "status": "REJECTED", "error": e.body}


def _restore(inst: MobilityInstance, changes: dict[str, int], execution_id: str, reason: str) -> dict:
    """Write the given CIOs straight through DME, verify, and re-send once on
    a mismatch (used for reverts and rollbacks)."""
    pending = {r: v for r, v in changes.items() if _read_relation(inst, r)[0] != v}
    if not pending:
        return {"performed": False, "result": "ALREADY_RESTORED", "verification": _verify_cio(inst, changes)}
    attempts = []
    for _ in range(2):
        action = _execute_direct(inst, pending, execution_id, reason)
        verification = _verify_cio(inst, pending)
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


def _expectation(inst: MobilityInstance, relation: str, value: int, execution_id: str) -> dict:
    """Builds the one TS 28.312 DELIVER expectation for a relation's new CIO (six equal entries); its id (`cio-<relation>-<first 8 of the execution
    id>`) is how the Intent's fulfilment report is matched back to the relation.
    """
    return {"expectationId": f"cio-{relation}-{execution_id[:8]}", "expectationVerb": "DELIVER",
            "expectationObject": {"objectType": "RAN_SUBNETWORK", "objectInstance": inst.managed_element_ref,
                                  "objectContexts": [{"contextAttribute": "Cell", "contextCondition": "IS_ALL_OF",
                                                      "contextValueRange": [relation]}]},
            "expectationTargets": [{"targetName": CIO_TARGET, "targetCondition": "IS_EQUAL_TO",
                                    "targetValueRange": _cio_list(value)}]}


def _settle(row: MobilityRelation, d: MobilityDecision, now: datetime.datetime | None, ok: bool, rollback: dict | None) -> None:
    """Records the end state of one relation's change on its row and audit row.

    When `ok`, the relation goes to OBSERVING with its new CIO and the change (time, from, to, the rate before) is kept for the KPI check.
    Otherwise it returns to STEADY on its old CIO and the outcome names the rollback trigger and whether the rollback was verified. `now` is
    None when the caller has no observation time, then the decision's own is used.
    """
    if ok:
        row.state, row.current_cio = engine.OBSERVING, d.to_cio
        changed_at = cast(datetime.datetime, now or d.observed_at)      # a decision to change has an observation
        row.last_change = {"at": changed_at.isoformat(), "from": d.from_cio, "to": d.to_cio, "preRate": d.rate}
        row.last_changed_at = changed_at
        d.outcome = "EXECUTED"
    else:
        row.state, row.current_cio = engine.STEADY, d.from_cio
        undone = cast(dict, rollback)      # not ok, so the caller made the rollback
        d.outcome = f"{undone['trigger']}_ROLLED_BACK" if undone["result"] in ("VERIFIED", "ALREADY_RESTORED") \
            else f"{undone['trigger']}_ROLLBACK_FAILED"
    d.final_state = {"state": row.state, "cio": row.current_cio}
    d.updated_at = datetime.datetime.now(datetime.UTC)


def _follow_dispatch(inst, rows, decisions: dict[str, MobilityDecision], dispatch: dict, execution_id: str) -> None:
    """Settles the relations of one AutonomyDispatch according to where it is.

    SHADOWED, REJECTED and AWAITING_SCOPE only record the outcome (ASSIST remembers the pending dispatch on the row); a dispatched change is
    matched to the Intent's action report, verified by an O1 read-back, and rolled back through DME when the action did not complete or the
    read-back differs.
    """
    intent = {"dispatchId": dispatch["dispatchId"], "autonomyMode": dispatch["autonomyMode"], "status": dispatch["status"],
              "intentId": dispatch.get("intentId"), "rejectedBy": dispatch.get("rejectedBy")}
    rels = list(decisions)
    for rel in rels:
        decisions[rel].intent = intent
    if dispatch["status"] in ("SHADOWED", "REJECTED", "AWAITING_SCOPE"):
        for rel in rels:
            d, row = decisions[rel], rows[rel]
            d.outcome = {"SHADOWED": "SHADOWED", "REJECTED": "REJECTED", "AWAITING_SCOPE": "AWAITING_APPROVAL"}[dispatch["status"]]
            if dispatch["status"] == "AWAITING_SCOPE":
                row.pending_dispatch_id, row.pending_decision_id = uuid.UUID(dispatch["dispatchId"]), d.decision_id
            d.final_state = {"state": row.state, "cio": row.current_cio}
        return
    actions = _intent_actions(dispatch["intentId"])
    for rel in rels:
        d, row = decisions[rel], rows[rel]
        to_cio, from_cio = cast(int, d.to_cio), cast(int, d.from_cio)      # a change has both
        action = actions.get(_expectation(inst, rel, to_cio, d.execution_id)["expectationId"])
        d.action = {"path": "INTENT", **action} if action else {"path": "INTENT", "status": "NOT_ENACTED"}
        status = d.action.get("status")
        d.verification = _verify_cio(inst, {rel: to_cio}) if status == "COMPLETED" else None
        ok = status == "COMPLETED" and cast(dict, d.verification)["result"] == "VERIFIED"      # verified just above
        rollback = None
        if not ok:
            trigger = "VERIFY_FAILED" if status == "COMPLETED" else "PARTIAL_SUCCESS" if status == "PARTIAL_SUCCESS" else "ACTION_FAILED"
            rollback = {"trigger": trigger, **_restore(inst, {rel: from_cio}, execution_id, f"ROLLBACK:{trigger}")}
        d.rollback = rollback
        _settle(row, d, None, ok, rollback)


def _reconcile(db: Session, inst: MobilityInstance) -> list[dict]:
    """Settles ASSIST dispatches the operator has since resolved or rejected.

    Groups the relations by pending dispatch, skips those still AWAITING_SCOPE, follows the others, commits, and returns one summary per settled
    dispatch with each relation's outcome.
    """
    rows = _relations(db, inst)
    pending: dict[uuid.UUID, list[str]] = {}
    for rel, row in rows.items():
        if row.pending_dispatch_id:
            pending.setdefault(row.pending_dispatch_id, []).append(rel)
    settled = []
    for dispatch_id, rels in pending.items():
        dispatch = sdk.intent.get_autonomy_dispatch(dispatch_id)
        if dispatch["status"] == "AWAITING_SCOPE":
            continue
        decisions = {r: db.get_one(MobilityDecision, rows[r].pending_decision_id) for r in rels}
        for r in rels:
            rows[r].pending_dispatch_id = rows[r].pending_decision_id = None
        _follow_dispatch(inst, rows, decisions, dispatch, next(iter(decisions.values())).execution_id)
        settled.append({"dispatchId": str(dispatch_id), "status": dispatch["status"],
                        "outcomes": {r: decisions[r].outcome for r in rels}})
    db.commit()
    return settled


# ---------------------------------------------------------------- instance + datasets

install_health(app)  # /live, /ready and the /health alias (PR-ST-7)


@app.post("/instances/{instance_id}/start")
def start_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Binds this rApp to its rapp-mgmt instance and discovers its datasets.
    The instance config carries the managed element, the relations
    ([{relation, source, target}]), the CIO baseline, the DMRO bounds, and
    optionally the EnergySaving instance to coordinate with."""
    resp = _r1.get(f"/rapp-mgmt/instances/{instance_id}")
    if resp.status_code != 200:
        raise RappError(404, "INSTANCE_NOT_FOUND", f"rapp-mgmt has no instance {instance_id}")
    info = resp.json()
    config = info.get("configuration") or {}
    relations = config.get("relations") or []
    if not config.get("managedElementRef") or not relations or not all({"relation", "source", "target"} <= set(r) for r in relations):
        raise RappError(422, "INSTANCE_CONFIG_INVALID", "instance config needs managedElementRef and relations [{relation, source, target}]")
    inst = db.get(MobilityInstance, instance_id) or MobilityInstance(instance_id=instance_id)
    inst.package_id = uuid.UUID(info["packageId"]) if info.get("packageId") else None
    inst.managed_element_ref, inst.relations = config["managedElementRef"], relations
    inst.baseline_cio = int(config.get("baselineCio", 0))
    inst.dmro_bounds = {**DEFAULT_DMRO_BOUNDS, **(config.get("dmroBounds") or {})}
    inst.autonomy_mode, inst.rmih_id = info.get("autonomyMode", "SHADOW"), config.get("rmihId", "sa-smos")
    inst.energy_saving_instance_id = config.get("energySavingInstanceId")
    inst.traffic_steering_instance_id = config.get("trafficSteeringInstanceId")
    inst.operator_notification_uri = config.get("operatorNotificationUri")
    datasets = {}
    for stage, name in (("TRAINING", DATASET), ("INFERENCE", DATASET), ("EMULATION", SIM_DATASET)):
        found = sdk.data.get_dataset(name, _consumer(inst), lifecycle_stage=stage, max_records=1)
        datasets[stage] = {"dataset": name, "dmeTypeId": found["dmeTypeId"], "dataJobId": found["dataJobId"],
                           "sourceDomain": found["sourceDomain"]}
    inst.data_jobs = datasets
    db.add(inst)
    _relations(db, inst)
    db.commit()
    # call flow 01d: the rApp reports the instance up itself, as the instance (its credentials were pushed at CreateInstance)
    return {**_instance_view(inst), "bootstrap": instances.bootstrap(sdk, str(instance_id))}


@app.get("/instances")
def list_instances(db: Session = Depends(get_session)):
    # Lists every started instance, oldest first.
    return {"items": [_instance_view(i) for i in db.scalars(select(MobilityInstance).order_by(MobilityInstance.created_at))]}


@app.get("/instances/{instance_id}")
def get_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # Returns one instance; 404 INSTANCE_NOT_STARTED when `start` was never called for it.
    return _instance_view(_instance(db, instance_id))


def _instance_view(i: MobilityInstance) -> dict:
    """The JSON shape of an instance on every route that returns one."""
    return {"instanceId": str(i.instance_id), "packageId": str(i.package_id) if i.package_id else None,
            "managedElementRef": i.managed_element_ref, "relations": i.relations, "baselineCio": i.baseline_cio,
            "dmroBounds": i.dmro_bounds, "autonomyMode": i.autonomy_mode, "rmihId": i.rmih_id,
            "energySavingInstanceId": i.energy_saving_instance_id,
            "trafficSteeringInstanceId": i.traffic_steering_instance_id, "datasets": i.data_jobs,
            "modelId": str(i.model_id) if i.model_id else None, "modelVersion": i.model_version,
            "artifactVersion": i.artifact_version, "model": i.model_params, "lifecycleJobs": i.lifecycle_jobs}


def _dataset(inst: MobilityInstance, stage: str) -> list[dict]:
    return sdk.data.get_dataset(SIM_DATASET if stage == "EMULATION" else DATASET, _consumer(inst), lifecycle_stage=stage)["records"]


def _jobs(inst: MobilityInstance, **updates) -> None:
    inst.lifecycle_jobs = {**(inst.lifecycle_jobs or {}), **updates}


# ---------------------------------------------------------------- lifecycle

@app.post("/instances/{instance_id}/lifecycle/train")
def train(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """TRAINING on MLTF: registered in MLMR, trained on handover history, stored as an artifact."""
    inst = _instance(db, instance_id)
    version = inst.model_version or f"1.0.0-{str(inst.instance_id)[:8]}"
    if inst.model_id is None:
        inst.model_id = uuid.UUID(sdk.models.register_model(
            MODEL_TYPE, version, description="MRO failure classification + next-hour failure-rate regression",
            input_data_type=DATASET, output_data_type="cellIndividualOffset", domain="CUSTOM",
            custom_domain="RAN_MOBILITY_ROBUSTNESS")["modelId"])
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
    stored = sdk.models.store_model(MODEL_TYPE, version, model.to_artifact(), filename="mobility_model.zip")
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
    model = MobilityModel.from_dict(cast(dict, inst.model_params))      # set by training
    passed, metrics = ValidationLogic.validate(model, _dataset(inst, "TRAINING"))
    completed = sdk.lifecycle.complete_validation(job["validationJobId"], passed, metrics=metrics)
    _jobs(inst, validation=job["validationJobId"])
    db.commit()
    return {"validationJobId": job["validationJobId"], "passed": passed, "status": completed["status"], "metrics": metrics,
            "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


@app.post("/instances/{instance_id}/lifecycle/emulate")
def emulate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """EMULATION on MLEF against the Digital Twin's relations with injected faults."""
    inst = _instance(db, instance_id)
    job = sdk.lifecycle.start_emulation(inst.model_id, RAPP_ID, package_id=inst.package_id,
                                        emulation_criteria={"dataset": SIM_DATASET, "minDirectionAccuracy": EmulationLogic.PASS_RATE})
    model = MobilityModel.from_dict(cast(dict, inst.model_params))      # set by training
    passed, metrics = EmulationLogic.emulate(model, _dataset(inst, "EMULATION"))
    completed = sdk.lifecycle.complete_emulation(job["emulationJobId"], passed, metrics=metrics)
    _jobs(inst, emulation=job["emulationJobId"])
    db.commit()
    return {"emulationJobId": job["emulationJobId"], "passed": passed, "status": completed["status"], "metrics": metrics,
            "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


@app.post("/instances/{instance_id}/lifecycle/deploy")
def deploy(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """MLIF deployment once the model is certified. The gNB's DMRO bounds
    are applied and verified (D10.2-1), and the serving model is loaded
    from MLMR."""
    inst = _instance(db, instance_id)
    sdk.lifecycle.deploy_model(inst.model_id, NODE_GROUPS)
    sdk.lifecycle.deploy_runtime(inst.model_id, package_id=inst.package_id)
    sdk.lifecycle.activate_runtime(inst.model_id)
    artifact = sdk.models.download_artifact(inst.model_id, inst.artifact_version)
    inst.model_params = MobilityModel.from_artifact(artifact.content).to_dict()
    dmro_ref = f"DMROFunction={inst.managed_element_ref}"
    action = sdk.platform.execute_action(
        f"{RAPP_ID}:{inst.instance_id}", [{"managedElementRef": inst.managed_element_ref, "className": "DMROFunction",
                                           "managedFunctionRef": dmro_ref, "attributeChanges": inst.dmro_bounds}],
        action_id=uuid.uuid4(), source_context={"rApp": RAPP_ID, "reason": "DMRO_BOUNDS"},
        decision={"modelVersion": inst.model_version, "rationale": "Apply the DMRO bounds the package declares once the model is certified and deployed"})
    observed = _read(inst, dmro_ref) or {}
    verified = all(str(observed.get(k)) == str(v) for k, v in inst.dmro_bounds.items())
    db.commit()
    return {"modelId": str(inst.model_id), "artifactVersion": inst.artifact_version, "model": inst.model_params,
            "dmro": {"actionId": action["actionId"], "status": action["status"], "bounds": inst.dmro_bounds,
                     "observed": observed, "verification": "VERIFIED" if verified else "VERIFY_FAILED"},
            "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


# ---------------------------------------------------------------- the closed loop

def _peer(instance_id, resource: str) -> str:
    """The route, through the gateway, of a list another rApp publishes for its instance (coordination): `/rapps/{instanceId}/operator/...` reaches the
    operator API that instance registered at rApp Management (GUI-8.3). A peer that has not registered one answers 404, which is read as "publishes nothing"."""
    return f"/rapps/{instance_id}/operator/instances/{instance_id}/{resource}"



def _es_cells(inst: MobilityInstance) -> dict[str, dict]:
    """The EnergySaving rApp's published cell states (coordination, D10.2-4c)."""
    if not inst.energy_saving_instance_id:
        return {}
    resp = _r1.get(_peer(inst.energy_saving_instance_id, "cells"))
    return {c["cellId"]: c for c in resp.json().get("items", [])} if resp.status_code == 200 else {}


def _mlb_observing(inst: MobilityInstance) -> set[str]:
    """Relations the Traffic Steering rApp has a CIO change on under
    observation (the shared-CIO arbitration, Wave 10.4 D10.4-1)."""
    if not inst.traffic_steering_instance_id:
        return set()
    resp = _r1.get(_peer(inst.traffic_steering_instance_id, "relations"))
    return {r["relation"] for r in resp.json().get("items", []) if r.get("state") == "OBSERVING"} \
        if resp.status_code == 200 else set()


@app.post("/instances/{instance_id}/evaluate")
def evaluate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """One pass of the closed loop over the instance's relations. Its
    X-Correlation-ID is the execution id in the audit trail."""
    inst = _instance(db, instance_id)
    if not inst.model_id or not inst.model_params:
        raise RappError(409, "MODEL_NOT_DEPLOYED", "train, certify and deploy the model first")
    reconciled = _reconcile(db, inst)
    execution_id = get_correlation_id() or str(uuid.uuid4())
    rows = _relations(db, inst)
    series = by_relation(_dataset(inst, "INFERENCE"))
    guards = {g["cellId"]: g for g in sdk.data.query_cell_guards(managed_element_ref=inst.managed_element_ref)}
    es_cells = _es_cells(inst)
    mlb = _mlb_observing(inst)
    model = MobilityModel.from_dict(inst.model_params)

    job = sdk.lifecycle.request_inference(inst.model_id)
    outputs = {r["relation"]: InferenceLogic.infer(model, r["relation"], series.get(r["relation"], [])) for r in inst.relations}
    resolved = sdk.lifecycle.resolve_inference(job["inferenceJobId"], True, inference_outputs=[
        {"aIMLInferenceName": MODEL_TYPE, "outputResult": o} for o in outputs.values() if o])

    decisions: dict[str, MobilityDecision] = {}
    results: dict[str, engine.Decision] = {}
    for rel in inst.relations:
        rid, row = rel["relation"], rows[rel["relation"]]
        if row.pending_dispatch_id:
            continue  # ASSIST: waiting for the operator
        live_cio, ho_allowed = _read_relation(inst, rid)
        current = live_cio if live_cio is not None else (row.current_cio if row.current_cio is not None else inst.baseline_cio)
        es = es_cells.get(rel["target"]) or {}
        change = row.last_change
        result = engine.decide(engine.RelationInput(
            relation=rid, source=rel["source"], target=rel["target"], series=series.get(rid, []), current_cio=current,
            baseline_cio=inst.baseline_cio, state=row.state,
            last_change={**change, "at": parse_time(change["at"])} if change else None,
            last_changed_at=as_utc(row.last_changed_at) if row.last_changed_at else None, prediction=outputs[rid],
            ho_allowed=ho_allowed, source_guard=guards.get(rel["source"], {}), target_guard=guards.get(rel["target"], {}),
            target_o1_asleep=_target_o1_asleep(inst, rel["target"]), target_es_state=es.get("state"),
            target_last_woken=parse_time(es["lastUnlockedAt"]) if es.get("lastUnlockedAt") else None,
            mlb_observing=rid in mlb))
        results[rid] = result
        row.current_cio = current
        decisions[rid] = d = MobilityDecision(
            execution_id=execution_id, instance_id=inst.instance_id, relation_id=rid, observed_at=result.observed_at,
            rate=result.rate, attempts=result.attempts, decision=result.decision, reason=result.reason, outcome="NONE",
            from_cio=current, to_cio=result.new_cio, kpi=result.kpi,
            prediction={"model": outputs[rid], "inferenceJobId": job["inferenceJobId"],
                        "aimlInferenceReportId": resolved.get("aIMLInferenceReportId")},
            safety={**result.safety, "hoAllowed": ho_allowed, "targetEnergySavingState": es.get("state")})
        db.add(d)
        if result.decision == engine.NO_CHANGE:
            row.state = result.next_state
            if result.reason == "CHANGE_CONFIRMED":
                d.outcome, row.last_change = "CONFIRMED", None
            d.final_state = {"state": row.state, "cio": row.current_cio}
    db.flush()

    # KPI-degraded reverts restore the previous CIO straight through DME
    for rid, result in results.items():
        if result.decision != engine.REVERT:
            continue
        d, row = decisions[rid], rows[rid]
        restore = _restore(inst, {rid: cast(int, result.new_cio)}, execution_id, "REVERT:KPI_DEGRADED")      # a revert names the CIO to restore
        d.action = restore["attempts"][0]["action"] if restore["performed"] else None
        d.verification = restore["attempts"][-1]["verification"] if restore["performed"] else restore["verification"]
        ok = restore["result"] in ("VERIFIED", "ALREADY_RESTORED")
        d.outcome = "REVERTED" if ok else "REVERT_FAILED"
        row.state, row.last_change = engine.STEADY, None
        if ok:
            row.current_cio, row.last_changed_at = result.new_cio, result.observed_at
        d.final_state = {"state": row.state, "cio": row.current_cio}

    # CIO changes follow the autonomy mode, through AutonomyDispatch
    changes = {rid: decisions[rid] for rid, r in results.items() if r.decision in (engine.RAISE, engine.LOWER)}
    if changes:
        dispatch = sdk.intent.request_autonomy_dispatch(
            inst.instance_id, [_expectation(inst, rid, cast(int, d.to_cio), execution_id) for rid, d in changes.items()], inst.rmih_id,
            model_id=inst.model_id, notification_destination=inst.operator_notification_uri,
            user_label=f"mobility-optimization {execution_id}")
        _follow_dispatch(inst, rows, changes, dispatch, execution_id)
    for row in rows.values():
        row.updated_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    return {"executionId": execution_id, "instanceId": str(inst.instance_id), "autonomyMode": inst.autonomy_mode,
            "inferenceJobId": job["inferenceJobId"], "reconciled": reconciled,
            "decisions": [_decision_view(decisions[r["relation"]]) for r in inst.relations if r["relation"] in decisions]}


@app.post("/instances/{instance_id}/reconcile")
def reconcile(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # Settles pending ASSIST dispatches now (the same step `evaluate` runs first); returns {settled: [...]}, empty when there is nothing to
    # settle.
    return {"settled": _reconcile(db, _instance(db, instance_id))}


# ---------------------------------------------------------------- audit + dashboard

def _decision_view(d: MobilityDecision) -> dict:
    """The JSON shape of one audit row, as the decisions list, the evaluate answer and the dashboard return it."""
    return {"decisionId": str(d.decision_id), "executionId": d.execution_id, "relation": d.relation_id,
            "observedAt": d.observed_at.isoformat() if d.observed_at else None, "rate": d.rate, "attempts": d.attempts,
            "prediction": d.prediction, "safety": d.safety, "decision": d.decision, "reason": d.reason,
            "fromCio": d.from_cio, "toCio": d.to_cio, "kpi": d.kpi, "intent": d.intent, "action": d.action,
            "verification": d.verification, "rollback": d.rollback, "outcome": d.outcome, "finalState": d.final_state,
            "createdAt": d.created_at.isoformat()}


@app.get("/instances/{instance_id}/decisions")
def list_decisions(instance_id: uuid.UUID, relation: str | None = None, execution_id: str | None = None,
                   limit: int = 100, db: Session = Depends(get_session)):
    # Audit rows of an instance, newest first; filtered by relation and execution_id when given; `limit` is capped at 500.
    stmt = select(MobilityDecision).where(MobilityDecision.instance_id == instance_id)
    if relation:
        stmt = stmt.where(MobilityDecision.relation_id == relation)
    if execution_id:
        stmt = stmt.where(MobilityDecision.execution_id == execution_id)
    rows = db.scalars(stmt.order_by(MobilityDecision.created_at.desc()).limit(min(limit, 500))).all()
    return {"items": [_decision_view(d) for d in rows]}


def _relation_view(inst: MobilityInstance, r: MobilityRelation) -> dict:
    rel = next(x for x in inst.relations if x["relation"] == r.relation_id)
    return {"relation": r.relation_id, "source": rel["source"], "target": rel["target"], "state": r.state,
            "cio": r.current_cio, "lastChange": r.last_change,
            "pendingDispatchId": str(r.pending_dispatch_id) if r.pending_dispatch_id else None}


@app.get("/instances/{instance_id}/relations")
def list_relations(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # The instance's relations with their state, CIO and last change; creates the missing rows and commits, so a freshly started instance lists
    # all its relations.
    inst = _instance(db, instance_id)
    rows = _relations(db, inst)
    db.commit()
    return {"items": [_relation_view(inst, rows[r["relation"]]) for r in inst.relations]}


@app.get("/instances/{instance_id}/dashboard")
def dashboard(instance_id: uuid.UUID, points: int = 48, db: Session = Depends(get_session)):
    """Per relation: the failure-rate trend, the latest classification and
    prediction, the safety evaluation, decision, intent, action,
    verification, KPI check and the resulting CIO."""
    inst = _instance(db, instance_id)
    rows = _relations(db, inst)
    series = by_relation(_dataset(inst, "INFERENCE"))
    out = []
    for rel in inst.relations:
        rid = rel["relation"]
        latest = db.scalars(select(MobilityDecision).where(MobilityDecision.instance_id == instance_id,
                                                           MobilityDecision.relation_id == rid)
                            .order_by(MobilityDecision.created_at.desc()).limit(1)).first()
        trend = [{"t": t.isoformat(), "v": mro_rate(counters(p))} for t, p in series.get(rid, [])[-points:]]
        out.append({**_relation_view(inst, rows[rid]), "rateTrend": trend,
                    "latestDecision": _decision_view(latest) if latest else None})
    db.commit()
    return {"instance": _instance_view(inst), "relations": out}


# ---------------------------------------------------------------- Digital Twin producer

@app.post("/sim-producer/register", status_code=201)
def register_sim_producer():
    # Registers the HO_PERFORMANCE_SIM data type at DME with this rApp as the Digital Twin producer (201).
    return register_sim_type(sdk)


@app.post("/sim-producer/publish")
def publish_sim_data(body: SimPublishRequest):
    # Generates the requested relations' windows with their injected scenario and delivers them to every data job of the sim type.
    return publish_sim(sdk, body)


# ---------------------------------------------------------------- demo / simulation (docs/GUI_DEMO_GUIDE.md)
# What demo.py does around the rApp, as routes the operator page declares as admin-only buttons (`role: admin` in manifest.yaml), so the demo runs from
# the console: the rApp plays the network (HO_PERFORMANCE PM reports into RAN NF OAM, as the NF would send them) and the Digital Twin. Simulation time,
# as demo.py's: handover history from 2026-09-01, live PM from midnight on the 4th, the KPI hour two hours after that.

DEMO_HISTORY_START = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
DEMO_LIVE_START = DEMO_HISTORY_START + datetime.timedelta(days=3)
# demo.py's problem per relation: 201->203 hands over too late, 202->203 too early. A relation the instance has under another id takes the scenario
# of the demo relation in the same position, so a renamed set of four shows the same story.
DEMO_SCENARIOS = {"201-202": "HEALTHY", "201-203": "TOO_LATE", "202-203": "TOO_EARLY", "203-204": "HEALTHY"}


def _demo_scenarios(inst: MobilityInstance) -> dict[str, str]:
    """The injected scenario of each of the instance's relations: demo.py's for its relation ids, by position for any other id."""
    fallback = list(DEMO_SCENARIOS.values())
    return {r["relation"]: DEMO_SCENARIOS.get(r["relation"], fallback[i % len(fallback)]) for i, r in enumerate(inst.relations)}


# How many measurements one demo PM report carries (`_report_pm`).
PM_REPORT_CHUNK = 96


def _report_pm(inst: MobilityInstance, scenarios: dict[str, str], start: datetime.datetime, hours: int) -> int:
    """Sends `hours` hourly handover windows from `start` for each relation in `scenarios` (relation -> scenario, counters from producer.py as demo.py
    makes them) to RAN NF OAM as HO_PERFORMANCE reports of the instance's element; returns how many measurements went. A refusal (no PM
    subscription, an element outside the rApp's scope) is answered as RAN NF OAM gave it. The caller ends its read transaction first (`db.commit()`),
    as RAN NF OAM does before it fans out: nothing of ours is held while others write."""
    sources = {r["relation"]: r["source"] for r in inst.relations}
    measurements = [{"cellId": sources.get(rel, rel.split("-")[0]), "relation": rel, "timestamp": t.isoformat(),
                     "values": producer.relation_counters(rel, t, scenario)}
                    for rel, scenario in scenarios.items()
                    for t in (start + datetime.timedelta(hours=h) for h in range(hours))]
    # in reports of at most PM_REPORT_CHUNK measurements: RAN NF OAM delivers each one to every open data job, one DME record at a time, so a whole
    # history in one report outlasts the 30 s this rApp's R1 call allows (call it at least once, as a single report did)
    for first in range(0, max(len(measurements), 1), PM_REPORT_CHUNK):
        chunk = measurements[first:first + PM_REPORT_CHUNK]
        resp = _r1.post("/ran-nf-oam/pm-reports", json=producer.pm_report(inst.managed_element_ref, chunk))
        if resp.status_code != 201:
            raise SdkError(resp.status_code, resp.json() if resp.content else None)
    return len(measurements)


@app.post("/instances/{instance_id}/demo/prepare")
def demo_prepare(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 00-01, the rApp's part: registers the Digital Twin producer at DME (DME takes the same registration again as it is) and starts the
    instance (`/start`), which subscribes it to its datasets."""
    registered = register_sim_type(sdk)
    return {"simProducer": registered.get("dmeTypeId"), "instance": start_instance(instance_id, db)}


@app.post("/instances/{instance_id}/demo/history")
def demo_history(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 02: three days of hourly handover counters per relation (201->203 too late, 202->203 too early), reported to RAN NF OAM, which delivers
    them to DME for the training dataset."""
    inst = _instance(db, instance_id)
    db.commit()                                            # end the read transaction before RAN NF OAM writes to DME
    scenarios = _demo_scenarios(inst)
    return {"measurements": _report_pm(inst, scenarios, DEMO_HISTORY_START, 72), "from": DEMO_HISTORY_START.isoformat(),
            "scenarios": scenarios, "datasets": {k: v["dataset"] for k, v in (inst.data_jobs or {}).items()}}


@app.post("/instances/{instance_id}/demo/emulate")
def demo_emulate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 05: the Digital Twin publishes two days of relations with one injected fault each, then the model is emulated against them
    (`lifecycle/emulate`)."""
    published = publish_sim(sdk, SimPublishRequest(
        managedElementRef="digital-twin-mro", start=DEMO_HISTORY_START, hours=48,
        relations={"a-b": "TOO_LATE", "b-c": "TOO_EARLY", "c-d": "HEALTHY", "d-e": "WRONG_CELL", "e-f": "PING_PONG"}))
    return {"digitalTwin": published, **emulate(instance_id, db)}


@app.post("/instances/{instance_id}/demo/live")
def demo_live(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 08's PM: two hours from midnight on the 4th with the history's problems still there. Then press Evaluate now: the CIO of the too-late
    relation is raised, the too-early one's lowered, and a relation towards a guarded cell is left alone."""
    inst = _instance(db, instance_id)
    db.commit()                                            # end the read transaction before RAN NF OAM writes to DME
    scenarios = _demo_scenarios(inst)
    return {"measurements": _report_pm(inst, scenarios, DEMO_LIVE_START, 2), "from": DEMO_LIVE_START.isoformat(), "scenarios": scenarios}


@app.post("/instances/{instance_id}/demo/kpi-hour")
def demo_kpi_hour(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 10's PM: the hour after the change, every relation healthy. Then press Evaluate now: the KPI check confirms the CIO changes (had the
    failures risen, they would have been reverted)."""
    inst = _instance(db, instance_id)
    db.commit()                                            # end the read transaction before RAN NF OAM writes to DME
    start = DEMO_LIVE_START + datetime.timedelta(hours=2)
    scenarios = {r["relation"]: "HEALTHY" for r in inst.relations}
    return {"measurements": _report_pm(inst, scenarios, start, 1), "from": start.isoformat(), "scenarios": scenarios}


app.include_router(producer_router)
