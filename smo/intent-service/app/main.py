"""Intent Service: the FastAPI module for TS 28.312 intents, their reports, intent handling functions (RMIHs), utility formulas and rApp autonomy
dispatches, served at `/intent-service` behind R1 Termination.

What it is: every route of the service and the helpers behind them. An intent owner (an rApp or the GUI, the RMIO) addresses an intent to one RMIH it
chose by `rmihId`; this module validates it, checks it against what that RMIH declares it can handle, stores it with an initial report, queues a push to
the RMIH, and delivers every later report to the recipients named in the intent's report control. An autonomy dispatch (`HISTORY.md` OI-6.3) is the
record that turns an AI/ML inference outcome into an intent according to the rApp instance's autonomy mode. Enactment belongs to the RMIH, not here.
Design record: `intent-service/README.md`; SMO Design v1.3 section 3.12 and Policy Mgmt LLD sections 1-3 (this module was Policy Management & Info SMOS
before it was renamed; `docs/ARCHITECTURE.md`, Intent Service).

Where it sits: rApps (through `sdk.intent`), the GUI BFF and SA SMOS call the routes through R1 Termination. The only outbound read is
`GET /rapp-mgmt/instances/{id}` (through `R1Client`, autonomy dispatch only). Pushes and reports to an RMIH, a report recipient or an operator are outbox rows
(`smo_shared.outbox`), never a direct HTTP call from a route. The spec models live in `ts28312.py`; this file does not redefine them.

Owns: the capability, feasibility and conflict rules that decide whether an intent is accepted, the initial report, report delivery, the admin-state
rule (only the creating RMIO may change it), the identity rules of call flow 09 (DOC-CF-1.8: an rApp manages only the intents it owns, dispatches only for
its own instance, and registers no handler, publishes no report and decides no ASSIST dispatch) and the autonomy-dispatch state machine (`AWAITING_SCOPE`,
`DISPATCHED`, `SHADOWED`, `REJECTED`). Does not own: which caller may call which route (R1 Termination checks the token and stamps the caller's role; the GUI BFF
pins `rmioId` and restricts registration and report publication by the console user's role) or the autonomy mode and region scope of an instance (rApp Management).

Before editing: (1) the docstring of a route function or a request model is published as OpenAPI text, so changing one makes
`tests_integration/test_openapi_specs.py` fail until `scripts/generate_openapi_specs.py` is rerun; the maintainer notes for routes are `#` comments under
the docstring. (2) `_create_intent_row` does not commit; its callers do, so the intent, its first report and the notifications are one transaction (PR-MSG-1.9).
A route that creates or changes something enqueues its notifications before `db.commit()` and never after. (3) `import httpx` is kept although nothing here
calls it: the tests patch `app.main.httpx.post` (see `ruff.toml`).
"""

import datetime
import json
import uuid
from typing import Any

# Unused here on purpose: the unit tests patch `app.main.httpx.post` and the integration tests patch `loaded_apps[<module>].httpx` (see `ruff.toml`, F401).
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from smo_shared.logconfig import install_logging
from smo_shared.metrics import count_by, install_metrics, register_query_gauge
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.identity import is_framework_internal_identity
from smo_shared.invoker import acting_user, invoker_id
from smo_shared.roles import ROLE_INTERNAL, ROLE_RAPP, role_of
from smo_shared.pagination import PageLimit, PageOffset, paginate
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id
from smo_shared.r1_client import R1Client
from smo_shared.outbox import enqueue

from . import ts28312
from .models import AutonomyDispatch, Intent, IntentHandlingFunction, IntentReport, IntentUtilityFormula

app = FastAPI(title="Intent Service")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
register_query_gauge("smo_intents", "Intents (the policy objects), by admin state.", ["admin_state"],
                     lambda s: count_by(s, Intent.intent_admin_state, ("ACTIVATED", "DEACTIVATED")))  # PR-OBS-4
apply_r1_gateway_security(app)
apply_correlation_id(app)

# HISTORY.md OI-6.3: cross-module read of a RAppInstance's own
# autonomyMode/regionScope (rApp Mgmt) — this module's first cross-module
# call; every other route here is purely local.
# The one cross-module client of this service; the tests replace `R1Client.get` to stand in for rApp Management.
_r1 = R1Client()


install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)


class CreateIntentRequest(BaseModel):
    """Wave 6 — TS 28.312 Intent, validated strictly (agreed: every caller
    migrated): the spec's own attribute names, required fields and enums,
    and every expectation checked against its expectation family
    (`app/ts28312.py`). `rmihId`/`rmioId`/`intentHandlingScope` are this
    build's own: the consumer-side choice of handling function (TS 28.312
    containment: IntentHandlingFunction *contains* Intent), the creating
    consumer's identity, and the handling-scope check against that
    function's declared coverage (HISTORY.md §7 items 1-3).
    """
    model_config = ConfigDict(extra="forbid")

    userLabel: str
    intentExpectations: list[ts28312.IntentExpectation] = Field(min_length=1)
    intentMgmtPurpose: ts28312.IntentMgmtPurpose = "FULFILMENT_WITHOUT_NEGOTIATION"
    contextSelectivity: ts28312.Selectivity | None = None
    consumerSatisfactionIndexThreshold: int | None = None
    expectationSelectivity: ts28312.Selectivity | None = None
    intentContexts: list[ts28312.Context] | None = None
    intentAdminState: ts28312.IntentAdminState = "ACTIVATED"
    intentPriority: int = Field(default=1, ge=1, le=100)
    intentPreemptionCapability: bool = False
    intentReportControl: list[ts28312.IntentReportControl] = Field(min_length=1)
    implicitIntentIndex: bool = False
    guaranteePeriods: list[ts28312.Context] | None = None
    intentHandlingInfo: ts28312.IntentHandlingInfo | None = None
    intentInterpretationAssistanceInfo: ts28312.IntentInterpretationAssistanceInfo | None = None
    intentUtilityFormulaRef: uuid.UUID | None = None
    rmihId: str
    rmioId: str = ""
    intentHandlingScope: ts28312.IntentHandlingScope | None = None


# Body of PATCH /intents/{id}/admin-state. From the console or a module `requesterId` must equal the intent's `rmioId`; an rApp is checked as the intent's owner
# instead and its `requesterId` is not read. An unknown `newState` is a 422.
class AdminStateRequest(BaseModel):
    newState: ts28312.IntentAdminState
    requesterId: str


class IntentReportRequest(BaseModel):
    """Wave 6 — TS 28.312 IntentReport, published by the handling function
    (or SO/SA-SMOS on its behalf). Every report kind of the spec."""
    model_config = ConfigDict(extra="forbid")

    intentReference: uuid.UUID
    intentFulfilmentReport: ts28312.IntentFulfilmentReport | None = None
    intentConflictReports: list[ts28312.IntentConflictReport] | None = None
    intentFeasibilityCheckReport: ts28312.IntentFeasibilityCheckReport | None = None
    intentExplorationReport: ts28312.IntentExplorationReport | None = None
    intentUtilityReports: list[ts28312.IntentUtilityReport] | None = None
    intentFulfilmentNegotiationReport: ts28312.IntentFulfilmentNegotiationReport | None = None
    intentDecompositionReport: ts28312.IntentDecompositionReport | None = None


class RegisterRmihRequest(BaseModel):
    """Wave 6 — TS 28.312 IntentHandlingFunction with its spec attributes
    (intentHandlingCapabilityList, supportedNegotiationFunctionalities,
    supportedUtilityList). `smeServiceId`/`notificationDestination` are this
    build's own: the SME-published API it serves and where new Intents are
    pushed (same callback convention as DME)."""
    model_config = ConfigDict(extra="forbid")

    rmihId: str
    smeServiceId: str
    intentHandlingCapabilityList: list[ts28312.IntentHandlingCapability] = Field(min_length=1)
    supportedNegotiationFunctionalities: list[ts28312.NegotiationFunctionality] | None = Field(default=None, min_length=1)
    supportedUtilityList: list[ts28312.UtilityDefinition] | None = None
    notificationDestination: str
    intentHandlingScope: list[ts28312.IntentHandlingScope] | None = None


