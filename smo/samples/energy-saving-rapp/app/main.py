"""EnergySaving rApp — the Wave 10.1 reference rApp (HISTORY.md
§9, W10-01..W10-25). A Non-RT RIC rApp that sleeps lightly used cells
through O1 and wakes them before load returns. It uses O1 PM data only;
there is no A1, Near-RT RIC, xApp or E2.

Every platform interaction goes through the AI Runtime SDK (`smo_sdk`), so
R1 only, never another module's database:

  * data — the PRB_UTILIZATION dataset (O1 PM → RAN NF OAM → DME) and the
    Digital Twin PRB_UTILIZATION_SIM dataset; cell guards; config read-back
  * models / lifecycle — MLMR model + artifact; AIMgF training, validation,
    emulation, certification (operator), MLLF + MLIF runtime, inference
  * analytics — MDAF's PRB prediction for a cell, when there is one
  * intent — AutonomyDispatch for a LOCK (AUTONOMOUS/ASSIST/SHADOW)
  * platform — direct DME O1 actions for service-restoring writes

The loop (POST /instances/{id}/evaluate, or the worker's `evaluate-due` task every `evaluateEverySeconds` of the instance, call flow 22e; one pass at a
time per instance):

    Input → Prediction (model via an AIMgF inference job, plus MDAF)
          → Safety evaluation → Decision (engine.py)
          → O1 execution → Verification (read-after-write) → Rollback → Audit

How a decision is written to O1:

  * **LOCK** (it reduces service) is governed by the instance's autonomy mode:
    - SHADOW: the recommendation is dispatched as SHADOWED; nothing changes.
    - ASSIST: it waits in AWAITING_SCOPE until an operator resolves it
      (then enacted) or rejects it (then nothing happens).
    - AUTONOMOUS: Intent → SA SMOS O1-CM handler → DME → RAN NF OAM → NETCONF.
  * **UNLOCK** (wake), rollback and an operator override restore service, so
    they go straight to DME `/actions` in every enforcing mode. Each carries
    its own `actionId`, so a replay is ignored.

Every write is verified by reading the cell's live configuration back. A
failed, partial or unverified LOCK is rolled back to UNLOCKED through the
same DME path.
"""

import datetime
import json
import logging
import time
import uuid
from typing import cast

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from smo_sdk import AiRuntimeSdk, SdkError, instances
from smo_sdk.data import AlarmScope
from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import install_health
from smo_shared.correlation import apply_correlation_id, correlation_scope, get_correlation_id
from smo_shared.db import get_session
from smo_shared.r1_client import R1Client, reset_acting_instance, set_acting_instance
from smo_shared.timeutil import as_utc

from . import engine
from .model import EmulationLogic, InferenceLogic, TrainingLogic, ValidationLogic
from .model.EnergyModel import MODEL_TYPE, EnergyModel
from .model.series import by_cell, cell_key, parse_time
from .models import EnergySavingCell, EnergySavingDecision, EnergySavingInstance
from .producer import SimPublishRequest, diurnal_prb, pm_report, publish_sim, register_sim_type, router as producer_router

app = FastAPI(title="EnergySaving rApp")
instances.install(app)  # the credentials route rApp Management pushes to, and acting as the instance of each /instances/{id}/... request (call flow 01c)
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
apply_correlation_id(app)

_r1 = R1Client()
sdk = AiRuntimeSdk(_r1)

RAPP_ID = "energy-saving-rapp"
logger = logging.getLogger(__name__)

# Call flow 22e: the bounds of an instance's `evaluateEverySeconds` (a pass reads PM, asks AIMgF and may write O1, so not more often than a minute).
EVALUATE_EVERY_MIN_SECONDS, EVALUATE_EVERY_MAX_SECONDS = 60, 86400

# The fulfilment report of a dispatched Intent is not there when the dispatch answers: Intent Service queues the push to the handler (an outbox row, sent
# right after the commit with a 2 s timeout, then retried by the delivery sweep every few seconds), and the handler enacts the change and publishes the
# report afterwards. `_intent_actions` therefore polls for it, at most this long, every this often, before it counts the change as not enacted.
INTENT_REPORT_WAIT_SECONDS = 30.0
INTENT_REPORT_POLL_SECONDS = 0.5
DATASET, SIM_DATASET = "PRB_UTILIZATION", "PRB_UTILIZATION_SIM"
NODE_GROUPS = ["energy-saving"]

# decision D-2: the O1 actuator, per instance
ACTUATORS = {
    "ADMINISTRATIVE_STATE": {"ioc": "NRCellDU", "attribute": "administrativeState", "lock": "LOCKED", "unlock": "UNLOCKED",
                             "readAttribute": "administrativeState", "locked": "LOCKED", "unlocked": "UNLOCKED"},
    "ENERGY_SAVING_CONTROL": {"ioc": "CESManagementFunction", "attribute": "energySavingControl",
                              "lock": "TO_BE_ENERGY_SAVING", "unlock": "TO_BE_NOT_ENERGY_SAVING",
                              "readAttribute": "energySavingState", "locked": "IS_ENERGY_SAVING",
                              "unlocked": "IS_NOT_ENERGY_SAVING"},
}


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": {"title": title, "status": status, "detail": detail}})


class RappError(Exception):
    """An error this rApp raises on purpose; `_rapp_error` answers it as `{detail: {title, status, detail}}` with `status` as the HTTP status."""
    def __init__(self, status: int, title: str, detail: str):
        self.status, self.title, self.detail = status, title, detail


@app.exception_handler(RappError)
def _rapp_error(_, exc: RappError):
    return _problem(exc.status, exc.title, exc.detail)


@app.exception_handler(SdkError)
def _sdk_error(_, exc: SdkError):
    body = exc.body if isinstance(exc.body, dict) else {"detail": exc.body}
    detail = body.get("detail", body)
    return JSONResponse(status_code=exc.status_code if exc.status_code < 500 else 502,
                        content={"detail": detail if isinstance(detail, dict) else {"title": "PLATFORM_ERROR", "detail": detail}})


# ---------------------------------------------------------------- helpers

def _instance(db: Session, instance_id: uuid.UUID) -> EnergySavingInstance:
    inst = db.get(EnergySavingInstance, instance_id)
    if inst is None:
        raise RappError(404, "INSTANCE_NOT_STARTED", f"instance {instance_id} is not started on this rApp")
    return inst


def _lock_instance(db: Session, instance_id: uuid.UUID) -> EnergySavingInstance:
    """The instance, locked for this transaction (`SELECT ... FOR UPDATE NOWAIT` on Postgres): a pass, a reconcile and an override of one instance run one
    at a time (call flow 22a). 409 EVALUATION_IN_PROGRESS when another holds it, at once rather than after it; 404 as `_instance`. SQLite has no row locks."""
    try:
        inst = db.get(EnergySavingInstance, instance_id, with_for_update={"nowait": True}, populate_existing=True)
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":       # lock_not_available; anything else is not ours to answer
            raise
        db.rollback()
        raise RappError(409, "EVALUATION_IN_PROGRESS", f"instance {instance_id} is being evaluated, reconciled or overridden; try again") from exc
    if inst is None:
        raise RappError(404, "INSTANCE_NOT_STARTED", f"instance {instance_id} is not started on this rApp")
    return inst


def _cells(db: Session, inst: EnergySavingInstance) -> dict[str, EnergySavingCell]:
    """Returns the instance's cell rows by cell id, adding (not committing) a SERVING row for any configured cell that has none yet; the caller
    commits.
    """
    rows = {c.cell_id: c for c in db.scalars(select(EnergySavingCell).where(EnergySavingCell.instance_id == inst.instance_id))}
    for cell in inst.cells:
        if cell not in rows:
            rows[cell] = EnergySavingCell(instance_id=inst.instance_id, cell_id=cell, state=engine.SERVING)
            db.add(rows[cell])
    return rows


