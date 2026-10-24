"""Generic O1-CM intent handler — Wave 8 (HISTORY.md W8-07,
decision D-1).

SA SMOS registers itself with Intent Service as an IntentHandlingFunction
(RMIH `sa-smos`) for RAN_SUBNETWORK expectations whose targets are O1 CM
attributes, named `<IOC>.<attribute>` (TS 28.541 class and attribute), e.g.
`NRCellDU.administrativeState` or `CESManagementFunction.energySavingControl`.
Those are generic TS 28.312 ExpectationTargets (the families allow any
target name beside their specialised ones), so any rApp can express
"set this CM attribute on these cells" as an Intent, and an AUTONOMOUS or
operator-resolved ASSIST AutonomyDispatch becomes a real O1 change.

When Intent Service pushes a new Intent here, the handler:

  1. reads the Intent back (Intent Service is the source of truth);
  2. for each expectation, takes `expectationObject.objectInstance` as the
     managed element and its `Cell` object context (if any) as the cells,
     and turns every `IS_EQUAL_TO` CM target into an attribute change per
     cell (`managedFunctionRef` = `<IOC>=<cell>`);
  3. writes them through DME's O1 action mediation (`POST /dme/actions`,
     which forwards to RAN NF OAM's config jobs — the same audited path a
     manual CM write takes), one action per expectation, carrying the
     intent/expectation ids as its source context;
  4. publishes the Intent's IntentReport: FULFILLED when every change was
     applied, otherwise NOT_FULFILLED / DEGRADED, with the DME action and
     RAN NF OAM job references in `additionalFulfilmentInfo` (W6-04).

Each enactment is recorded (`GET /o1-cm-handler/enactments`), once per intent (SA-REPLAY): Intent Service pushes an intent only when it is
created, and its outbox sends that push again when it got no answer in time, so a push for an intent already enacted is answered with the
recorded enactment (`replayed: true`) and nothing is written or reported again. A unique index on `intent_id` (revision 0062) settles two pushes
that arrive at once: the one whose row is second rolls back and answers the first one's.
"""

import json
import os
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared import mtls
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.pagination import PageLimit, PageOffset, paginate
from smo_shared.r1_client import R1Client

from .models import O1CmEnactment

# The routes of the O1-CM handler; main.py includes this router.
router = APIRouter()
# One R1 client for the module, as everywhere in this build; calls go through R1 Termination.
_r1 = R1Client()

# The RMIH id this handler registers under at Intent Service, and the `requestedBy` attributed to the DME actions it issues.
RMIH_ID = "sa-smos"
REQUESTED_BY = "sa-smos:o1-cm-intent-handler"
# Where Intent Service pushes new Intents for this handler (in-cluster).
# mtls.http_url switches the scheme to https when mTLS is on; the URL can be overridden with SA_SMOS_O1_CM_HANDLER_URL.
HANDLER_URL = mtls.http_url(os.environ.get("SA_SMOS_O1_CM_HANDLER_URL", "http://sa-smos:8000/o1-cm-handler/intents"))

# The CM targets this handler enacts by default, with their allowed values
# (TS 28.541 / TS 28.623 enums). A registration may declare others.
DEFAULT_CM_TARGETS = {
    "NRCellDU.administrativeState": ["LOCKED", "UNLOCKED"],
    "CESManagementFunction.energySavingControl": ["TO_BE_ENERGY_SAVING", "TO_BE_NOT_ENERGY_SAVING"],
    # Wave 10.2 (W10.2-06): a neighbour relation's CIO. The value (six
    # QOffsetRange dB entries) isn't enumerable here — RAN NF OAM's schema
    # pre-check validates the write against the vendor's data model.
    "NRCellRelation.cellIndividualOffset": [],
    # Wave 10.3 (W10.3-06): a cell's coverage knobs (integers, schema-checked
    # by RAN NF OAM like the CIO)
    "CommonBeamformingFunction.digitalTilt": [],
    "NRSectorCarrier.configuredMaxTxPower": [],
    # Wave 10.4 (W10.4-06): a cell's idle-mode reselection priority towards a
    # frequency layer (Cell-context values name NRFreqRelation=<cell>-<layer>)
    "NRFreqRelation.cellReselectionPriority": [],
}
# The DME action statuses that count as applied. Any other status (including HTTP_<code> when DME did not answer 200 or 202) leaves the expectation NOT_FULFILLED.
SUCCESS_STATUSES = {"COMPLETED"}
# Fixed namespace of the uuid5 that derives each DME action id from the intent and expectation ids. Changing it changes every action id and breaks the replay protection for intents already enacted.
ACTION_ID_NAMESPACE = uuid.UUID("6f2b8c1e-3d4a-5b6c-8d9e-0a1b2c3d4e5f")