# Body of POST /intent-utility-formulas. `utilityScale` defaults to 1 and `utilityOffset` to 0; unknown fields are refused.
class IntentUtilityFormulaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    utilityFunctionId: str
    utilityParameterList: list[ts28312.UtilityParameter]
    utilityScale: float = 1
    utilityOffset: float = 0


# Body of POST /intents/{id}/negotiation-feedback. `referredIntentOutcomeId` must be the id of an outcome in the intent's newest negotiation report.
class NegotiationFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    referredIntentOutcomeId: int
    consumerSatisfactionIndex: int | None = None


# Body of POST /autonomy-dispatches. `instanceId` is the rApp instance whose autonomy mode decides the outcome; `expectations` are validated as TS 28.312
# expectations. Unlike the intent request, unknown fields are not refused. `notificationDestination` is where the operator is told; without it nothing is sent.
class CreateAutonomyDispatchRequest(BaseModel):
    instanceId: uuid.UUID
    modelId: uuid.UUID | None = None
    # The Intent an AUTONOMOUS or resolved ASSIST dispatch creates is a strict TS 28.312 Intent, so its expectations are validated as such up front.
    expectations: list[ts28312.IntentExpectation] = Field(min_length=1)
    priority: int = Field(default=1, ge=1, le=100)
    rmihId: str
    intentMgmtPurpose: ts28312.IntentMgmtPurpose = "FULFILMENT_WITHOUT_NEGOTIATION"
    intentHandlingScope: ts28312.IntentHandlingScope | None = None
    # All three modes always notify the operator — optional only in the
    # same sense every other subscription-shaped resource's own
    # notification_destination already is (a purely poll-based consumer
    # may still omit it).
    notificationDestination: str | None = None
    userLabel: str | None = None


# Body of POST /autonomy-dispatches/{id}/resolve. `regionScope` is a free dict; the keys used are `objectInstance` and `cells` (see `_scoped`).
class ResolveAutonomyDispatchRequest(BaseModel):
    regionScope: dict


# ---------------------------------------------------------------- DOC-CF-1.8 flow 09: who may do what

def _is_rapp(request: Request) -> bool:
    """Whether R1 Termination stamped the caller as an rApp (`X-R1-Role: rapp`). A call without the header (a test, an in-process call) is not one."""
    return role_of(request) == ROLE_RAPP


def _refuse_rapp(request: Request, what: str) -> None:
    """Raises 403 `ROLE_NOT_PERMITTED` when the caller is an rApp. Handlers and the reports they publish are the SMO's own (SO SMOS, SA SMOS) or the console's."""
    if _is_rapp(request):
        raise framework_error(FrameworkError.ROLE_NOT_PERMITTED, detail=f"an rApp may not {what}")


def _owner(intent: Intent) -> str:
    """Returns the invoker that owns `intent`: the one recorded at creation, else (an intent from before revision 0057) its `rmio_id`."""
    return intent.owner_invoker_id or intent.rmio_id


def _check_intent_owner(request: Request, intent: Intent) -> None:
    """Raises 403 `INTENT_ACCESS_DENIED` when an rApp acts on an intent it does not own; the console and modules pass (they act for others)."""
    if _is_rapp(request) and invoker_id(request) != _owner(intent):
        raise framework_error(FrameworkError.INTENT_ACCESS_DENIED, detail="an rApp may act only on the intents it created")


def _operator(request: Request, fallback: str | None) -> str | None:
    """Returns who decides an ASSIST dispatch: the console user (`X-R1-Acting-User`, `smo-gui:<user>`) for an internal caller, `fallback` for a call with
    no role (a test or an in-process call). Raises 403 `ROLE_NOT_PERMITTED` for an rApp, and for an internal caller that names no signed-in person: the
    decision is an operator's, never an rApp's or a module's on its own account.
    """
    if _is_rapp(request):
        raise framework_error(FrameworkError.ROLE_NOT_PERMITTED, detail="an ASSIST dispatch is decided by an operator, not an rApp")
    if role_of(request) == ROLE_INTERNAL:
        person = acting_user(request)
        if person is None:
            raise framework_error(FrameworkError.ROLE_NOT_PERMITTED, detail="an ASSIST dispatch is decided by a signed-in operator (X-R1-Acting-User)")
        return person
    return fallback


@app.post("/intents", status_code=201)
def create_intent(body: CreateIntentRequest, request: Request, db: Session = Depends(get_session)):
    """CreateIntent — the caller addresses a specific, already-registered
    RMIH by `rmihId` (consumer-side selection, TS 28.312 containment).
    404 if no such function; 422 RMIH_CAPABILITY_MISMATCH if the function
    cannot handle the intent's object types, scope or management purpose,
    or — for a fulfilment purpose — any of its targets (see
    `_create_intent_row`). Dispatch to the RMIH is best-effort.
    """
    # Route notes (the docstring above is published): 201 `{intentId, intentReportReference}`. All checks are in `_create_intent_row`: 404
    # `INTENT_HANDLING_FUNCTION_NOT_FOUND`, 422 `RMIH_CAPABILITY_MISMATCH`, 404 `NRM_OBJECT_NOT_FOUND` (unknown utility formula). An rApp's `rmioId` is its own
    # invoker id whatever it sent (call flow 09a); the console's is pinned by the GUI BFF, a module's is taken as given. The caller's invoker is recorded as the
    # intent's owner. A callback that is unreachable never fails the request: the RMIH push is an outbox row.
    caller = invoker_id(request)
    if _is_rapp(request):
        body = body.model_copy(update={"rmioId": caller or ""})
    intent = _create_intent_row(db, body, owner=caller)
    db.commit()  # the intent, its first report and their notifications commit together (PR-MSG-1.9)
    return {"intentId": str(intent.intent_id), "intentReportReference": _s(intent.intent_report_reference)}


def _s(value) -> str | None:
    """Returns `str(value)`, or None for None (a UUID column to its JSON form)."""
    return str(value) if value is not None else None


def _supported_targets(fn: IntentHandlingFunction, object_type: str | None) -> list[dict] | None:
    """Returns the `supportedExpectationTargetInfoList` of the first capability of `fn` for `object_type`, or None when `fn` declares no capability for it.

    An empty list (a capability that names no targets) is different from None: it means the object type is covered but no target is supported.
    """
    for cap in fn.intent_handling_capability_list or []:
        if cap.get("supportedExpectationObjectType") == object_type:
            return cap.get("supportedExpectationTargetInfoList") or []
    return None


def _feasibility(fn: IntentHandlingFunction, expectations: list[dict]) -> dict:
    """Returns a TS 28.312 `IntentFeasibilityCheckReport` computed against the capabilities `fn` declared.

    A target is feasible when the capability for its expectation's object type lists its name and, if that entry names a condition, the target uses the same
    one; value ranges are not compared. An expectation whose object type has no capability is skipped (the capability check has already refused it on
    the create path). The result is `INFEASIBLE` with reason `INVALID_INTENT_EXPRESSION` and `inFeasibleExpectationInfos` when any target fails, else `FEASIBLE`.
    """
    infeasible = []
    for exp in expectations:
        supported = _supported_targets(fn, exp["expectationObject"].get("objectType"))
        if supported is None:
            continue  # object type not covered at all — rejected by the capability check
        by_name = {t["supportedTargetName"]: t for t in supported}
        bad = []
        for target in exp["expectationTargets"]:
            known = by_name.get(target["targetName"])
            if known is None or (known.get("supportedTargetCondition")
                                 and known["supportedTargetCondition"] != target["targetCondition"]):
                bad.append({"targetName": target["targetName"]})
        if bad:
            infeasible.append({"expectationId": exp["expectationId"], "inFeasibleTargets": bad})
    report: dict[str, Any] = {"feasibilityCheckResult": "INFEASIBLE" if infeasible else "FEASIBLE",
              "infeasibilityReasons": ["INVALID_INTENT_EXPRESSION"] if infeasible else []}
    if infeasible:
        report["inFeasibleExpectationInfos"] = infeasible
    return report