def _consumer(inst: EnergySavingInstance) -> str:
    return f"{RAPP_ID}:{inst.instance_id}"


def _mfr(inst: EnergySavingInstance, cell: str) -> str:
    return f"{ACTUATORS[inst.actuator]['ioc']}={cell}"


def _change(inst: EnergySavingInstance, cell: str, value: str) -> dict:
    act = ACTUATORS[inst.actuator]
    return {"managedElementRef": inst.managed_element_ref, "className": act["ioc"], "managedFunctionRef": _mfr(inst, cell),
            "attributeChanges": {act["attribute"]: value}}


def _read(inst: EnergySavingInstance, cell: str) -> str | None:
    """The cell's live actuator state, read back over O1 (W10-20)."""
    try:
        attrs = sdk.data.read_config(inst.managed_element_ref, _mfr(inst, cell))["attributes"]
    except SdkError:
        return None
    return attrs.get(ACTUATORS[inst.actuator]["readAttribute"])


def _verify(inst: EnergySavingInstance, cells: list[str], want_locked: bool) -> dict:
    """Reads each cell's actuator back over O1 and compares it with the locked or unlocked value; returns {result: VERIFIED | VERIFY_FAILED,
    expected, observed, attribute}. An unreadable cell counts as a mismatch.
    """
    act = ACTUATORS[inst.actuator]
    expected = act["locked"] if want_locked else act["unlocked"]
    observed = {cell: _read(inst, cell) for cell in cells}
    return {"result": "VERIFIED" if all(v == expected for v in observed.values()) else "VERIFY_FAILED",
            "expected": expected, "observed": observed,
            "attribute": f"{act['ioc']}.{act['readAttribute']}"}


def _decision(inst, execution_id: str, reason: str) -> dict:
    """PR-AI-13: why this write is made, kept by RAN NF OAM as the decision record of the config job (and shown to a person who is asked to approve it): a
    reference to this execution (its inputs are on the instance's operator page, not in the record), the version of the model that decided, and the reason in words.
    The direct path of this rApp is a service-restoring write (a wake, a revert, a rollback, an operator override)."""
    wanted = {"inputsRef": f"{RAPP_ID}:{inst.instance_id}:execution:{execution_id}", "modelVersion": inst.model_version,
              "rationale": f"Restoring service: {reason}"}
    return {k: v for k, v in wanted.items() if v}


def _execute_direct(inst: EnergySavingInstance, cells: list[str], lock: bool, execution_id: str, reason: str) -> dict:
    """A DME O1 action carrying its own idempotency key and this
    execution's correlation id (W10-16/W10-18)."""
    action_id = str(uuid.uuid4())
    act = ACTUATORS[inst.actuator]
    try:
        result = sdk.platform.execute_action(
            f"{RAPP_ID}:{inst.instance_id}", [_change(inst, c, act["lock"] if lock else act["unlock"]) for c in cells],
            action_id=action_id, source_context={"rApp": RAPP_ID, "correlationId": execution_id, "reason": reason},
            decision=_decision(inst, execution_id, reason))
        done = {"path": "DME_DIRECT", "actionId": result["actionId"], "forwardedJobId": result.get("forwardedJobId"), "status": result["status"]}
        return {**done, "approvalId": result["approvalId"]} if result.get("approvalId") else done
    except SdkError as e:
        return {"path": "DME_DIRECT", "actionId": action_id, "status": "REJECTED", "error": e.body}


def _restore(inst: EnergySavingInstance, cells: list[str], execution_id: str, reason: str) -> dict:
    """Rollback / wake: UNLOCK the cells that aren't already UNLOCKED,
    verify, and re-send once if the read-back still disagrees (W10-21).

    Call flow 22d: when RAN NF OAM holds the write for a person (an approval policy on this rApp, PR-AI-11; DME answers PENDING_APPROVAL with an
    `approvalId` and no job), nothing has changed yet, so the cells are neither read back nor sent again (that would ask a second time): the result is
    AWAITING_APPROVAL with the `approvalId` and the `cells` it covers, which `_settle_approvals` follows up."""
    act = ACTUATORS[inst.actuator]
    pending = [c for c in cells if _read(inst, c) != act["unlocked"]]
    if not pending:
        return {"performed": False, "result": "ALREADY_UNLOCKED", "verification": _verify(inst, cells, False)}
    attempts: list[dict] = []
    result = "VERIFY_FAILED"
    for _ in range(2):
        action = _execute_direct(inst, pending, False, execution_id, reason)
        if action["status"] == "PENDING_APPROVAL":
            attempts.append({"action": action, "verification": None})
            return {"performed": True, "result": "AWAITING_APPROVAL", "approvalId": action.get("approvalId"), "cells": pending, "attempts": attempts}
        verification = _verify(inst, pending, False)
        attempts.append({"action": action, "verification": verification})
        result = verification["result"]
        if result == "VERIFIED":
            break
    return {"performed": True, "result": result, "attempts": attempts}


def _intent_actions(intent_id: str) -> list[dict]:
    """The DME actions the intent handler took, from the Intent's own
    fulfilment report (TS 28.312 IntentReport, additionalFulfilmentInfo).
    Waits for it: it polls the reports until one carries `additionalFulfilmentInfo`, for at most `INTENT_REPORT_WAIT_SECONDS`; only then is the
    answer empty (the change counts as not enacted)."""
    deadline = time.monotonic() + INTENT_REPORT_WAIT_SECONDS
    while True:
        for report in sdk.intent.list_intent_reports(intent_id):
            info = (report["attributes"].get("intentFulfilmentReport") or {}).get("additionalFulfilmentInfo")
            if info:
                try:
                    return json.loads(info).get("actions", [])
                except ValueError:
                    return []
        if time.monotonic() >= deadline:
            return []
        time.sleep(INTENT_REPORT_POLL_SECONDS)


def _finish_lock(db: Session, inst: EnergySavingInstance, rows: dict, decisions: dict[str, EnergySavingDecision],
                 cells: list[str], action: dict, execution_id: str, now: datetime.datetime | None) -> None:
    """After a LOCK was enacted (any path): verify, roll back on failure,
    and settle each cell's state and audit row."""
    status = action.get("status")
    act = ACTUATORS[inst.actuator]
    verification = _verify(inst, cells, True) if status == "COMPLETED" else None
    ok = status == "COMPLETED" and cast(dict, verification)["result"] == "VERIFIED"      # a COMPLETED action was verified just above
    rollback = None
    if not ok:
        trigger = ("VERIFY_FAILED" if status == "COMPLETED" else
                   "PARTIAL_SUCCESS" if status == "PARTIAL_SUCCESS" else "ACTION_FAILED")
        rollback = {"trigger": trigger, **_restore(inst, cells, execution_id, f"ROLLBACK:{trigger}")}
    for cell in cells:
        d, row = decisions[cell], rows[cell]
        d.action, d.verification, d.rollback = action, verification, rollback
        if ok:
            d.outcome = "EXECUTED"
            row.state, row.o1_value = engine.SLEEP, act["locked"]
        else:
            undone = cast(dict, rollback)      # not ok, so the rollback was made above
            if undone["result"] == "AWAITING_APPROVAL" and cell in undone["cells"]:
                # call flow 22d: the cell may be locked and its rollback waits for a person; `_settle_approvals` reads it back once decided
                d.outcome = f"{undone['trigger']}_ROLLBACK_AWAITING_APPROVAL"
                row.state = engine.SLEEP
                row.pending_approval_id, row.pending_decision_id = _uuid_or_none(undone.get("approvalId")), d.decision_id
                d.final_state = {"state": row.state, "o1": row.o1_value}
                d.updated_at = datetime.datetime.now(datetime.UTC)
                continue
            d.outcome = f"{undone['trigger']}_ROLLED_BACK" if undone["result"] in ("VERIFIED", "ALREADY_UNLOCKED", "AWAITING_APPROVAL") \
                else f"{undone['trigger']}_ROLLBACK_FAILED"
            row.state, row.o1_value = engine.SERVING, act["unlocked"]
            if undone["performed"] and undone["result"] != "AWAITING_APPROVAL":
                row.last_unlocked_at = now or d.observed_at or row.last_unlocked_at
        d.final_state = {"state": row.state, "o1": row.o1_value}
        d.updated_at = datetime.datetime.now(datetime.UTC)


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    """`value` as a UUID, or None when there is none."""
    return uuid.UUID(str(value)) if value else None