# Request body of POST /o1-cm-handler/registration. Both fields are optional: `cmTargets` maps "<IOC>.<attribute>" to its allowed values (an empty list means any value) and defaults to
# DEFAULT_CM_TARGETS; `intentHandlingScope` defaults to ["RAN"]. Unknown fields are refused (extra="forbid").
class RegistrationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmTargets: dict[str, list[str]] | None = None  # "<IOC>.<attribute>" -> allowed values
    intentHandlingScope: list[str] = ["RAN"]


# What Intent Service pushes to this handler for a new Intent: only the intent id. The intent itself is read back from Intent Service.
class IntentNotification(BaseModel):
    """What Intent Service pushes to a handling function on CreateIntent."""
    intentId: uuid.UUID


def _cm_targets_from_registration() -> dict[str, list[str]]:
    """The CM targets (name -> allowed values) as currently registered with Intent Service, whose capability list is the source of truth; DEFAULT_CM_TARGETS when it is not registered or the lookup fails.

    Any exception is swallowed on purpose and the defaults are used.
    """
    try:
        resp = _r1.get("/intent-service/intent-handling-functions")
        if resp.status_code == 200:
            for fn in resp.json().get("items", []):
                if fn["rmihId"] == RMIH_ID:
                    targets = {}
                    for cap in fn["attributes"]["intentHandlingCapabilityList"]:
                        for t in cap.get("supportedExpectationTargetInfoList") or []:
                            targets[t["supportedTargetName"]] = t.get("supportedTargetValueRange") or []
                    return targets
    except Exception:  # noqa: BLE001, S110 — fall back to the defaults
        pass
    return DEFAULT_CM_TARGETS


@router.post("/o1-cm-handler/registration", status_code=201)
def register_o1_cm_handler(body: RegistrationBody = RegistrationBody()):
    """Registers SA SMOS with Intent Service as the O1-CM intent handling
    function. Idempotent: an existing registration is updated in place,
    and the intents addressed to it stay."""
    # Maintainer notes (not published). 201 with {rmihId, cmTargets, notificationDestination}; 422 SCHEMA_VALIDATION_FAILED for a target name without a '.' or when Intent Service does not answer 201.
    # One POST: Intent Service updates an existing registration in place (call flow 09c), so re-registering keeps every intent addressed to the handler (before
    # DOC-CF-1.8 it deleted the handler first, and the database cascade deleted every one of them). `notificationDestination` is HANDLER_URL, where Intent Service pushes new intents.
    targets = body.cmTargets or DEFAULT_CM_TARGETS
    for name in targets:
        if "." not in name:
            raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"CM target {name!r} must be <IOC>.<attribute>")
    resp = _r1.post("/intent-service/intent-handling-functions", json={
        "rmihId": RMIH_ID, "smeServiceId": "sa-smos-o1-cm-intent-handler", "notificationDestination": HANDLER_URL,
        "intentHandlingScope": body.intentHandlingScope,
        "intentHandlingCapabilityList": [{
            "intentHandlingCapabilityId": "o1-cm", "supportedExpectationObjectType": "RAN_SUBNETWORK",
            "supportedExpectationTargetInfoList": [
                {"supportedTargetName": name, "supportedTargetCondition": "IS_EQUAL_TO", "supportedTargetValueRange": values}
                for name, values in targets.items()],
        }],
    })
    if resp.status_code != 201:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"Intent Service refused registration: {resp.json()}")
    return {"rmihId": RMIH_ID, "cmTargets": targets, "notificationDestination": HANDLER_URL}