def _conflicts(db: Session, expectations: list[dict]) -> list[dict]:
    """Returns the `TARGET_CONFLICT` reports for `expectations`: another ACTIVATED intent already sets the same target on the same object instance with a
    different condition or value range.

    Only expectations that name an `objectInstance` can conflict. The comparison reads every ACTIVATED intent in Python (any RMIH), so its cost grows with
    the number of active intents. The new intent is not stored yet, so it cannot conflict with itself. Each report recommends `MODIFY`. A conflict is reported,
    never a reason to refuse the intent.
    """
    wanted = {}
    for exp in expectations:
        instance = exp["expectationObject"].get("objectInstance")
        if not instance:
            continue
        for target in exp["expectationTargets"]:
            wanted[(instance, target["targetName"])] = (exp["expectationId"], target)
    if not wanted:
        return []
    conflicts = []
    for other in db.scalars(select(Intent).where(Intent.intent_admin_state == "ACTIVATED")).all():
        for exp in other.intent_expectations or []:
            instance = (exp.get("expectationObject") or {}).get("objectInstance")
            for target in exp.get("expectationTargets") or []:
                mine = wanted.get((instance, target.get("targetName")))
                if mine and (mine[1]["targetCondition"], mine[1]["targetValueRange"]) != (
                        target.get("targetCondition"), target.get("targetValueRange")):
                    conflicts.append({"conflictId": str(uuid.uuid4()), "conflictType": "TARGET_CONFLICT",
                                      "conflictingIntent": str(other.intent_id),
                                      "conflictingExpectation": exp.get("expectationId"),
                                      "conflictingTarget": target.get("targetName"), "recommendedSolutions": "MODIFY"})
    return conflicts


def _received_fulfilment(expectations: list[dict], state: str = "RECEIVED") -> dict:
    """Returns an `IntentFulfilmentReport` saying nothing is fulfilled yet: NOT_FULFILLED with `state` (RECEIVED, or SUSPENDED for a deactivated intent), for the
    intent, each expectation and each target.

    The key `expectaitonId` is the spec's own spelling (see `ts28312.ExpectationFulfilmentResult`).
    """
    info = {"fulfilmentStatus": "NOT_FULFILLED", "notFullfilledState": state}
    return {"intentFulfilmentInfo": info, "expectationFulfilmentResult": [
        {"expectaitonId": e["expectationId"], "expectationFulfilmentInfo": info,
         "targetFulfilmentResults": [{"targetName": t["targetName"], "targetFulfilmentInfo": info}
                                     for t in e["expectationTargets"]]}
        for e in expectations]}


def _create_intent_row(db: Session, body: CreateIntentRequest, owner: str | None = None) -> Intent:
    """Validates a `CreateIntentRequest` and adds the intent, its initial report and the notifications to `db`'s transaction; returns the intent. Does not commit.
    `owner` is the invoker recorded as the intent's owner (`owner_invoker_id`; None leaves `rmio_id` standing in).

    Shared by `POST /intents` and the autonomy paths (AUTONOMOUS, and ASSIST at resolve), so an intent made from a dispatch is an intent in every respect.
    Raises, in this order: 404 `INTENT_HANDLING_FUNCTION_NOT_FOUND` (no such `rmihId`); 422 `RMIH_CAPABILITY_MISMATCH` when the RMIH lacks a capability for
    any expectation object type, does not cover the handling scope, lacks the negotiation functionality the `intentMgmtPurpose` needs (only when it declares
    any), or, for a purpose other than FEASIBILITYCHECK*, an infeasible target; 404 `NRM_OBJECT_NOT_FOUND` for an unknown `intentUtilityFormulaRef`.

    A FEASIBILITYCHECK* intent with infeasible targets is accepted and its initial report carries the INFEASIBLE feasibility report. Target conflicts are
    added to the report, not refused. Side effects: an `Intent` row, an `IntentReport` row (fulfilment NOT_FULFILLED / RECEIVED, plus the conflict and
    feasibility reports when present) that becomes `intent_report_reference`, one outbox row pushing the new intent to the RMIH, and one outbox row per
    matching report recipient (`_deliver_report`). The caller commits; a rollback discards all of them.
    """
    fn = db.get(IntentHandlingFunction, body.rmihId)
    if fn is None:
        raise framework_error(FrameworkError.INTENT_HANDLING_FUNCTION_NOT_FOUND, detail="no such intent handling function")
    expectations = ts28312.dump(body.intentExpectations)
    _validate_rmih_can_handle(fn, _requested_expectation_object_types(expectations), body.intentHandlingScope)
    needed = ts28312.PURPOSE_NEEDS.get(body.intentMgmtPurpose)
    if needed and fn.supported_negotiation_functionalities and needed not in fn.supported_negotiation_functionalities:
        raise framework_error(FrameworkError.RMIH_CAPABILITY_MISMATCH,
                               detail=f"{fn.rmih_id} does not support {needed} (intentMgmtPurpose {body.intentMgmtPurpose})")
    if body.intentUtilityFormulaRef is not None and db.get(IntentUtilityFormula, body.intentUtilityFormulaRef) is None:
        raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail="no such IntentUtilityFormula")

    feasibility = _feasibility(fn, expectations)
    is_check = body.intentMgmtPurpose.startswith("FEASIBILITYCHECK")
    if feasibility["feasibilityCheckResult"] == "INFEASIBLE" and not is_check:
        raise framework_error(FrameworkError.RMIH_CAPABILITY_MISMATCH,
                               detail=f"{fn.rmih_id} cannot fulfil: {feasibility['inFeasibleExpectationInfos']}")
    conflicts = _conflicts(db, expectations)

    intent = Intent(user_label=body.userLabel, intent_expectations=expectations, intent_priority=body.intentPriority,
                    rmio_id=body.rmioId, owner_invoker_id=owner, intent_mgmt_purpose=body.intentMgmtPurpose, rmih_id=body.rmihId,
                    intent_admin_state=body.intentAdminState, intent_preemption_capability=body.intentPreemptionCapability,
                    context_selectivity=body.contextSelectivity, expectation_selectivity=body.expectationSelectivity,
                    consumer_satisfaction_index_threshold=body.consumerSatisfactionIndexThreshold,
                    intent_contexts=ts28312.dump(body.intentContexts),
                    intent_report_control=ts28312.dump(body.intentReportControl),
                    implicit_intent_index=body.implicitIntentIndex, guarantee_periods=ts28312.dump(body.guaranteePeriods),
                    intent_handling_info=ts28312.dump(body.intentHandlingInfo),
                    intent_interpretation_assistance_info=ts28312.dump(body.intentInterpretationAssistanceInfo),
                    intent_utility_formula_id=body.intentUtilityFormulaRef)
    # Two flushes: the intent first, so the report row can name its id; then the report, so `intent_report_reference` can name the report's id. Nothing is committed yet.
    db.add(intent)
    db.flush()
    report = IntentReport(intent_id=intent.intent_id, intent_fulfilment_report=_received_fulfilment(expectations),
                          intent_conflict_reports=conflicts or None,
                          intent_feasibility_check_report=feasibility if is_check else None)
    db.add(report)
    db.flush()
    intent.intent_report_reference = report.id
    _apply_report(intent, report)
    intent.in_conflict = bool(conflicts)       # a new intent with no conflict is known not to be in one

    # Outbox rows in the caller's transaction (PR-MSG-1.9): this function no longer commits, its caller does, and the
    # notifications are sent once that commit has happened.
    enqueue(db, fn.notification_destination, {
        "intentId": str(intent.intent_id), "expectationObjectTypes": sorted(_requested_expectation_object_types(expectations)),
        "intentPriority": intent.intent_priority, "rmioId": intent.rmio_id, "intentMgmtPurpose": intent.intent_mgmt_purpose,
    })
    _deliver_report(db, intent, report)
    return intent