def _settle_approvals(db: Session, inst: EnergySavingInstance, rows: dict[str, EnergySavingCell]) -> list[dict]:
    """Call flow 22d: follows up the service-restoring writes RAN NF OAM held for a person (PR-AI-11). A request still PENDING is left; a decided one
    (APPROVED, REJECTED, EXPIRED or REFUSED) is settled by reading the cell back: UNLOCKED makes it SERVING, anything else SLEEP. The audit row of the write
    gets the read-back and its outcome: EXECUTED (approved and verified), VERIFY_FAILED (approved, not unlocked) or APPROVAL_<status>; a rollback's row
    becomes `<trigger>_ROLLED_BACK` or `<trigger>_ROLLBACK_<FAILED | status>`. A rejected wake is asked again by the next pass that decides it."""
    act = ACTUATORS[inst.actuator]
    settled = []
    for cell, row in rows.items():
        if row.pending_approval_id is None:
            continue
        approval = sdk.platform.get_approval(row.pending_approval_id)
        status = approval["status"]
        if status == "PENDING":
            continue
        verification = _verify(inst, [cell], False)
        unlocked = verification["result"] == "VERIFIED"
        d = db.get(EnergySavingDecision, row.pending_decision_id) if row.pending_decision_id else None
        approval_id = row.pending_approval_id
        row.pending_approval_id = row.pending_decision_id = None
        row.state = engine.SERVING if unlocked else engine.SLEEP
        if unlocked:
            row.o1_value, row.last_unlocked_at = act["unlocked"], datetime.datetime.now(datetime.UTC)
        if d is not None:
            settled_as = "FAILED" if status == "APPROVED" else status      # how a rollback that did not unlock the cell ended
            if d.outcome.endswith("_ROLLBACK_AWAITING_APPROVAL"):
                trigger = d.outcome.removesuffix("_ROLLBACK_AWAITING_APPROVAL")
                d.outcome = f"{trigger}_ROLLED_BACK" if unlocked else f"{trigger}_ROLLBACK_{settled_as}"
            else:
                d.outcome = "EXECUTED" if unlocked and status == "APPROVED" else "VERIFY_FAILED" if status == "APPROVED" else f"APPROVAL_{status}"
            d.action = {**(d.action or {}), "approval": {"status": status, "jobId": approval.get("jobId")}}
            d.verification = verification
            d.final_state = {"state": row.state, "o1": row.o1_value}
            d.updated_at = datetime.datetime.now(datetime.UTC)
        settled.append({"approvalId": str(approval_id), "status": status, "cells": [cell],
                        "outcomes": {cell: d.outcome if d is not None else row.state}})
    return settled


def _lock_expectation(inst: EnergySavingInstance, cells: list[str], execution_id: str) -> dict:
    """Builds the one TS 28.312 DELIVER expectation that sets the actuator of all `cells` to its locked value; its id carries the first 8
    characters of the execution id.
    """
    act = ACTUATORS[inst.actuator]
    return {"expectationId": f"energy-saving-{execution_id[:8]}", "expectationVerb": "DELIVER",
            "expectationObject": {"objectType": "RAN_SUBNETWORK", "objectInstance": inst.managed_element_ref,
                                  "objectContexts": [{"contextAttribute": "Cell", "contextCondition": "IS_ALL_OF",
                                                      "contextValueRange": cells}]},
            "expectationTargets": [{"targetName": f"{act['ioc']}.{act['attribute']}", "targetCondition": "IS_EQUAL_TO",
                                    "targetValueRange": act["lock"]}]}


def _follow_dispatch(db, inst, rows, decisions, cells, dispatch: dict, execution_id, now) -> None:
    """Settle LOCK decisions according to where their AutonomyDispatch is."""
    intent = {"dispatchId": dispatch["dispatchId"], "autonomyMode": dispatch["autonomyMode"], "status": dispatch["status"],
              "intentId": dispatch.get("intentId"), "rejectedBy": dispatch.get("rejectedBy")}
    for cell in cells:
        decisions[cell].intent = intent
    if dispatch["status"] == "SHADOWED":
        for cell in cells:
            decisions[cell].outcome = "SHADOWED"          # recommended + operator notified, never enacted
            rows[cell].state = engine.PRE_SLEEP
            decisions[cell].final_state = {"state": rows[cell].state, "o1": rows[cell].o1_value}
    elif dispatch["status"] == "AWAITING_SCOPE":
        for cell in cells:
            decisions[cell].outcome = "AWAITING_APPROVAL"
            rows[cell].state = engine.PRE_SLEEP
            rows[cell].pending_dispatch_id = uuid.UUID(dispatch["dispatchId"])
            rows[cell].pending_decision_id = decisions[cell].decision_id
            decisions[cell].final_state = {"state": rows[cell].state, "o1": rows[cell].o1_value}
    elif dispatch["status"] == "REJECTED":
        for cell in cells:
            decisions[cell].outcome = "REJECTED"
            rows[cell].state = engine.SERVING
            decisions[cell].final_state = {"state": rows[cell].state, "o1": rows[cell].o1_value}
    elif dispatch["status"] == "DISPATCHED":
        actions = _intent_actions(dispatch["intentId"])
        action = {"path": "INTENT", **actions[0]} if actions else {"path": "INTENT", "status": "NOT_ENACTED"}
        _finish_lock(db, inst, rows, decisions, cells, action, execution_id, now)
    for cell in cells:
        decisions[cell].updated_at = datetime.datetime.now(datetime.UTC)


def _reconcile(db: Session, inst: EnergySavingInstance) -> list[dict]:
    """ASSIST: follow up dispatches an operator has since resolved or rejected (call flow 22c), and the writes held for approval that were decided
    (call flow 22d). The caller holds the instance's lock and commits."""
    rows = _cells(db, inst)
    pending: dict[uuid.UUID, list[str]] = {}
    for cell, row in rows.items():
        if row.pending_dispatch_id:
            pending.setdefault(row.pending_dispatch_id, []).append(cell)
    settled = []
    for dispatch_id, cells in pending.items():
        dispatch = sdk.intent.get_autonomy_dispatch(dispatch_id)
        if dispatch["status"] == "AWAITING_SCOPE":
            continue
        decisions = {c: db.get_one(EnergySavingDecision, rows[c].pending_decision_id) for c in cells}
        for c in cells:
            rows[c].pending_dispatch_id = rows[c].pending_decision_id = None
        execution_id = next(iter(decisions.values())).execution_id
        _follow_dispatch(db, inst, rows, decisions, cells, dispatch, execution_id, None)
        settled.append({"dispatchId": str(dispatch_id), "status": dispatch["status"], "cells": cells,
                        "outcomes": {c: decisions[c].outcome for c in cells}})
    return settled + _settle_approvals(db, inst, rows)