@router.delete("/o1-cm-handler/registration", status_code=204)
def deregister_o1_cm_handler():
    # Maintainer notes (not published). Deletes the RMIH at Intent Service. Its refusal is passed on (call flow 09c): 409 INTENT_HANDLING_FUNCTION_IN_USE while
    # intents are addressed to the handler (they are deleted first); any other answer but 204 or 404 is 502 UPSTREAM_FAILED.
    resp = _r1.delete(f"/intent-service/intent-handling-functions/{RMIH_ID}")
    if resp.status_code == 409:
        raise framework_error(FrameworkError.INTENT_HANDLING_FUNCTION_IN_USE, detail=resp.json().get("detail", {}).get("detail", "intents are addressed to the handler"))
    if resp.status_code not in (204, 404):
        raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"Intent Service answered {resp.status_code} to the deregistration")


def _cells(expectation: dict) -> list[str]:
    """The cell identifiers of an expectation, from its `Cell` object contexts, as strings.

    `contextValueRange` may be a list or a single value; a dict value is reduced to its `cellLocalId`, else `nCI`, else `id`. Contexts for other attributes and values that resolve to None are skipped.
    """
    cells = []
    for ctx in (expectation["expectationObject"].get("objectContexts") or []):
        if ctx.get("contextAttribute") != "Cell":
            continue
        values = ctx.get("contextValueRange")
        for v in values if isinstance(values, list) else [values]:
            if isinstance(v, dict):
                v = v.get("cellLocalId", v.get("nCI", v.get("id")))
            if v is not None:
                cells.append(str(v))
    return cells


def _changes_for(expectation: dict, cm_targets: dict[str, list[str]]) -> tuple[list[dict], list[dict]]:
    """Returns (CM attribute changes, unsupported targets) for one expectation.

    Each target is `<IOC>.<attribute>`. A target is unsupported, with the first reason that applies, when its name is not in `cm_targets`, its condition is not IS_EQUAL_TO, its value is not in the allowed list
    (an empty list allows any value; RAN NF OAM's schema pre-check validates those), or the expectation has no `objectInstance` (the managed element). A supported target gives one change per cell of the
    expectation, with `managedFunctionRef` `<IOC>=<cell>`, or a single change without a function ref when the expectation names no cell.
    """
    element = expectation["expectationObject"].get("objectInstance")
    changes, unsupported = [], []
    for target in expectation["expectationTargets"]:
        name, value = target["targetName"], target["targetValueRange"]
        allowed = cm_targets.get(name)
        reason = None
        if allowed is None:
            reason = "not a CM target of this handler"
        elif target["targetCondition"] != "IS_EQUAL_TO":
            reason = "CM targets are set with IS_EQUAL_TO"
        elif allowed and value not in allowed:
            reason = f"value must be one of {allowed}"
        elif not element:
            reason = "expectationObject.objectInstance (the managed element) is required"
        if reason:
            unsupported.append({"expectationId": expectation["expectationId"], "targetName": name, "reason": reason})
            continue
        ioc, attribute = name.split(".", 1)
        # No cell context means one change for the managed element itself; None stands for that case.
        cells: list[str | None] = [*_cells(expectation)] or [None]
        for cell in cells:
            change = {"managedElementRef": element, "className": ioc, "attributeChanges": {attribute: value}}
            if cell is not None:
                change["managedFunctionRef"] = f"{ioc}={cell}"
            changes.append(change)
    return changes, unsupported