def _requested_expectation_object_types(expectations: list[dict]) -> set[str]:
    """Returns the set of `expectationObject.objectType` values named by `expectations` (an expectation without one adds nothing)."""
    types: set[str] = set()
    for expectation in expectations:
        object_type = (expectation.get("expectationObject") or {}).get("objectType")
        if object_type:
            types.add(object_type)
    return types


def _validate_rmih_can_handle(fn: IntentHandlingFunction, expectation_object_types: set[str], scope: str | None) -> None:
    """Raises 422 `RMIH_CAPABILITY_MISMATCH` unless `fn` covers the request: the handling `scope` (only checked when both the request and `fn` name one), and a
    capability for every object type in `expectation_object_types` (all of them, not any one).

    The negotiation-functionality and feasibility checks are not here; they are in `_create_intent_row`, so an autonomy dispatch that is only SHADOWED or
    awaiting scope is not checked for them until an intent is actually made.
    """
    if scope is not None and fn.intent_handling_scope and scope not in fn.intent_handling_scope:
        raise framework_error(FrameworkError.RMIH_CAPABILITY_MISMATCH,
                               detail=f"{fn.rmih_id} does not cover handling scope {scope!r}")
    supported = {cap.get("supportedExpectationObjectType") for cap in fn.intent_handling_capability_list or []}
    missing = expectation_object_types - supported
    if missing:
        raise framework_error(FrameworkError.RMIH_CAPABILITY_MISMATCH,
                               detail=f"{fn.rmih_id} does not support {sorted(missing)}")


# Report kind (the spec's attribute name) -> the `IntentReport` column that holds it. `publish_intent_report` and `_report_view` both walk this table.
_REPORT_COLUMNS = {
    "intentFulfilmentReport": "intent_fulfilment_report", "intentConflictReports": "intent_conflict_reports",
    "intentFeasibilityCheckReport": "intent_feasibility_check_report", "intentExplorationReport": "intent_exploration_report",
    "intentUtilityReports": "intent_utility_reports",
    "intentFulfilmentNegotiationReport": "intent_fulfilment_negotiation_report",
    "intentDecompositionReport": "intent_decomposition_report",
}


def _report_view(r: IntentReport) -> dict:
    """Returns the wire form of a report: `id` / `reportId`, `intentId`, and `attributes` with each report kind that is present plus `lastUpdatedTime` and
    `intentReference`. A kind that was not published is left out, not null.
    """
    attrs = {name: getattr(r, column) for name, column in _REPORT_COLUMNS.items()}
    attrs = {k: v for k, v in attrs.items() if v is not None}
    return {"id": str(r.id), "reportId": str(r.id), "intentId": str(r.intent_id),
            "attributes": {**attrs, "lastUpdatedTime": r.last_updated_time.isoformat(), "intentReference": str(r.intent_id)}}


def fulfilment_percent(fulfilment_report: dict | None) -> float | None:
    """GUI-9.8: the share (0-100, one decimal) of an `IntentFulfilmentReport` that is FULFILLED, counted over its targets
    (`expectationFulfilmentResult[].targetFulfilmentResults[]`); a report that lists no targets is counted over its expectations, and one that lists
    neither by the intent's own `intentFulfilmentInfo` (0 or 100). None for no report. Revision 0037 repeats this rule to fill the existing rows."""
    if not isinstance(fulfilment_report, dict):
        return None
    results = [e for e in fulfilment_report.get("expectationFulfilmentResult") or [] if isinstance(e, dict)]
    infos = [t.get("targetFulfilmentInfo") for e in results for t in e.get("targetFulfilmentResults") or [] if isinstance(t, dict)]
    if not infos:
        infos = [e.get("expectationFulfilmentInfo") for e in results]
    if not infos:
        infos = [fulfilment_report.get("intentFulfilmentInfo")]
    graded = [i for i in infos if isinstance(i, dict)]
    if not graded:
        return None
    return round(100 * sum(i.get("fulfilmentStatus") == "FULFILLED" for i in graded) / len(graded), 1)


def _apply_report(intent: Intent, report: IntentReport) -> None:
    """Keeps the intent's summary columns in step with a report just stored for it (GUI-9.8): a fulfilment report sets `fulfilment_percent` and
    `fulfilled` (the intent's own `intentFulfilmentInfo.fulfilmentStatus`); a report that carries conflict reports sets `in_conflict` (an empty list
    clears it). A report of other kinds changes neither. Does not commit."""
    if report.intent_fulfilment_report is not None:
        intent.fulfilment_percent = fulfilment_percent(report.intent_fulfilment_report)
        info = report.intent_fulfilment_report.get("intentFulfilmentInfo") or {}
        intent.fulfilled = info.get("fulfilmentStatus") == "FULFILLED"
    if report.intent_conflict_reports is not None:
        intent.in_conflict = bool(report.intent_conflict_reports)


def _deliver_report(db: Session, intent: Intent, report: IntentReport) -> None:
    """Enqueues `report` for every report control of `intent` that has a `reportRecipientAddress` and wants it; the caller commits.

    A control wants the report when one of its `expectedReportTypes` is among the kinds the report holds (all kinds when the list is empty). The payload is
    `{"notificationType": "notifyIntentReport", ...report view}`. One outbox row per recipient, in the caller's transaction (PR-MSG-1.9); a destination the
    SSRF guard refuses is dropped by `enqueue` with a warning.
    """
    view = _report_view(report)
    present = {ts28312.REPORT_TYPE_OF[k] for k in view["attributes"] if k in ts28312.REPORT_TYPE_OF}
    for control in intent.intent_report_control or []:
        wanted = set(control.get("expectedReportTypes") or []) or present
        if control.get("reportRecipientAddress") and present & wanted:
            enqueue(db, control["reportRecipientAddress"], {"notificationType": "notifyIntentReport", **view})


@app.get("/intents/{intent_id}")
def query_intent(intent_id: uuid.UUID, db: Session = Depends(get_session)):
    """QueryIntent — a real 404 matters since `Intent.rmih_id`'s ON DELETE
    CASCADE means an Intent can vanish when its RMIH is deregistered."""
    intent = db.get(Intent, intent_id)
    if intent is None:
        raise framework_error(FrameworkError.INTENT_NOT_FOUND, detail="no such intent")
    return _intent_view(intent)


@app.get("/intents")
def query_intents(admin_state: str | None = None, fulfilled: bool | None = None, in_conflict: bool | None = None, limit: int = PageLimit,
                  offset: int = PageOffset, db: Session = Depends(get_session)):
    """QueryIntents, paginated. `admin_state` is an exact match. GUI-9.8: `fulfilled` keeps the intents whose newest fulfilment report says
    FULFILLED (`true`) or NOT_FULFILLED (`false`; an intent with no fulfilment report matches neither); `in_conflict` keeps the intents whose
    newest conflict report names a conflict (`true`) or the others (`false`)."""
    # Route notes: `admin_state` is not validated (an unknown value gives an empty page). Paginated, in primary-key order (a random UUID, so not creation order).
    # Both GUI-9.8 filters are SQL on the summary columns `_apply_report` keeps; a row written by the previous release during a rolling upgrade has
    # `in_conflict` NULL, which counts as not in conflict.
    stmt = select(Intent)
    if admin_state:
        stmt = stmt.where(Intent.intent_admin_state == admin_state)
    if fulfilled is not None:
        stmt = stmt.where(Intent.fulfilled.is_(fulfilled))
    if in_conflict is not None:
        stmt = stmt.where(Intent.in_conflict.is_(True) if in_conflict else or_(Intent.in_conflict.is_(False), Intent.in_conflict.is_(None)))
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_intent_view(i) for i in page["items"]]}