# ---------------------------------------------------------------- instance + datasets (W10-04/05)

install_health(app)  # /live, /ready and the /health alias (PR-ST-7)


@app.post("/instances/{instance_id}/start")
def start_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Binds this rApp to its rapp-mgmt instance (cells, actuator,
    autonomyMode come from the instance) and discovers its datasets through
    DME — one data job per execution mode (TC02)."""
    resp = _r1.get(f"/rapp-mgmt/instances/{instance_id}")
    if resp.status_code != 200:
        raise RappError(404, "INSTANCE_NOT_FOUND", f"rapp-mgmt has no instance {instance_id}")
    info = resp.json()
    config = info.get("configuration") or {}
    if not config.get("managedElementRef") or not config.get("cells"):
        raise RappError(422, "INSTANCE_CONFIG_INVALID", "instance config needs managedElementRef and cells")
    actuator = config.get("actuator", "ADMINISTRATIVE_STATE")
    if actuator not in ACTUATORS:
        raise RappError(422, "INSTANCE_CONFIG_INVALID", f"actuator must be one of {sorted(ACTUATORS)}")
    every = config.get("evaluateEverySeconds")
    if every is not None and (isinstance(every, bool) or not isinstance(every, int)
                              or not EVALUATE_EVERY_MIN_SECONDS <= every <= EVALUATE_EVERY_MAX_SECONDS):
        raise RappError(422, "INSTANCE_CONFIG_INVALID",
                        f"evaluateEverySeconds must be a whole number from {EVALUATE_EVERY_MIN_SECONDS} to {EVALUATE_EVERY_MAX_SECONDS}, or absent")
    inst = db.get(EnergySavingInstance, instance_id) or EnergySavingInstance(instance_id=instance_id)
    inst.package_id = uuid.UUID(info["packageId"]) if info.get("packageId") else None
    inst.managed_element_ref, inst.cells = config["managedElementRef"], [str(c) for c in config["cells"]]
    inst.actuator, inst.autonomy_mode = actuator, info.get("autonomyMode", "SHADOW")
    inst.rmih_id = config.get("rmihId", "sa-smos")
    inst.operator_notification_uri = config.get("operatorNotificationUri")
    inst.evaluate_every_seconds = every
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
    return {"items": [_instance_view(i) for i in db.scalars(select(EnergySavingInstance).order_by(EnergySavingInstance.created_at))]}


@app.get("/instances/{instance_id}")
def get_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # Returns one instance; 404 INSTANCE_NOT_STARTED when `start` was never called for it.
    return _instance_view(_instance(db, instance_id))


def _instance_view(i: EnergySavingInstance) -> dict:
    """The JSON shape of an instance on every route that returns one."""
    return {"instanceId": str(i.instance_id), "packageId": str(i.package_id) if i.package_id else None,
            "managedElementRef": i.managed_element_ref, "cells": i.cells, "actuator": i.actuator,
            "autonomyMode": i.autonomy_mode, "rmihId": i.rmih_id, "datasets": i.data_jobs,
            "modelId": str(i.model_id) if i.model_id else None, "modelVersion": i.model_version,
            "artifactVersion": i.artifact_version, "model": i.model_params, "lifecycleJobs": i.lifecycle_jobs,
            "evaluateEverySeconds": i.evaluate_every_seconds, "lastEvaluation": i.last_evaluation}


def _dataset(inst: EnergySavingInstance, stage: str) -> list[dict]:
    name = SIM_DATASET if stage == "EMULATION" else DATASET
    return sdk.data.get_dataset(name, _consumer(inst), lifecycle_stage=stage)["records"]


# ---------------------------------------------------------------- lifecycle (W10-02, W10-07, W10-08)

def _jobs(inst: EnergySavingInstance, **updates) -> None:
    inst.lifecycle_jobs = {**(inst.lifecycle_jobs or {}), **updates}


@app.post("/instances/{instance_id}/lifecycle/train")
def train(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """TRAINING on the package's MLTF runtime profile: the model is
    registered in MLMR, trained on PRB history and stored there as an
    artifact (TC03)."""
    inst = _instance(db, instance_id)
    version = inst.model_version or f"1.0.0-{str(inst.instance_id)[:8]}"
    if inst.model_id is None:
        inst.model_id = uuid.UUID(sdk.models.register_model(
            MODEL_TYPE, version, description="Threshold + linear regression: next-hour PRB per cell",
            input_data_type=DATASET, output_data_type="recommendedState", domain="CUSTOM",
            custom_domain="RAN_ENERGY_SAVING")["modelId"])
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
    stored = sdk.models.store_model(MODEL_TYPE, version, model.to_artifact(), filename="energy_model.zip")
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
    """VALIDATION on MLVF: held-out history must score above threshold (TC04)."""
    inst = _instance(db, instance_id)
    job = sdk.lifecycle.start_validation(inst.model_id, RAPP_ID, package_id=inst.package_id,
                                         training_job_id=(inst.lifecycle_jobs or {}).get("training"),
                                         validation_criteria={"minScore": ValidationLogic.PASS_THRESHOLD})
    model = EnergyModel.from_dict(cast(dict, inst.model_params))      # set by training
    passed, metrics = ValidationLogic.validate(model, _dataset(inst, "TRAINING"))
    completed = sdk.lifecycle.complete_validation(job["validationJobId"], passed, metrics=metrics)
    _jobs(inst, validation=job["validationJobId"])
    db.commit()
    return {"validationJobId": job["validationJobId"], "passed": passed, "status": completed["status"], "metrics": metrics,
            "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


@app.post("/instances/{instance_id}/lifecycle/emulate")
def emulate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """EMULATION on MLEF against the Digital Twin dataset (TC05)."""
    inst = _instance(db, instance_id)
    job = sdk.lifecycle.start_emulation(inst.model_id, RAPP_ID, package_id=inst.package_id,
                                        emulation_criteria={"dataset": SIM_DATASET, "midnightRecommendation": "LOCKED"})
    model = EnergyModel.from_dict(cast(dict, inst.model_params))      # set by training
    passed, metrics = EmulationLogic.emulate(model, _dataset(inst, "EMULATION"))
    completed = sdk.lifecycle.complete_emulation(job["emulationJobId"], passed, metrics=metrics)
    _jobs(inst, emulation=job["emulationJobId"])
    db.commit()
    return {"emulationJobId": job["emulationJobId"], "passed": passed, "status": completed["status"], "metrics": metrics,
            "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


@app.post("/instances/{instance_id}/lifecycle/deploy")
def deploy(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """MLIF deployment once an operator has certified the model (TC07):
    MLLF clears the node groups (it checks CERTIFIED), AIMgF instantiates
    the inference runtime through NFO and activates it, and the serving
    model is loaded from its MLMR artifact."""
    inst = _instance(db, instance_id)
    sdk.lifecycle.deploy_model(inst.model_id, NODE_GROUPS)
    sdk.lifecycle.deploy_runtime(inst.model_id, package_id=inst.package_id)
    sdk.lifecycle.activate_runtime(inst.model_id)
    artifact = sdk.models.download_artifact(inst.model_id, inst.artifact_version)
    inst.model_params = EnergyModel.from_artifact(artifact.content).to_dict()
    db.commit()
    return {"modelId": str(inst.model_id), "artifactVersion": inst.artifact_version, "model": inst.model_params,
            "lifecycle": sdk.lifecycle.get_model_lifecycle(inst.model_id)}


# ---------------------------------------------------------------- the closed loop (W10-09..W10-23)

def _inputs(inst: EnergySavingInstance, rows: dict) -> dict:
    """Everything the engine needs, read from the platform."""
    series = by_cell(_dataset(inst, "INFERENCE"))
    guards = {cell_key(g["managedElementRef"], g["cellId"]): g for g in sdk.data.query_cell_guards()}
    # W10-alarm-cellref: an alarm raised on a cell holds that cell (and the
    # cells whose neighbour it is); one that names no cell holds them all
    alarms = sdk.data.query_critical_alarms(inst.managed_element_ref)
    coverage = AlarmScope([a for a in alarms.alarms
                           if "coverage" in f"{a.get('probableCause')} {a.get('specificProblem')}".lower()])
    asleep = {cell_key(inst.managed_element_ref, c) for c, r in rows.items() if r.state == engine.SLEEP}
    return {"series": series, "guards": guards, "alarms": alarms, "coverage": coverage, "asleep": asleep}


def _cell_and_neighbours(inst: EnergySavingInstance, cell: str, guard: dict) -> list[str]:
    """A cell plus the cells of the same element it hands its traffic to
    when asleep (neighbourRefs are cell keys `<element>/<cell>`): an alarm on
    any of them keeps this cell awake."""
    prefix = f"{inst.managed_element_ref}/"
    return [cell, *(n[len(prefix):] for n in guard.get("neighbourRefs") or [] if n.startswith(prefix))]


def _sector_peers_awake(key: str, guards: dict, asleep: set) -> int | None:
    group = (guards.get(key) or {}).get("sectorGroup")
    if not group:
        return None
    return sum(1 for k, g in guards.items() if k != key and g.get("sectorGroup") == group and k not in asleep)


def _mdaf_prediction(key: str) -> float | None:
    """MDAF's predicted PRB for a cell key, or None when MDAF has no prediction or is unavailable; an MDAF outage never stops the loop."""
    try:
        prediction = sdk.analytics.get_prediction(key, pm_name=DATASET)
    except SdkError:
        return None
    return float(prediction["pmPredictedValue"]) if prediction and "pmPredictedValue" in prediction else None