@router.post("/o1-cm-handler/intents")
def enact_intent(body: IntentNotification, db: Session = Depends(get_session)):
    """Intent Service's push for a new Intent addressed to this handler."""
    # Maintainer notes (not published). Intent Service's push. Answers the enactment view with `replayed` (false for the push that enacted); {status: SKIPPED} (nothing recorded, no
    # report) for a DEACTIVATED intent; 404 INTENT_NOT_FOUND when Intent Service does not know the intent; 422 RMIH_CAPABILITY_MISMATCH when it is addressed to another handler.
    # SA-REPLAY: an intent already enacted is answered from its record with `replayed: true`, before anything is read, written or reported.
    # For every expectation: derive the changes, post one DME action (if there are any), then build the fulfilment result. After the loop the enactment row is flushed first, so the
    # unique index on intent_id decides a concurrent push before a second report goes out: the loser (IntegrityError) rolls back and answers the winner's record, replayed. Then one
    # IntentReport is published and its id (null when the publish failed) committed with the row. A transport exception from R1Client is not caught: the request is a 500 and no
    # enactment is recorded, so Intent Service's retry enacts it again (the DME action ids are the same, and DME ignores the repeat).
    recorded = _enactment_of(db, body.intentId)
    if recorded is not None:
        return {**_enactment_view(recorded), "replayed": True}
    resp = _r1.get(f"/intent-service/intents/{body.intentId}")
    if resp.status_code != 200:
        raise framework_error(FrameworkError.INTENT_NOT_FOUND, detail=f"no such intent {body.intentId}")
    intent = resp.json()
    if intent["rmihId"] != RMIH_ID:
        raise framework_error(FrameworkError.RMIH_CAPABILITY_MISMATCH, detail="intent is addressed to another handler")
    if intent["intentAdminState"] != "ACTIVATED":
        return {"intentId": str(body.intentId), "status": "SKIPPED", "reason": "intent is DEACTIVATED"}

    # Read from Intent Service on every push, so a registration with other targets is honoured without a restart.
    cm_targets = _cm_targets_from_registration()
    actions, unsupported, expectation_results = [], [], []
    for expectation in intent["attributes"]["intentExpectations"]:
        changes, bad = _changes_for(expectation, cm_targets)
        unsupported.extend(bad)
        applied = None
        if changes:
            # Wave 10.1 (W10-18): the action id is derived from the intent and
            # expectation, so a re-pushed Intent replays the same action —
            # which DME ignores — instead of writing the change twice.
            action = _r1.post("/dme/actions", json={
                "requestedBy": REQUESTED_BY, "changes": changes,
                "actionId": str(uuid.uuid5(ACTION_ID_NAMESPACE, f"{body.intentId}:{expectation['expectationId']}")),
                "sourceContext": {"intentId": str(body.intentId), "expectationId": expectation["expectationId"],
                                  "rmioId": intent["rmioId"]},
            })
            # Only a 200 or 202 answer is read as an action body; any other status is recorded as HTTP_<code>.
            payload = action.json() if action.status_code in (200, 202) else {"status": f"HTTP_{action.status_code}"}
            # DME answers a replayed action with status IGNORED and the first execution's status as originalStatus; that first status decides fulfilment, and `replayed` records that it was a replay.
            status = payload.get("originalStatus") if payload.get("status") == "IGNORED" else payload.get("status")
            actions.append({"expectationId": expectation["expectationId"], "actionId": payload.get("actionId"),
                            "forwardedJobId": payload.get("forwardedJobId"), "status": status,
                            "replayed": payload.get("status") == "IGNORED"})
            applied = status in SUCCESS_STATUSES
        # An expectation is fulfilled only when its action was applied and none of its targets was unsupported. A supported target in an expectation with an unsupported one is still written (the action went out) but the expectation is reported NOT_FULFILLED.
        bad_names = {b["targetName"] for b in bad}
        ok = applied is True and not bad_names
        info = {"fulfilmentStatus": "FULFILLED"} if ok else {"fulfilmentStatus": "NOT_FULFILLED", "notFullfilledState": "DEGRADED"}
        expectation_results.append({
            # The keys `expectaitonId` and `notFullfilledState` are misspelled on purpose: that is how TS28312_IntentNrm.yaml spells them (specs/5G_APIs), and the report keeps the spec's names.
            "expectaitonId": expectation["expectationId"], "expectationFulfilmentInfo": info,
            "targetFulfilmentResults": [
                {"targetName": t["targetName"],
                 "targetFulfilmentInfo": {"fulfilmentStatus": "FULFILLED"} if (applied and t["targetName"] not in bad_names)
                 else {"fulfilmentStatus": "NOT_FULFILLED", "notFullfilledState": "DEGRADED"},
                 **({"targetAchievedValue": t["targetValueRange"]} if (applied and t["targetName"] not in bad_names) else {})}
                for t in expectation["expectationTargets"]],
        })

    # The intent is FULFILLED only when every expectation is.
    fulfilled = all(r["expectationFulfilmentInfo"]["fulfilmentStatus"] == "FULFILLED" for r in expectation_results)
    status = "FULFILLED" if fulfilled else "NOT_FULFILLED"
    report = {"intentReference": str(body.intentId), "intentFulfilmentReport": {
        "intentFulfilmentInfo": {"fulfilmentStatus": "FULFILLED"} if fulfilled else {
            "fulfilmentStatus": "NOT_FULFILLED", "notFullfilledState": "DEGRADED",
            "notFulfilledReasons": [f"{u['targetName']}: {u['reason']}" for u in unsupported]
                                   + [f"action {a['actionId']} {a['status']}" for a in actions if a["status"] not in SUCCESS_STATUSES]},
        "expectationFulfilmentResult": expectation_results,
        "additionalFulfilmentInfo": json.dumps({"actions": actions}),
    }}
    enactment = O1CmEnactment(intent_id=body.intentId, status=status, actions=actions, unsupported_targets=unsupported)
    db.add(enactment)
    try:
        db.flush()
    except IntegrityError:
        # another push of the same intent recorded its enactment first (Postgres makes this insert wait for it): answer that one, report nothing
        db.rollback()
        winner = _enactment_of(db, body.intentId)
        if winner is None:          # the violation was not the per-intent index: not a lost race
            raise
        return {**_enactment_view(winner), "replayed": True}
    # Failing to publish the report is not an error of the push: the enactment is still recorded, with a null intent_report_id.
    published = _r1.post("/intent-service/intent-reports", json=report)
    report_id = published.json().get("reportId") if published.status_code == 201 else None
    enactment.intent_report_id = uuid.UUID(report_id) if report_id else None
    db.commit()
    return {**_enactment_view(enactment), "replayed": False}