@app.patch("/intents/{intent_id}/admin-state")
def update_intent_admin_state(intent_id: uuid.UUID, body: AdminStateRequest, request: Request, db: Session = Depends(get_session)):
    """UpdateIntentAdminState — RMIO-only. Wave 6: a DEACTIVATED intent's
    report moves to NOT_FULFILLED / SUSPENDED (TS 28.312 NotFulfilledState)
    and is delivered per intentReportControl; reactivating it reports
    RECEIVED again until the handler reports otherwise."""
    # Route notes (the docstring above is published): 404 `INTENT_NOT_FOUND`; 403 `INTENT_ACCESS_DENIED` when an rApp does not own the intent (its `requesterId`
    # is not read), or when the console or a module names a `requesterId` that is not the intent's `rmioId` (call flow 09b). A change to the state it already has
    # does nothing and writes no report. Otherwise: state changed, a new fulfilment report (SUSPENDED when deactivated, RECEIVED when activated) becomes the
    # current report and is delivered, one commit. Returns the intent view.
    intent = db.get(Intent, intent_id)
    if intent is None:
        raise framework_error(FrameworkError.INTENT_NOT_FOUND, detail="no such intent")
    _check_intent_owner(request, intent)
    if not _is_rapp(request) and intent.rmio_id != body.requesterId:
        raise framework_error(FrameworkError.INTENT_ACCESS_DENIED, detail="only the intent's creator (RMIO) may change its admin state")
    if intent.intent_admin_state != body.newState:
        intent.intent_admin_state = body.newState
        state = "SUSPENDED" if body.newState == "DEACTIVATED" else "RECEIVED"
        report = IntentReport(intent_id=intent.intent_id,
                              intent_fulfilment_report=_received_fulfilment(intent.intent_expectations, state))
        db.add(report)
        db.flush()
        intent.intent_report_reference = report.id
        _apply_report(intent, report)
        _deliver_report(db, intent, report)
        db.commit()
    return _intent_view(intent)


@app.delete("/intents/{intent_id}", status_code=204)
def delete_intent(intent_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """HISTORY.md §7 item 5: an RMIO retracts an Intent it created.
    Idempotent."""
    # Route notes: idempotent, 204 for an unknown id; 403 `INTENT_ACCESS_DENIED` when an rApp does not own the intent (call flow 09b). The intent's reports are
    # deleted with it, in the same transaction (explicitly, so SQLite matches PostgreSQL's ON DELETE CASCADE). Nothing is sent to the RMIH.
    intent = db.get(Intent, intent_id)
    if intent is not None:
        _check_intent_owner(request, intent)
        intent.intent_report_reference = None
        db.flush()
        for report in db.scalars(select(IntentReport).where(IntentReport.intent_id == intent_id)):
            db.delete(report)
        db.delete(intent)
        db.commit()


@app.post("/intents/{intent_id}/negotiation-feedback")
def submit_negotiation_feedback(intent_id: uuid.UUID, body: NegotiationFeedbackRequest, request: Request, db: Session = Depends(get_session)):
    """TS 28.312 IntentFulfilmentNegotiationFeedback: the consumer picks one
    of the possibleIntentOutcomeList entries of the intent's latest
    negotiation report."""
    # Route notes (the docstring above is published): 404 `INTENT_NOT_FOUND`; 404 `NRM_OBJECT_NOT_FOUND` when the intent has no negotiation report; 422
    # `SCHEMA_VALIDATION_FAILED` when `referredIntentOutcomeId` is not one of that report's outcomes. The feedback is written into the newest negotiation report
    # in place (a new JSON document is assigned so the change is saved): no new report row, no change to `intent_report_reference`, no notification to the RMIH.
    # Calling it again overwrites the feedback. 403 `INTENT_ACCESS_DENIED` when an rApp does not own the intent (call flow 09b). Returns the report view.
    intent = db.get(Intent, intent_id)
    if intent is None:
        raise framework_error(FrameworkError.INTENT_NOT_FOUND, detail="no such intent")
    _check_intent_owner(request, intent)
    report = db.scalars(select(IntentReport).where(IntentReport.intent_id == intent_id,
                                                   IntentReport.intent_fulfilment_negotiation_report.is_not(None))
                        .order_by(IntentReport.last_updated_time.desc())).first()
    if report is None:
        raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail="intent has no negotiation report to answer")
    negotiation = dict(report.intent_fulfilment_negotiation_report or {})
    outcome_ids = {o["possibleIntentOutcomeId"] for o in negotiation.get("possibleIntentOutcomeList") or []}
    if body.referredIntentOutcomeId not in outcome_ids:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                               detail=f"no possibleIntentOutcomeId {body.referredIntentOutcomeId}")
    negotiation["intentFulfilmentNegotiationConsumerFeedback"] = body.model_dump(exclude_none=True)
    report.intent_fulfilment_negotiation_report = negotiation
    report.last_updated_time = datetime.datetime.now(datetime.UTC)
    db.commit()
    return _report_view(report)


@app.post("/intent-reports", status_code=201)
def publish_intent_report(body: IntentReportRequest, request: Request, db: Session = Depends(get_session)):
    """PublishIntentReport — any of the spec's report kinds, at least one.
    The new report becomes the intent's intentReportReference and is
    delivered per its intentReportControl."""
    # Route notes (the docstring above is published): 403 `ROLE_NOT_PERMITTED` for an rApp (a report is the handler's, and handlers are SMO modules, call flow
    # 09c); 404 `INTENT_NOT_FOUND`; 422 `SCHEMA_VALIDATION_FAILED` when no report kind is present. A module or the console (admin, by the GUI BFF) may publish for
    # any intent. The new report row becomes `intent_report_reference`, is delivered per report control, one commit. Answers 201 with `reportId` and the report view.
    _refuse_rapp(request, "publish an intent report")
    intent = db.get(Intent, body.intentReference)
    if intent is None:
        raise framework_error(FrameworkError.INTENT_NOT_FOUND, detail="no such intent")
    values = {column: ts28312.dump(getattr(body, name)) for name, column in _REPORT_COLUMNS.items()}
    if all(v is None for v in values.values()):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="an IntentReport needs at least one report")
    report = IntentReport(intent_id=intent.intent_id, **values)
    db.add(report)
    db.flush()
    intent.intent_report_reference = report.id
    _apply_report(intent, report)
    _deliver_report(db, intent, report)
    db.commit()
    return {"reportId": str(report.id), **_report_view(report)}


@app.post("/intent-handling-functions", status_code=201)
def register_intent_handling_function(body: RegisterRmihRequest, request: Request, db: Session = Depends(get_session)):
    """RegisterIntentHandlingFunction — only the SMO registers handlers
    (D-SEC-POLICY-1): an rApp caller is refused, and so is an rmihId that
    is an rApp identity. Registering an rmihId again updates it in place,
    keeping the intents addressed to it.
    """
    # Route notes (the docstring above is published): 403 `ROLE_NOT_PERMITTED` for an rApp caller (call flow 09c); 409 `SERVICE_NAME_CONFLICT` for an `rmihId`
    # that parses as a UUID (an rApp identity). An `rmihId` already registered is replaced in place (capabilities, destination, scope), so its intents stay
    # (before DOC-CF-1.8 it was a 409, and SA SMOS deleted the handler, and with it every intent, to register again). The destination is not checked here; the
    # SSRF guard runs when a push is enqueued. 201 with the RMIH view.
    _refuse_rapp(request, "register an intent handling function")
    if not is_framework_internal_identity(body.rmihId):
        raise framework_error(FrameworkError.SERVICE_NAME_CONFLICT, detail="external callers may never hold an rmihId (D-SEC-POLICY-1)")
    fn = db.get(IntentHandlingFunction, body.rmihId) or IntentHandlingFunction(rmih_id=body.rmihId)
    fn.sme_service_id = body.smeServiceId
    fn.intent_handling_capability_list = ts28312.dump(body.intentHandlingCapabilityList)
    fn.supported_negotiation_functionalities = body.supportedNegotiationFunctionalities
    fn.supported_utility_list = ts28312.dump(body.supportedUtilityList)
    fn.notification_destination = body.notificationDestination
    fn.intent_handling_scope = body.intentHandlingScope
    db.add(fn)
    db.commit()
    return _rmih_view(fn)