@app.post("/instances/{instance_id}/evaluate")
def evaluate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """One pass of the closed loop over the instance's cells ("Evaluate now"). The pass's
    X-Correlation-ID is its execution id in the audit trail. 409 EVALUATION_IN_PROGRESS while another pass, reconcile or override of the instance runs."""
    inst = _lock_instance(db, instance_id)
    return run_pass(db, inst, get_correlation_id() or str(uuid.uuid4()), "MANUAL")


def run_pass(db: Session, inst: EnergySavingInstance, execution_id: str, trigger: str,
             started_at: datetime.datetime | None = None) -> dict:
    """One pass over the instance's cells (call flow 22a-22d), for `evaluate` (trigger MANUAL) and the worker (SCHEDULED, `run_due_passes`). The caller
    holds the instance's lock (`_lock_instance`); the pass commits once at its end, with `last_evaluated_at` and `last_evaluation`. 409 MODEL_NOT_DEPLOYED
    before the model is deployed."""
    if not inst.model_id or not inst.model_params:
        raise RappError(409, "MODEL_NOT_DEPLOYED", "train, certify and deploy the model first")
    started_at = started_at or datetime.datetime.now(datetime.UTC)
    reconciled = _reconcile(db, inst)
    rows = _cells(db, inst)
    inputs = _inputs(inst, rows)
    model = EnergyModel.from_dict(inst.model_params)
    keys = {cell: cell_key(inst.managed_element_ref, cell) for cell in inst.cells}

    # Prediction — through an AIMgF inference job on the ACTIVE MLIF runtime (TC08)
    job = sdk.lifecycle.request_inference(inst.model_id)
    outputs = {cell: InferenceLogic.infer(model, cell, inputs["series"].get(keys[cell], [])) for cell in inst.cells}
    resolved = sdk.lifecycle.resolve_inference(job["inferenceJobId"], True, inference_outputs=[
        {"aIMLInferenceName": MODEL_TYPE, "outputResult": o} for o in outputs.values() if o])

    decisions: dict[str, EnergySavingDecision] = {}
    results: dict[str, engine.Decision] = {}
    for cell in inst.cells:
        row, key = rows[cell], keys[cell]
        if row.pending_dispatch_id or row.pending_approval_id:
            continue  # ASSIST, or a write held for approval (22d): still waiting for a person
        mdaf = _mdaf_prediction(key)
        g = inputs["guards"].get(key, {})
        neighbours = {n: inputs["series"][n][-1][1] for n in g.get("neighbourRefs") or [] if inputs["series"].get(n)}
        peers_awake = _sector_peers_awake(key, inputs["guards"], inputs["asleep"])
        watched = _cell_and_neighbours(inst, cell, g)
        result = engine.decide(engine.CellInput(
            cell=key, series=inputs["series"].get(key, []), state=row.state, prediction=outputs[cell],
            mdaf_future_prb=mdaf, guards=g, sector_peers_awake=peers_awake,
            neighbour_prb=neighbours, critical_alarm=bool(inputs["alarms"].holding(watched)),
            coverage_alarm=bool(inputs["coverage"].holding(watched)),
            last_unlocked_at=as_utc(row.last_unlocked_at) if row.last_unlocked_at else None,
            override=bool(row.override_by)))
        results[cell] = result
        if result.decision == engine.LOCK:
            inputs["asleep"].add(key)  # a later cell of the same sector group must see this one asleep
        elif result.decision == engine.UNLOCK:
            inputs["asleep"].discard(key)
        decisions[cell] = d = EnergySavingDecision(
            execution_id=execution_id, instance_id=inst.instance_id, cell_id=cell, observed_at=result.observed_at,
            prb=result.prb, decision=result.decision, reason=result.reason, outcome="NONE",
            prediction={"model": outputs[cell], "mdafFuturePrb": mdaf, "inferenceJobId": job["inferenceJobId"],
                        "aimlInferenceReportId": resolved.get("aIMLInferenceReportId"), "lowForMinutes": result.low_for_minutes},
            safety={**result.safety, "criticalAlarmIds": inputs["alarms"].ids_holding(watched), "neighbourPrb": neighbours,
                    "sectorPeersAwake": peers_awake})
        db.add(d)
        if result.decision == engine.NO_CHANGE:
            row.state = result.next_state
            d.final_state = {"state": row.state, "o1": row.o1_value}
    db.flush()

    act = ACTUATORS[inst.actuator]
    locks = [c for c, r in results.items() if r.decision == engine.LOCK]
    unlocks = [c for c, r in results.items() if r.decision == engine.UNLOCK]

    # Idempotency (W10-18): a cell already in the wanted state needs no action
    for cells, wanted, settled in ((locks, act["locked"], engine.SLEEP), (unlocks, act["unlocked"], engine.SERVING)):
        for cell in list(cells):
            if _read(inst, cell) == wanted:
                cells.remove(cell)
                decisions[cell].outcome = "NO_ACTION_ALREADY_IN_STATE"
                rows[cell].state, rows[cell].o1_value = settled, wanted
                decisions[cell].final_state = {"state": settled, "o1": wanted}

    if locks:
        dispatch = sdk.intent.request_autonomy_dispatch(
            inst.instance_id, [_lock_expectation(inst, locks, execution_id)], inst.rmih_id, model_id=inst.model_id,
            notification_destination=inst.operator_notification_uri, user_label=f"energy-saving {execution_id}")
        _follow_dispatch(db, inst, rows, decisions, locks, dispatch, execution_id,
                         max(cast(datetime.datetime, results[c].observed_at) for c in locks))

    if unlocks:
        now = max(cast(datetime.datetime, results[c].observed_at) for c in unlocks)
        if inst.autonomy_mode == "SHADOW":
            for cell in unlocks:
                decisions[cell].outcome = "SHADOWED"
        else:
            restore = _restore(inst, unlocks, execution_id, "WAKE:" + ",".join(sorted({results[c].reason for c in unlocks})))
            for cell in unlocks:
                d, row = decisions[cell], rows[cell]
                if restore["result"] == "AWAITING_APPROVAL" and cell in restore["cells"]:
                    # call flow 22d: the wake waits for a person; the cell stays asleep until `_settle_approvals` reads it back
                    d.action, d.outcome = restore["attempts"][0]["action"], "AWAITING_APPROVAL"
                    row.pending_approval_id, row.pending_decision_id = _uuid_or_none(restore.get("approvalId")), d.decision_id
                    d.final_state = {"state": row.state, "o1": row.o1_value}
                    continue
                d.action = restore["attempts"][0]["action"] if restore["performed"] else None
                d.verification = restore["attempts"][-1]["verification"] if restore["performed"] else restore["verification"]
                d.rollback = {"trigger": "VERIFY_FAILED", **restore} if restore["performed"] and len(restore["attempts"]) > 1 else None
                ok = restore["result"] in ("VERIFIED", "ALREADY_UNLOCKED")
                d.outcome = "EXECUTED" if ok else "VERIFY_FAILED"
                row.state = engine.SERVING if ok else engine.SLEEP
                row.o1_value = act["unlocked"] if ok else row.o1_value
                if ok:
                    row.last_unlocked_at = now
                d.final_state = {"state": row.state, "o1": row.o1_value}
    for row in rows.values():
        row.updated_at = datetime.datetime.now(datetime.UTC)
    inst.last_evaluated_at = started_at
    inst.last_evaluation = {"at": started_at.isoformat(), "executionId": execution_id, "trigger": trigger, "result": "OK"}
    db.commit()
    return {"executionId": execution_id, "instanceId": str(inst.instance_id), "autonomyMode": inst.autonomy_mode,
            "inferenceJobId": job["inferenceJobId"], "reconciled": reconciled,
            "decisions": [_decision_view(decisions[c]) for c in inst.cells if c in decisions]}