def _enactment_of(db: Session, intent_id: uuid.UUID) -> O1CmEnactment | None:
    """The recorded enactment of the intent, or None. One at most since revision 0062; the oldest if a database still held more."""
    return db.scalars(select(O1CmEnactment).where(O1CmEnactment.intent_id == intent_id).order_by(O1CmEnactment.created_at).limit(1)).first()


def _enactment_view(e: O1CmEnactment) -> dict:
    """The JSON view of an enactment record, with ids as strings and `createdAt` in ISO 8601."""
    return {"enactmentId": str(e.enactment_id), "intentId": str(e.intent_id), "status": e.status, "actions": e.actions,
            "unsupportedTargets": e.unsupported_targets,
            "intentReportId": str(e.intent_report_id) if e.intent_report_id else None, "createdAt": e.created_at.isoformat()}


@router.get("/o1-cm-handler/enactments")
def list_enactments(intent_id: uuid.UUID | None = None, limit: int = PageLimit, offset: int = PageOffset,
                    db: Session = Depends(get_session)):
    # Maintainer notes (not published). Newest first (created_at descending); optional exact-match filter `intent_id`; paginated.
    stmt = select(O1CmEnactment)
    if intent_id:
        stmt = stmt.where(O1CmEnactment.intent_id == intent_id)
    page = paginate(db, stmt.order_by(O1CmEnactment.created_at.desc()), limit, offset)
    return {**page, "items": [_enactment_view(e) for e in page["items"]]}