@app.delete("/intent-handling-functions/{rmih_id}", status_code=204)
def deregister_intent_handling_function(rmih_id: str, request: Request, db: Session = Depends(get_session)):
    """NEW — symmetric with Register, closing v1.3's gap (Policy Mgmt LLD section 1).
    Refused while intents are still addressed to the function."""
    # Route notes: 403 `ROLE_NOT_PERMITTED` for an rApp caller; idempotent, 204 for an unknown id; 409 `INTENT_HANDLING_FUNCTION_IN_USE` while any intent is
    # addressed to it (call flow 09c: its owners delete them first, so no intent disappears under its owner). Its autonomy dispatches go with it (ON DELETE
    # CASCADE, PostgreSQL only).
    _refuse_rapp(request, "deregister an intent handling function")
    fn = db.get(IntentHandlingFunction, rmih_id)
    if fn is not None:
        in_use = db.scalar(select(func.count()).select_from(Intent).where(Intent.rmih_id == rmih_id))
        if in_use:
            raise framework_error(FrameworkError.INTENT_HANDLING_FUNCTION_IN_USE,
                                  detail=f"{in_use} intent(s) are addressed to {rmih_id}; delete them first")
        db.delete(fn)
        db.commit()


def _intent_view(i: Intent) -> dict:
    """Returns the wire form of an intent: summary keys the existing readers use, plus the full TS 28.312 Intent under `attributes`.

    Attributes that are None are left out of `attributes`; `intentReportReference` and `intentUtilityFormulaRef` are strings. GUI-9.8 summary keys:
    `fulfilmentPercent` and `fulfilled` (null until a fulfilment report exists) and `inConflict` (always a bool), from the columns `_apply_report` keeps.
    """
    return {"id": str(i.intent_id), "intentId": str(i.intent_id), "intentAdminState": i.intent_admin_state,
            "intentPriority": i.intent_priority, "rmioId": i.rmio_id, "ownerInvokerId": _owner(i), "intentMgmtPurpose": i.intent_mgmt_purpose,
            "rmihId": i.rmih_id, "userLabel": i.user_label,
            "fulfilmentPercent": i.fulfilment_percent, "fulfilled": i.fulfilled, "inConflict": bool(i.in_conflict),
            "attributes": {k: v for k, v in {
                "userLabel": i.user_label, "intentExpectations": i.intent_expectations,
                "intentMgmtPurpose": i.intent_mgmt_purpose, "contextSelectivity": i.context_selectivity,
                "consumerSatisfactionIndexThreshold": i.consumer_satisfaction_index_threshold,
                "expectationSelectivity": i.expectation_selectivity, "intentContexts": i.intent_contexts,
                "intentAdminState": i.intent_admin_state, "intentPriority": i.intent_priority,
                "intentPreemptionCapability": i.intent_preemption_capability,
                "intentReportControl": i.intent_report_control, "implicitIntentIndex": i.implicit_intent_index,
                "guaranteePeriods": i.guarantee_periods, "intentHandlingInfo": i.intent_handling_info,
                "intentInterpretationAssistanceInfo": i.intent_interpretation_assistance_info,
                "intentReportReference": _s(i.intent_report_reference),
                "intentUtilityFormulaRef": _s(i.intent_utility_formula_id)}.items() if v is not None}}


def _rmih_view(fn: IntentHandlingFunction) -> dict:
    """Returns the wire form of a handling function: the summary keys (`rmihId`, `smeServiceId`, `notificationDestination`, `intentHandlingScope`) plus the spec
    attributes under `attributes`.
    """
    return {"id": fn.rmih_id, "rmihId": fn.rmih_id, "smeServiceId": fn.sme_service_id,
            "notificationDestination": fn.notification_destination, "intentHandlingScope": fn.intent_handling_scope,
            "attributes": {"intentHandlingScope": fn.intent_handling_scope,
                           "intentHandlingCapabilityList": fn.intent_handling_capability_list,
                           "supportedNegotiationFunctionalities": fn.supported_negotiation_functionalities,
                           "supportedUtilityList": fn.supported_utility_list}}