@app.post("/instances/{instance_id}/reconcile")
def reconcile(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Follows up ASSIST dispatches the operator has resolved or rejected, and writes held for approval that were decided (call flows 22c, 22d)."""
    settled = _reconcile(db, _lock_instance(db, instance_id))
    db.commit()
    return {"settled": settled}


def run_due_passes(db: Session, now: datetime.datetime | None = None) -> list[dict]:
    """Call flow 22e, the worker's `evaluate-due` task: a pass (trigger SCHEDULED) of every instance with an `evaluateEverySeconds` whose last pass began
    at least that long ago, each acting as its instance and under its own execution id. An instance whose lock another holds is skipped (BUSY, the next
    tick tries again); a pass that fails is rolled back and recorded on the instance (`lastEvaluation` result ERROR, with the error), so it is retried
    after the interval, not on every tick, and the other instances still run. Returns one entry per instance it looked at."""
    now = now or datetime.datetime.now(datetime.UTC)
    scheduled = list(db.scalars(select(EnergySavingInstance.instance_id).where(EnergySavingInstance.evaluate_every_seconds.is_not(None))))
    db.rollback()
    results = []
    for instance_id in scheduled:
        try:
            inst = _lock_instance(db, instance_id)
        except RappError as exc:
            results.append({"instanceId": str(instance_id), "result": "BUSY" if exc.status == 409 else "GONE"})
            continue
        last = as_utc(inst.last_evaluated_at) if inst.last_evaluated_at else None
        if inst.evaluate_every_seconds is None or (last is not None and (now - last).total_seconds() < inst.evaluate_every_seconds):
            db.rollback()
            continue
        execution_id = str(uuid.uuid4())
        token = set_acting_instance(str(instance_id))
        try:
            with correlation_scope(execution_id):
                run_pass(db, inst, execution_id, "SCHEDULED", started_at=now)
            results.append({"instanceId": str(instance_id), "executionId": execution_id, "result": "OK"})
        except Exception as exc:  # noqa: BLE001 — one instance's failure is recorded on it and never stops the others
            db.rollback()
            error = f"{exc.status} {exc.title}" if isinstance(exc, RappError) else \
                f"{exc.status_code} {exc.body}" if isinstance(exc, SdkError) else f"{type(exc).__name__}: {exc}"
            logger.warning("scheduled pass of instance %s failed: %s", instance_id, error)
            failed = db.get_one(EnergySavingInstance, instance_id)
            failed.last_evaluated_at = now
            failed.last_evaluation = {"at": now.isoformat(), "executionId": execution_id, "trigger": "SCHEDULED", "result": "ERROR", "error": error[:500]}
            db.commit()
            results.append({"instanceId": str(instance_id), "executionId": execution_id, "result": "ERROR", "error": error[:500]})
        finally:
            reset_acting_instance(token)
    return results


# ---------------------------------------------------------------- operator override (W10-15)

# Request body of the override route: who is overriding and an optional reason, kept in the audit row's safety note.
class OverrideRequest(BaseModel):
    operator: str
    reason: str | None = None


@app.post("/instances/{instance_id}/cells/{cell_id}/override")
def override(instance_id: uuid.UUID, cell_id: str, body: OverrideRequest, db: Session = Depends(get_session)):
    """Manual UNLOCK: the cell is woken now (if it isn't already UNLOCKED)
    and AI recommendations for it are suppressed until the override is
    cleared. When RAN NF OAM holds the write for a person, the outcome is AWAITING_APPROVAL and `reconcile` settles it (call flow 22d)."""
    inst = _lock_instance(db, instance_id)
    rows = _cells(db, inst)
    if cell_id not in rows:
        raise RappError(404, "CELL_NOT_MANAGED", f"cell {cell_id} is not managed by instance {instance_id}")
    execution_id = get_correlation_id() or str(uuid.uuid4())
    row = rows[cell_id]
    series = by_cell(_dataset(inst, "INFERENCE")).get(cell_key(inst.managed_element_ref, cell_id), [])
    now = series[-1][0] if series else datetime.datetime.now(datetime.UTC)
    restore = _restore(inst, [cell_id], execution_id, "OPERATOR_OVERRIDE")
    awaiting = restore["result"] == "AWAITING_APPROVAL"
    ok = restore["result"] in ("VERIFIED", "ALREADY_UNLOCKED")
    row.override_by, row.override_at = body.operator, datetime.datetime.now(datetime.UTC)
    if ok:
        row.state, row.o1_value = engine.SERVING, ACTUATORS[inst.actuator]["unlocked"]
        if restore["performed"]:
            row.last_unlocked_at = now
    d = EnergySavingDecision(
        execution_id=execution_id, instance_id=inst.instance_id, cell_id=cell_id, observed_at=now,
        prb=series[-1][1] if series else None, decision=engine.UNLOCK, reason=f"OPERATOR_OVERRIDE:{body.operator}",
        outcome="AWAITING_APPROVAL" if awaiting else ("EXECUTED" if restore["performed"] else "NO_ACTION_ALREADY_IN_STATE") if ok else "VERIFY_FAILED",
        safety={"passed": True, "blocks": [], "note": body.reason},
        action=restore["attempts"][0]["action"] if restore["performed"] else None,
        verification=restore["attempts"][-1]["verification"] if restore["performed"] else restore["verification"],
        final_state={"state": row.state, "o1": row.o1_value})
    db.add(d)
    if awaiting:
        db.flush()
        row.pending_approval_id, row.pending_decision_id = _uuid_or_none(restore.get("approvalId")), d.decision_id
    db.commit()
    return _decision_view(d)


@app.delete("/instances/{instance_id}/cells/{cell_id}/override", status_code=204)
def clear_override(instance_id: uuid.UUID, cell_id: str, db: Session = Depends(get_session)):
    # Releases the cell from its operator override so AI recommendations apply again; 204 whether or not the cell is managed or was overridden.
    row = _cells(db, _instance(db, instance_id)).get(cell_id)
    if row is not None:
        row.override_by = row.override_at = None
        db.commit()


# ---------------------------------------------------------------- audit + dashboard (W10-23, W10-24)

def _decision_view(d: EnergySavingDecision) -> dict:
    """The JSON shape of one audit row, as the decisions list, the evaluate answer and the dashboard return it."""
    return {"decisionId": str(d.decision_id), "executionId": d.execution_id, "cellId": d.cell_id,
            "observedAt": d.observed_at.isoformat() if d.observed_at else None, "prb": d.prb,
            "prediction": d.prediction, "safety": d.safety, "decision": d.decision, "reason": d.reason,
            "intent": d.intent, "action": d.action, "verification": d.verification, "rollback": d.rollback,
            "outcome": d.outcome, "finalState": d.final_state, "createdAt": d.created_at.isoformat()}


@app.get("/instances/{instance_id}/decisions")
def list_decisions(instance_id: uuid.UUID, cell_id: str | None = None, execution_id: str | None = None,
                   limit: int = 100, db: Session = Depends(get_session)):
    # Audit rows of an instance, newest first; filtered by cell_id and execution_id when given; `limit` is capped at 500.
    stmt = select(EnergySavingDecision).where(EnergySavingDecision.instance_id == instance_id)
    if cell_id:
        stmt = stmt.where(EnergySavingDecision.cell_id == cell_id)
    if execution_id:
        stmt = stmt.where(EnergySavingDecision.execution_id == execution_id)
    rows = db.scalars(stmt.order_by(EnergySavingDecision.created_at.desc()).limit(min(limit, 500))).all()
    return {"items": [_decision_view(d) for d in rows]}


@app.get("/instances/{instance_id}/cells")
def list_cells(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # The instance's cells with their state and override; creates the missing rows and commits, so a freshly started instance lists all its cells.
    inst = _instance(db, instance_id)
    rows = _cells(db, inst)
    db.commit()
    return {"items": [_cell_view(rows[c]) for c in inst.cells]}


def _cell_view(r: EnergySavingCell) -> dict:
    return {"cellId": r.cell_id, "state": r.state, "o1Value": r.o1_value,
            "lastUnlockedAt": r.last_unlocked_at.isoformat() if r.last_unlocked_at else None,
            "overrideBy": r.override_by, "pendingDispatchId": str(r.pending_dispatch_id) if r.pending_dispatch_id else None}


@app.get("/instances/{instance_id}/dashboard")
def dashboard(instance_id: uuid.UUID, points: int = 48, db: Session = Depends(get_session)):
    """Everything the operator must see for every execution (§19): PRB
    trend, prediction, safety, decision, intent, action, verification,
    rollback and the final cell state."""
    inst = _instance(db, instance_id)
    rows = _cells(db, inst)
    series = by_cell(_dataset(inst, "INFERENCE"))
    cells = []
    for cell in inst.cells:
        latest = db.scalars(select(EnergySavingDecision).where(EnergySavingDecision.instance_id == instance_id,
                                                               EnergySavingDecision.cell_id == cell)
                            .order_by(EnergySavingDecision.created_at.desc()).limit(1)).first()
        trend = series.get(cell_key(inst.managed_element_ref, cell), [])[-points:]
        cells.append({**_cell_view(rows[cell]), "prbTrend": [{"t": t.isoformat(), "v": v} for t, v in trend],
                      "latestDecision": _decision_view(latest) if latest else None})
    db.commit()
    return {"instance": _instance_view(inst), "cells": cells}


# ---------------------------------------------------------------- cell-state history (PR-GUI-9.8b)
#
# Derived from the audit trail, not a table of its own: every change of a cell's state is settled in a decision row's `final_state` (an evaluation
# pass, the follow-up of an ASSIST dispatch, an operator override), and a cell with a pending dispatch gets no newer row until it is settled. So a
# cell's rows in `created_at` order, read for their `final_state`, are its state history; the time of a change is the row's `updated_at` (when the
# final state was settled, wall clock), and `observedAt` beside it is the PRB sample's own (simulation) time.

CELL_STATE_LEVEL = {engine.SLEEP: 0, engine.PRE_SLEEP: 1, engine.SERVING: 2}   # the chart's y: 2 serving, 1 pre-sleep, 0 sleep
CELL_STATE_MAX_HOURS = 168
CELL_STATE_MAX_ROWS = 2000      # decision rows read for one answer; past it the oldest are left out and `truncated` is true
CELL_STATE_LOOKBACK = 50        # rows read per cell to find its state before the window


def _settled_at(d: EnergySavingDecision) -> datetime.datetime:
    """When a decision row's final state was settled (its `updated_at`, falling back to `created_at`), as an aware UTC datetime."""
    return as_utc(d.updated_at or d.created_at)


def _state_before(db: Session, inst: EnergySavingInstance, cell: str, created_before: datetime.datetime | None,
                  settled_before: datetime.datetime) -> str | None:
    """The state a cell was in before the window: the `final_state` of its newest settled row created before `created_before` (when the window's rows
    of the cell start there) or settled before `settled_before`. SERVING (a cell's first state) when it has no such row at all; None when none of
    the last CELL_STATE_LOOKBACK rows recorded a state."""
    stmt = select(EnergySavingDecision).where(EnergySavingDecision.instance_id == inst.instance_id, EnergySavingDecision.cell_id == cell)
    stmt = stmt.where(EnergySavingDecision.created_at < created_before) if created_before is not None \
        else stmt.where(EnergySavingDecision.updated_at < settled_before)
    rows = db.scalars(stmt.order_by(EnergySavingDecision.created_at.desc()).limit(CELL_STATE_LOOKBACK)).all()
    if not rows:
        return engine.SERVING
    return next(((d.final_state or {}).get("state") for d in rows if (d.final_state or {}).get("state")), None)


@app.get("/instances/{instance_id}/cell-states")
def cell_state_history(instance_id: uuid.UUID, hours: int = Query(24, ge=1, le=CELL_STATE_MAX_HOURS), cell_id: str | None = None,
                       db: Session = Depends(get_session)):
    """GUI-9.8b, each managed cell's state transitions over the last `hours` (1 to 168, default 24), optionally for one `cell_id` (404 when the
    instance does not manage it). Answers `{instanceId, since, until, truncated, transitions, points, cells}`:

      * `transitions`, newest first: `{transitionId, cellId, at, fromState, toState, o1Value, decision, reason, outcome, executionId, observedAt}`,
        one per change of state (SERVING / PRE_SLEEP / SLEEP) settled in the window;
      * `points`, oldest first, for a chart with one series per cell: `{cellId, t, state, level}` (level 2 serving, 1 pre-sleep, 0 sleep), the
        state at `since`, two points at each change (before and after, the same `t`) and the state at `until`;
      * `cells`: `{cellId, state, transitions}`, the current state and the number of changes in the window.

    Derived from the decision audit trail (see above). At most CELL_STATE_MAX_ROWS rows are read; past that the oldest are left out, `truncated` is
    true and `since` moves up to the oldest row kept."""
    inst = _instance(db, instance_id)
    if cell_id is not None and cell_id not in inst.cells:
        raise RappError(404, "CELL_NOT_MANAGED", f"cell {cell_id} is not managed by instance {instance_id}")
    cells = [cell_id] if cell_id is not None else list(inst.cells)
    current = _cells(db, inst)
    until = datetime.datetime.now(datetime.UTC)
    since = until - datetime.timedelta(hours=hours)
    stmt = (select(EnergySavingDecision).where(EnergySavingDecision.instance_id == inst.instance_id, EnergySavingDecision.cell_id.in_(cells),
                                               EnergySavingDecision.updated_at >= since)
            .order_by(EnergySavingDecision.created_at.desc()).limit(CELL_STATE_MAX_ROWS + 1))
    rows = list(db.scalars(stmt))
    truncated = len(rows) > CELL_STATE_MAX_ROWS
    rows = list(reversed(rows[:CELL_STATE_MAX_ROWS]))
    if truncated and rows:
        since = min(_settled_at(d) for d in rows)
    by_cell: dict[str, list[EnergySavingDecision]] = {c: [] for c in cells}
    for d in rows:
        by_cell[d.cell_id].append(d)

    transitions: list[dict] = []
    points: list[tuple[datetime.datetime, int, dict]] = []
    summary = []
    for order, cell in enumerate(cells):
        mine = by_cell[cell]
        state = _state_before(db, inst, cell, as_utc(mine[0].created_at) if mine else None, since)
        if state in CELL_STATE_LEVEL:
            points.append((since, order, {"cellId": cell, "t": since.isoformat(), "state": state, "level": CELL_STATE_LEVEL[state]}))
        changes = 0
        for d in mine:
            to = (d.final_state or {}).get("state")
            if not to or to == state:
                continue
            at = _settled_at(d)
            transitions.append({"transitionId": str(d.decision_id), "cellId": cell, "at": at.isoformat(), "fromState": state, "toState": to,
                                "o1Value": (d.final_state or {}).get("o1"), "decision": d.decision, "reason": d.reason, "outcome": d.outcome,
                                "executionId": d.execution_id, "observedAt": as_utc(d.observed_at).isoformat() if d.observed_at else None})
            for value in (state, to):
                if value in CELL_STATE_LEVEL:
                    points.append((at, order, {"cellId": cell, "t": at.isoformat(), "state": value, "level": CELL_STATE_LEVEL[value]}))
            state, changes = to, changes + 1
        now_state = state or current[cell].state
        if now_state in CELL_STATE_LEVEL:
            points.append((until, order, {"cellId": cell, "t": until.isoformat(), "state": now_state, "level": CELL_STATE_LEVEL[now_state]}))
        summary.append({"cellId": cell, "state": current[cell].state, "transitions": changes})
    db.commit()  # `_cells` may have added the row of a cell that never had one
    points.sort(key=lambda p: p[0])  # stable: a cell's before/after pair at one change keeps its order
    transitions.sort(key=lambda t: t["at"], reverse=True)
    return {"instanceId": str(inst.instance_id), "since": since.isoformat(), "until": until.isoformat(), "truncated": truncated,
            "transitions": transitions, "points": [p for _, _, p in points], "cells": summary}


# ---------------------------------------------------------------- Digital Twin producer (W10-05)

@app.post("/sim-producer/register", status_code=201)
def register_sim_producer():
    # Registers the PRB_UTILIZATION_SIM data type at DME with this rApp as the Digital Twin producer (201).
    return register_sim_type(sdk)


@app.post("/sim-producer/publish")
def publish_sim_data(body: SimPublishRequest):
    # Generates the requested cells' synthetic PRB samples and delivers them to every data job of the sim type.
    return publish_sim(sdk, body)



# ---------------------------------------------------------------- demo / simulation (docs/GUI_DEMO_GUIDE.md)
# What demo.py does around the rApp, as routes the operator page declares as admin-only buttons (`role: admin` in manifest.yaml), so the demo runs from
# the console: the rApp plays the network (PM reports into RAN NF OAM, as the NF would send them) and the Digital Twin. Simulation time, as demo.py's:
# history from 2026-09-01, live PM from midnight on the 4th.

DEMO_HISTORY_START = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
DEMO_LIVE_START = DEMO_HISTORY_START + datetime.timedelta(days=3)


# How many measurements one demo PM report carries (`_report_pm`).
PM_REPORT_CHUNK = 96


def _report_pm(inst: EnergySavingInstance, values: dict[str, list[float]], start: datetime.datetime, step_minutes: int) -> int:
    """Sends `values` (cell -> one PRB value per step from `start`) to RAN NF OAM as PM reports of the instance's element; returns how many
    measurements went. A refusal (no PM subscription, an element outside the rApp's scope) is answered as RAN NF OAM gave it. The caller's read
    transaction is ended first (`db.commit()` by the caller), as RAN NF OAM does before it fans out: nothing of ours is held while others write."""
    measurements = [{"cellId": c, "value": v, "timestamp": (start + datetime.timedelta(minutes=i * step_minutes)).isoformat()}
                    for c, series in values.items() for i, v in enumerate(series)]
    # in reports of at most PM_REPORT_CHUNK measurements: RAN NF OAM delivers each one to every open data job, one DME record at a time, so a whole
    # history in one report outlasts the 30 s this rApp's R1 call allows (call it at least once, as a single report did)
    for first in range(0, max(len(measurements), 1), PM_REPORT_CHUNK):
        chunk = measurements[first:first + PM_REPORT_CHUNK]
        resp = _r1.post("/ran-nf-oam/pm-reports", json=pm_report(inst.managed_element_ref, chunk))
        if resp.status_code != 201:
            raise SdkError(resp.status_code, resp.json() if resp.content else None)
    return len(measurements)


@app.post("/instances/{instance_id}/demo/prepare")
def demo_prepare(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 00-01, the rApp's part: registers the Digital Twin producer at DME (DME takes the same registration again as it is) and starts the
    instance (`/start`), which subscribes it to its datasets."""
    producer = register_sim_type(sdk)
    return {"simProducer": producer.get("dmeTypeId"), "instance": start_instance(instance_id, db)}


@app.post("/instances/{instance_id}/demo/history")
def demo_history(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 02: three days of hourly PRB history for every cell, reported to RAN NF OAM, which delivers it to DME for the training dataset."""
    inst = _instance(db, instance_id)
    db.commit()                                            # end the read transaction before RAN NF OAM writes to DME
    values = {c: [diurnal_prb(c, DEMO_HISTORY_START + datetime.timedelta(hours=h)) for h in range(72)] for c in inst.cells}
    sent = _report_pm(inst, values, DEMO_HISTORY_START, 60)
    return {"measurements": sent, "from": DEMO_HISTORY_START.isoformat(), "datasets": {k: v["dataset"] for k, v in (inst.data_jobs or {}).items()}}


@app.post("/instances/{instance_id}/demo/emulate")
def demo_emulate(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 05: the Digital Twin publishes two days of its trend, then the model is emulated against it (`lifecycle/emulate`)."""
    published = publish_sim(sdk, SimPublishRequest(managedElementRef="digital-twin-01", cells=["dt-1", "dt-2"], start=DEMO_HISTORY_START, hours=48))
    return {"digitalTwin": published, **emulate(instance_id, db)}


@app.post("/instances/{instance_id}/demo/live")
def demo_live(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """Demo 08's PM: an hour after midnight in five-minute steps, every other cell (the first, the third, ...) idle at 2 % PRB and the rest at 40 %.
    Then press Evaluate now: the idle cells are put to sleep, unless guarded."""
    inst = _instance(db, instance_id)
    db.commit()                                            # end the read transaction before RAN NF OAM writes to DME
    values = {c: [2.0 if i % 2 == 0 else 40.0] * 14 for i, c in enumerate(inst.cells)}
    return {"measurements": _report_pm(inst, values, DEMO_LIVE_START, 5), "from": DEMO_LIVE_START.isoformat(),
            "idleCells": [c for i, c in enumerate(inst.cells) if i % 2 == 0]}

app.include_router(producer_router)

# re-exported for tests/demo helpers
__all__ = ["app", "parse_time"]