@app.get("/intent-handling-functions")
def list_intent_handling_functions(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """List read over registered RMIHs (GUI pass)."""
    page = paginate(db, select(IntentHandlingFunction), limit, offset)
    return {**page, "items": [_rmih_view(fn) for fn in page["items"]]}


@app.get("/intent-reports")
def list_intent_reports(intent_id: uuid.UUID | None = None, limit: int = PageLimit, offset: int = PageOffset,
                         db: Session = Depends(get_session)):
    """Read side of publish_intent_report, newest first."""
    stmt = select(IntentReport)
    if intent_id:
        stmt = stmt.where(IntentReport.intent_id == intent_id)
    page = paginate(db, stmt.order_by(IntentReport.last_updated_time.desc()), limit, offset)
    return {**page, "items": [_report_view(r) for r in page["items"]]}


@app.get("/intent-reports/{report_id}")
def get_intent_report(report_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` (not `INTENT_NOT_FOUND`) for an unknown report id.
    report = db.get(IntentReport, report_id)
    if report is None:
        raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail="no such IntentReport")
    return _report_view(report)


# ---------------------------------------------------------------- IntentUtilityFormula (TS 28.312 IOC)

def _formula_view(f: IntentUtilityFormula) -> dict:
    """Returns the wire form of a utility formula: `id` and its spec attributes under `attributes`."""
    return {"id": str(f.intent_utility_formula_id), "attributes": {
        "utilityFunctionId": f.utility_function_id, "utilityParameterList": f.utility_parameter_list,
        "utilityScale": f.utility_scale, "utilityOffset": f.utility_offset}}


@app.post("/intent-utility-formulas", status_code=201)
def create_intent_utility_formula(body: IntentUtilityFormulaRequest, db: Session = Depends(get_session)):
    # Route notes: 201. Stored as sent; an intent can name it later through `intentUtilityFormulaRef`.
    f = IntentUtilityFormula(utility_function_id=body.utilityFunctionId,
                             utility_parameter_list=ts28312.dump(body.utilityParameterList),
                             utility_scale=body.utilityScale, utility_offset=body.utilityOffset)
    db.add(f)
    db.commit()
    return _formula_view(f)


@app.get("/intent-utility-formulas")
def list_intent_utility_formulas(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated.
    page = paginate(db, select(IntentUtilityFormula), limit, offset)
    return {**page, "items": [_formula_view(f) for f in page["items"]]}


@app.get("/intent-utility-formulas/{formula_id}")
def get_intent_utility_formula(formula_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    f = db.get(IntentUtilityFormula, formula_id)
    if f is None:
        raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail="no such IntentUtilityFormula")
    return _formula_view(f)


@app.delete("/intent-utility-formulas/{formula_id}", status_code=204)
def delete_intent_utility_formula(formula_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: idempotent, 204 for an unknown id. Intents that named the formula keep existing; the database sets their reference to null (PostgreSQL).
    f = db.get(IntentUtilityFormula, formula_id)
    if f is not None:
        db.delete(f)
        db.commit()


# ---------------------------------------------------------------- HISTORY.md OI-6.3: rApp Autonomy Modes

def _notify_autonomy_operator(db: Session, notification_destination: str | None, dispatch: AutonomyDispatch) -> None:
    """Enqueues the dispatch summary for the operator at `notification_destination`; does nothing when it is None; the caller commits.

    Every mode notifies, and so does every outcome (request, resolve, reject): sending is not tied to the autonomy mode. The payload carries the dispatch id,
    instance, model, mode, current status, expectations, priority and the intent id (null until one exists). An outbox row in the caller's transaction
    (PR-MSG-1.9), screened by the SSRF guard of `smo_shared.webhook` at enqueue and again at send.
    """
    enqueue(db, notification_destination, {
        "dispatchId": str(dispatch.dispatch_id), "instanceId": str(dispatch.instance_id),
        "modelId": str(dispatch.model_id) if dispatch.model_id else None,
        "autonomyMode": dispatch.autonomy_mode, "status": dispatch.status,
        "expectations": dispatch.expectations, "priority": dispatch.priority,
        "intentId": str(dispatch.intent_id) if dispatch.intent_id else None,
    })


@app.post("/autonomy-dispatches", status_code=201)
def request_autonomy_dispatch(body: CreateAutonomyDispatchRequest, request: Request, db: Session = Depends(get_session)):
    """RequestAutonomyDispatch — the real, spec-shaped hand-off call flow
    02/03 lacked: an rApp that just pulled an inference result via DME
    calls this instead of a raw CM write (call flow 03 Path A/B) or an
    unaddressed CreateIntent, and its own onboarding-time `autonomyMode`
    (rApp Mgmt's own RAppInstance) decides what happens next —
    AUTONOMOUS enacts the outcome immediately, as part of a real Intent,
    scoped to the instance's own pre-configured regionScope; ASSIST holds
    it for an operator to scope before anything is dispatched; SHADOW
    computes nothing beyond this record — observe-only, never dispatched.
    All three always notify the operator.
    """
    # Route notes (the docstring above is published): 201 with the dispatch view. In order: (1) `GET /rapp-mgmt/instances/{id}` through `R1Client`; any answer but
    # 200 is 404 `RAPP_INSTANCE_NOT_FOUND` (an rApp Management outage looks the same). (2) 404 `INTENT_HANDLING_FUNCTION_NOT_FOUND` and 422
    # `RMIH_CAPABILITY_MISMATCH` (object types and scope only) for every mode. (3) The dispatch row is stored with the instance's mode copied onto it. (4)
    # AUTONOMOUS: the instance's `regionScope` is applied, an intent is created with `_create_intent_row` (which can still refuse it: purpose, infeasible target, 422
    # on a region-scope conflict) and the dispatch becomes DISPATCHED; ASSIST: AWAITING_SCOPE; any other mode value behaves as SHADOW. (5) The operator is
    # notified and everything commits once, so a refused intent leaves no dispatch behind. Call flow 09d: an rApp dispatches only for its own instance, the one
    # whose `oauthClientId` is its invoker id (403 `INSTANCE_IDENTITY_MISMATCH`, no fallback to a shared identity); the console and modules for any. The caller is
    # recorded as `dispatchedBy` and owns the intent the dispatch makes.
    try:
        inst_resp = _r1.get(f"/rapp-mgmt/instances/{body.instanceId}")
    except httpx.HTTPError as exc:          # DOC-CF-2.5: rApp Management not answering is a 503, not a 500
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"rApp Management did not answer ({type(exc).__name__})") from exc
    if inst_resp.status_code != 200:
        raise framework_error(FrameworkError.RAPP_INSTANCE_NOT_FOUND, detail="no such RAppInstance")
    instance = inst_resp.json()
    caller = invoker_id(request)
    if _is_rapp(request) and (not caller or instance.get("oauthClientId") != caller):
        raise framework_error(FrameworkError.INSTANCE_IDENTITY_MISMATCH,
                              detail="an rApp dispatches only for its own instance (call it with that instance's credentials)")
    autonomy_mode = instance["autonomyMode"]

    # Validated the same way for all three modes, up front — even SHADOW
    # "computes" the Intent it would have produced (design: "the Intent
    # (or its equivalent) is computed and surfaced"), so a SHADOW dispatch
    # addressed to a non-existent or incapable RMIH is rejected here too,
    # not silently accepted because nothing real ends up dispatched.
    fn = db.get(IntentHandlingFunction, body.rmihId)
    if fn is None:
        raise framework_error(FrameworkError.INTENT_HANDLING_FUNCTION_NOT_FOUND, detail="no such intent handling function")
    expectations = ts28312.dump(body.expectations)
    _validate_rmih_can_handle(fn, _requested_expectation_object_types(expectations), body.intentHandlingScope)

    # The row starts as SHADOWED and the branches below only move it away from that: a mode that is neither AUTONOMOUS nor ASSIST stays observe-only.
    dispatch = AutonomyDispatch(instance_id=body.instanceId, model_id=body.modelId, autonomy_mode=autonomy_mode,
                                 expectations=expectations, priority=body.priority, rmih_id=body.rmihId,
                                 intent_mgmt_purpose=body.intentMgmtPurpose, intent_handling_scope=body.intentHandlingScope,
                                 notification_destination=body.notificationDestination, status="SHADOWED", dispatched_by=caller)
    db.add(dispatch)
    db.flush()
    if autonomy_mode == "AUTONOMOUS":
        # Set before `_dispatch_intent_request`, which reads `dispatch.region_scope` to fold the scope into the expectations.
        dispatch.region_scope = instance.get("regionScope")
        intent = _create_intent_row(db, _dispatch_intent_request(dispatch, body.userLabel), owner=caller)
        dispatch.status = "DISPATCHED"
        dispatch.intent_id = intent.intent_id
    elif autonomy_mode == "ASSIST":
        dispatch.status = "AWAITING_SCOPE"
    # SHADOW: dispatch.status stays "SHADOWED" — no Intent, ever, for this record.

    db.add(dispatch)
    _notify_autonomy_operator(db, body.notificationDestination, dispatch)
    db.commit()
    return _autonomy_dispatch_view(dispatch)


@app.post("/autonomy-dispatches/{dispatch_id}/resolve")
def resolve_autonomy_dispatch(dispatch_id: uuid.UUID, body: ResolveAutonomyDispatchRequest, request: Request, db: Session = Depends(get_session)):
    """ASSIST's own human-in-the-loop scoping step: the operator supplies
    (or narrows) which RAN nodes/cells/slices the dispatch applies to,
    only now creating the real Intent — distinct from AUTONOMOUS, whose
    scope was already fixed at onboarding.
    """
    # Route notes (the docstring above is published): 404 `AUTONOMY_DISPATCH_NOT_FOUND`; 409 `AUTONOMY_DISPATCH_NOT_AWAITING_SCOPE` for any status but
    # AWAITING_SCOPE. The operator's `regionScope` is stored as given and folded into the expectations (`_scoped`); the intent is created with the
    # dispatch's recorded values and a generated label. An intent that `_create_intent_row` or `_scoped` refuses (422) leaves the dispatch AWAITING_SCOPE, since nothing was
    # committed. The operator destination is notified; one commit. Call flow 09e: 403 `ROLE_NOT_PERMITTED` for an rApp, and for an internal caller with no
    # console user; the user is recorded as `resolvedBy`. The intent is owned by the rApp that dispatched, as an AUTONOMOUS one would be.
    decider = _operator(request, None)
    dispatch = db.get(AutonomyDispatch, dispatch_id)
    if dispatch is None:
        raise framework_error(FrameworkError.AUTONOMY_DISPATCH_NOT_FOUND, detail="no such autonomy dispatch")
    if dispatch.status != "AWAITING_SCOPE":
        raise framework_error(FrameworkError.AUTONOMY_DISPATCH_NOT_AWAITING_SCOPE,
                               detail=f"cannot resolve a dispatch in status {dispatch.status}")

    dispatch.region_scope = body.regionScope
    intent = _create_intent_row(db, _dispatch_intent_request(dispatch, None), owner=dispatch.dispatched_by)
    dispatch.status = "DISPATCHED"
    dispatch.intent_id = intent.intent_id
    dispatch.resolved_by = decider
    _notify_autonomy_operator(db, dispatch.notification_destination, dispatch)
    db.commit()
    return _autonomy_dispatch_view(dispatch)


# Body of POST /autonomy-dispatches/{id}/reject. `rejectedBy` is replaced by the console user (`X-R1-Acting-User`) when there is one; the GUI BFF also pins it.
class RejectAutonomyDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rejectedBy: str
    reason: str | None = None


@app.post("/autonomy-dispatches/{dispatch_id}/reject")
def reject_autonomy_dispatch(dispatch_id: uuid.UUID, body: RejectAutonomyDispatchRequest, request: Request, db: Session = Depends(get_session)):
    """Wave 8 (W8-08, decision D-1b): ASSIST's other operator answer — the
    operator declines the recommendation outright. Only from AWAITING_SCOPE
    (an ASSIST dispatch stays there until it is either resolved or
    rejected); no Intent is ever created, and the operator destination is
    notified like every other dispatch outcome."""
    # Route notes (the docstring above is published): 404 `AUTONOMY_DISPATCH_NOT_FOUND`; 409 `AUTONOMY_DISPATCH_NOT_AWAITING_SCOPE` for any status but
    # AWAITING_SCOPE. Stores `rejectedBy` and the reason, creates no intent, notifies the operator destination, one commit. Call flow 09e: 403
    # `ROLE_NOT_PERMITTED` for an rApp, and for an internal caller with no console user; `rejectedBy` is that user.
    decider = _operator(request, body.rejectedBy)
    dispatch = db.get(AutonomyDispatch, dispatch_id)
    if dispatch is None:
        raise framework_error(FrameworkError.AUTONOMY_DISPATCH_NOT_FOUND, detail="no such autonomy dispatch")
    if dispatch.status != "AWAITING_SCOPE":
        raise framework_error(FrameworkError.AUTONOMY_DISPATCH_NOT_AWAITING_SCOPE,
                               detail=f"cannot reject a dispatch in status {dispatch.status}")
    dispatch.status = "REJECTED"
    dispatch.rejected_by = decider
    dispatch.rejection_reason = body.reason
    _notify_autonomy_operator(db, dispatch.notification_destination, dispatch)
    db.commit()
    return _autonomy_dispatch_view(dispatch)


@app.get("/autonomy-dispatches/{dispatch_id}")
def query_autonomy_dispatch(dispatch_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `AUTONOMY_DISPATCH_NOT_FOUND` for an unknown id.
    dispatch = db.get(AutonomyDispatch, dispatch_id)
    if dispatch is None:
        raise framework_error(FrameworkError.AUTONOMY_DISPATCH_NOT_FOUND, detail="no such autonomy dispatch")
    return _autonomy_dispatch_view(dispatch)


@app.get("/autonomy-dispatches")
def list_autonomy_dispatches(instance_id: uuid.UUID | None = None, status: str | None = None, limit: int = PageLimit,
                              offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: optional exact-match filters `instance_id` and `status` (the status is not validated); newest first; paginated.
    stmt = select(AutonomyDispatch)
    if instance_id:
        stmt = stmt.where(AutonomyDispatch.instance_id == instance_id)
    if status:
        stmt = stmt.where(AutonomyDispatch.status == status)
    page = paginate(db, stmt.order_by(AutonomyDispatch.created_at.desc()), limit, offset)
    return {**page, "items": [_autonomy_dispatch_view(d) for d in page["items"]]}


def _scoped(expectation: dict, region_scope: dict | None) -> dict:
    """Returns a copy of `expectation` with the dispatch's `region_scope` folded in; raises 422 `SCHEMA_VALIDATION_FAILED` when the expectation is outside it.

    With no scope the expectation is returned unchanged. `regionScope.objectInstance` fills a missing `objectInstance`; an expectation that names a different one is
    refused. `regionScope.cells` bound the expectation's `Cell` object contexts: cells outside the region are dropped, and an empty remainder is refused, so a
    scope never widens what the rApp asked for. An expectation with no `Cell` context gets one made of all the region's cells. Other `regionScope` keys stay on
    the dispatch and are not applied.
    """
    if not region_scope:
        return expectation
    # A deep copy through JSON, so the expectations stored on the dispatch are never modified in place.
    scoped = json.loads(json.dumps(expectation))
    obj = scoped["expectationObject"]
    region_instance = region_scope.get("objectInstance")
    if region_instance and not obj.get("objectInstance"):
        obj["objectInstance"] = region_instance
    elif region_instance and obj["objectInstance"] != region_instance:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                              detail=f"{obj['objectInstance']} is outside the dispatch's regionScope ({region_instance})")
    region_cells = region_scope.get("cells")
    if region_cells:
        # Wave 10.1: an expectation that already names its cells (e.g. one
        # cell an rApp decided to sleep) is *bounded* by the region scope,
        # never widened to it — cells outside the region are dropped, and
        # nothing left in scope is refused. Otherwise the region's cells
        # become the expectation's Cell context.
        cell_contexts = [c for c in obj.get("objectContexts") or [] if c.get("contextAttribute") == "Cell"]
        allowed = {str(c) for c in region_cells}
        for ctx in cell_contexts:
            values = ctx["contextValueRange"] if isinstance(ctx["contextValueRange"], list) else [ctx["contextValueRange"]]
            ctx["contextValueRange"] = [v for v in values if str(v) in allowed]
            if not ctx["contextValueRange"]:
                raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                                      detail=f"cells {values} are outside the dispatch's regionScope cells {region_cells}")
        if not cell_contexts:
            obj.setdefault("objectContexts", []).append(
                {"contextAttribute": "Cell", "contextCondition": "IS_ALL_OF", "contextValueRange": region_cells})
    return scoped


def _dispatch_intent_request(dispatch: AutonomyDispatch, user_label: str | None) -> CreateIntentRequest:
    """Builds the strict `CreateIntentRequest` that an AUTONOMOUS or resolved ASSIST dispatch turns into an intent.

    Expectations are the dispatched ones with the region scope folded in (`_scoped`). The report control has a 60 second observation period and, when the
    dispatch has a notification destination, sends reports there. `rmioId` is the instance id, so the instance is the intent's creator. The request is
    built with `model_validate`, so the stored JSON is validated again; a `ValidationError` from it is not translated and would answer 500.
    """
    control: dict[str, Any] = {"observationPeriod": 60}
    if dispatch.notification_destination:
        control["reportRecipientAddress"] = dispatch.notification_destination
    return CreateIntentRequest.model_validate({       # the dispatch's stored JSON is validated into the strict model here
        "userLabel": user_label or f"autonomy-dispatch {dispatch.dispatch_id}",
        "intentExpectations": [_scoped(e, dispatch.region_scope) for e in dispatch.expectations],
        "intentPriority": dispatch.priority, "intentMgmtPurpose": dispatch.intent_mgmt_purpose or "FULFILMENT_WITHOUT_NEGOTIATION",
        "intentReportControl": [control], "rmihId": dispatch.rmih_id, "rmioId": str(dispatch.instance_id),
        "intentHandlingScope": dispatch.intent_handling_scope})


def _autonomy_dispatch_view(d: AutonomyDispatch) -> dict:
    """Returns the wire form of a dispatch, with ids as strings (null when unset) and `createdAt` in ISO 8601."""
    return {
        "dispatchId": str(d.dispatch_id), "instanceId": str(d.instance_id),
        "modelId": str(d.model_id) if d.model_id else None, "autonomyMode": d.autonomy_mode,
        "expectations": d.expectations, "priority": d.priority, "rmihId": d.rmih_id,
        "intentMgmtPurpose": d.intent_mgmt_purpose, "intentHandlingScope": d.intent_handling_scope,
        "regionScope": d.region_scope, "status": d.status,
        "intentId": str(d.intent_id) if d.intent_id else None,
        "rejectedBy": d.rejected_by, "rejectionReason": d.rejection_reason,
        "dispatchedBy": d.dispatched_by, "resolvedBy": d.resolved_by,
        "createdAt": d.created_at.isoformat(),
    }
