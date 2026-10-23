"""The TS 28.104 MDA resources of MDAF, served as `{"id", "attributes"}` resources with the spec's attribute names:
MDAFunction, MDARequest and MDAReport (design records: `HISTORY.md` W5-01 to W5-04, `docs/STANDARDS.md` decision D-9).

Where it sits: a router mounted into the app by `main.py`, which also owns the older producer-push surface (`POST /reports`,
`/subscriptions`). Both kinds of report, the spec-shaped one from `POST /mda-reports` and the legacy one, are matched against
the open MDA requests and delivered per each request's `reportingMethod`:

  NOTIFICATION  an outbox row POSTing the report to `reportingTarget`
  FILE          the report is downloadable (`GET /mda-reports/{id}/file`) and a file-ready notification goes to `reportingTarget`
  STREAMING     a delivery row is recorded and nothing is sent; this build has no TS 28.532 streaming transport (OPEN_ITEMS.md §3)

A DRIFT report that names a model (`mLModelRef` output) is also forwarded to that model's AIMgF MLMF subscriptions as a
performance report, so AIMgF's guard-KPI floor decides whether retraining fires. MDAF forwards it on its own account, not for the publisher.

An MDA request belongs to who made it (MDA-REQ-OWNER): for an rApp, `requestedBy` is its own invoker id, and only that rApp, the console or an SMO module
may withdraw it.

Before editing: `_deliver` commits (it ends the publish transaction that `main.py` started); the request matching in
`_request_matches` mutates each request's `threshold_state`, which is committed with the report. The docstrings of routes and
request bodies are published in `docs/openapi/mdaf.json`.
"""

import datetime
import json
import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.invoker import invoker_id, on_own_account
from smo_shared.roles import ROLE_RAPP, role_of
from smo_shared.timeutil import as_utc
from smo_shared.pagination import PageLimit, PageOffset, paginate, paginate_list
from smo_shared.outbox import enqueue

from . import ts28104
from .main import _notify_report_subscribers, _r1, _validate_input_sources_are_real_dme_artifacts, check_publisher
from .models import MDAFReport, MDAFunction, MDAReportDelivery, MDARequest

router = APIRouter()


# Base for the request bodies below: unknown fields are refused (422).
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _s(value) -> str | None:
    """`str(value)`, or None for None."""
    return str(value) if value is not None else None


def _iso(value: datetime.datetime | None) -> str | None:
    """ISO 8601 text of a datetime, or None for None."""
    return value.isoformat() if value is not None else None


def _aware(value: datetime.datetime | None) -> datetime.datetime | None:
    """Treats a naive datetime as UTC (SQLite returns timezone-aware columns naive); None passes."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value


def _get(db: Session, cls, object_id: uuid.UUID, name: str):
    """Loads an object by id or raises 404 NRM_OBJECT_NOT_FOUND naming `name`."""
    obj = db.get(cls, object_id)
    if obj is None:
        raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail=f"no such {name} {object_id}")
    return obj


# ================================================================ MDAFunction

# Body of `POST` and `PUT /mda-functions`; capabilities must be TS 28.104 MDA types.
class MDAFunctionBody(_Body):
    userLabel: str | None = None
    supportedMDACapabilities: list[ts28104.MDAType] = []
    supportedMDADomain: ts28104.MDADomain | None = None
    mLModelRefList: list[str] = []
    aIMLInferenceFunctionRefList: list[str] = []


def _function_view(f: MDAFunction) -> dict:
    """The MDAFunction resource of a function."""
    return {"id": str(f.mda_function_id), "attributes": {
        "userLabel": f.user_label, "supportedMDACapabilities": list(f.supported_mda_capabilities or []),
        "supportedMDADomain": f.supported_mda_domain, "mLModelRefList": list(f.ml_model_refs or []),
        "aIMLInferenceFunctionRefList": list(f.aiml_inference_function_refs or [])}}


def _apply_function(f: MDAFunction, body: MDAFunctionBody) -> None:
    """Copies the body onto the function; the capabilities are stored sorted and without duplicates. Does not commit."""
    f.user_label = body.userLabel
    f.supported_mda_capabilities = sorted(set(body.supportedMDACapabilities))
    f.supported_mda_domain = body.supportedMDADomain
    f.ml_model_refs = body.mLModelRefList
    f.aiml_inference_function_refs = body.aIMLInferenceFunctionRefList


@router.post("/mda-functions", status_code=201)
def create_mda_function(body: MDAFunctionBody, db: Session = Depends(get_session)):
    # Creates an MDAFunction (201) and returns its resource.
    f = MDAFunction()
    _apply_function(f, body)
    db.add(f)
    db.commit()
    return _function_view(f)


@router.get("/mda-functions")
def list_mda_functions(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Paginated MDAFunction resources.
    page = paginate(db, select(MDAFunction), limit, offset)
    return {**page, "items": [_function_view(f) for f in page["items"]]}


@router.get("/mda-functions/{function_id}")
def get_mda_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # One MDAFunction resource (404 NRM_OBJECT_NOT_FOUND).
    return _function_view(_get(db, MDAFunction, function_id, "MDAFunction"))


@router.put("/mda-functions/{function_id}")
def replace_mda_function(function_id: uuid.UUID, body: MDAFunctionBody, db: Session = Depends(get_session)):
    # Replaces all writable attributes of a function (404 NRM_OBJECT_NOT_FOUND).
    f = _get(db, MDAFunction, function_id, "MDAFunction")
    _apply_function(f, body)
    db.commit()
    return _function_view(f)


@router.delete("/mda-functions/{function_id}", status_code=204)
def delete_mda_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # Deletes a function (204; an unknown id is 204 too). Requests and reports that named it keep existing; on Postgres the
    # reference is cleared by ON DELETE SET NULL.
    f = db.get(MDAFunction, function_id)
    if f is not None:
        db.delete(f)
        db.commit()


# ================================================================ MDARequest

# Body of `POST /mda-requests`. Only the outputs, the scope, the time window and the IE filters take part in matching; the other
# attributes are stored and returned.
class MDARequestBody(_Body):
    mDAFunctionRef: uuid.UUID | None = None
    requestedMDAOutputs: list[ts28104.MDAOutputPerMDAType]
    reportingMethod: ts28104.ReportingMethod
    reportingTarget: str | None = None
    analyticsScope: ts28104.AnalyticsScopeType | None = None
    startTime: datetime.datetime | None = None
    stopTime: datetime.datetime | None = None
    recommendationFilter: ts28104.AnalyticsScopeType | None = None
    performanceThresholdInfo: list[ts28104.ThresholdInfo] | None = None
    analysisRequirements: ts28104.AnalysisRequirement | None = None
    thresholdMonitorRefList: list[str] | None = None
    # Not a spec attribute — who asked, as on /subscriptions. From an rApp it is ignored: the caller's own invoker id is stored (MDA-REQ-OWNER).
    requestedBy: str | None = None


def _request_view(r: MDARequest) -> dict:
    """The MDARequest resource, with `active` computed now from its time window (not stored)."""
    return {"id": str(r.mda_request_id), "attributes": {
        "mDAFunctionRef": _s(r.mda_function_id), "requestedMDAOutputs": r.requested_mda_outputs,
        "reportingMethod": r.reporting_method, "reportingTarget": r.reporting_target,
        "analyticsScope": r.analytics_scope, "startTime": _iso(r.start_time), "stopTime": _iso(r.stop_time),
        "recommendationFilter": r.recommendation_filter, "performanceThresholdInfo": r.performance_threshold_info,
        "analysisRequirements": r.analysis_requirements, "thresholdMonitorRefList": r.threshold_monitor_refs,
        "requestedBy": r.requested_by, "active": _is_active(r, datetime.datetime.now(datetime.UTC))}}


def _is_active(r: MDARequest, now: datetime.datetime) -> bool:
    """True when `now` is inside the request's window; no start means already started and no stop means open ended. The stop time itself is outside."""
    start, stop = _aware(r.start_time), _aware(r.stop_time)
    return (start is None or start <= now) and (stop is None or now < stop)


@router.post("/mda-requests", status_code=201)
def create_mda_request(body: MDARequestBody, request: Request, db: Session = Depends(get_session)):
    """A request names the MDA outputs it wants. When it is addressed to an
    MDAFunction, every requested mDAType must be one of that function's
    supportedMDACapabilities. NOTIFICATION and FILE delivery need a
    reportingTarget. A request belongs to who made it: for an rApp,
    `requestedBy` is its own invoker id whatever it sent."""
    # Creates a request (201). 422 SCHEMA_VALIDATION_FAILED for empty `requestedMDAOutputs`, NOTIFICATION or FILE without a `reportingTarget`, or a
    # `stopTime` not after the `startTime`; 404 NRM_OBJECT_NOT_FOUND for an unknown `mDAFunctionRef`; 422 MDA_CAPABILITY_NOT_SUPPORTED for a type the
    # function does not support. All checks run before anything is written; times are stored as UTC. MDA-REQ-OWNER: for a caller with the rApp role the
    # stored `requested_by` is its gateway-stamped invoker id (the body's value is ignored), as `POST /subscriptions` does, so an rApp cannot make a request in
    # another's name; the console and SMO modules (role `internal`) and a call with no role name whoever they make it for, and their value is stored as sent.
    if not body.requestedMDAOutputs:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="requestedMDAOutputs must not be empty")
    if body.reportingMethod in ("NOTIFICATION", "FILE") and not body.reportingTarget:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                               detail=f"reportingMethod {body.reportingMethod} needs a reportingTarget")
    start, stop = _aware(body.startTime), _aware(body.stopTime)
    if start and stop and stop <= start:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="stopTime must be after startTime")
    if body.mDAFunctionRef is not None:
        function = _get(db, MDAFunction, body.mDAFunctionRef, "MDAFunction")
        unsupported = sorted({o.mDAType for o in body.requestedMDAOutputs} - set(function.supported_mda_capabilities or []))
        if unsupported:
            raise framework_error(FrameworkError.MDA_CAPABILITY_NOT_SUPPORTED,
                                   detail=f"MDAFunction {function.mda_function_id} does not support {unsupported}")
    requested_by = body.requestedBy
    if role_of(request) == ROLE_RAPP:
        requested_by = invoker_id(request) or requested_by
    r = MDARequest(mda_function_id=body.mDAFunctionRef, requested_by=requested_by,
                   requested_mda_outputs=ts28104.dump(body.requestedMDAOutputs), reporting_method=body.reportingMethod,
                   reporting_target=body.reportingTarget, analytics_scope=ts28104.dump(body.analyticsScope),
                   start_time=_aware(body.startTime), stop_time=_aware(body.stopTime),
                   recommendation_filter=ts28104.dump(body.recommendationFilter),
                   performance_threshold_info=ts28104.dump(body.performanceThresholdInfo),
                   analysis_requirements=ts28104.dump(body.analysisRequirements),
                   threshold_monitor_refs=body.thresholdMonitorRefList)
    db.add(r)
    db.commit()
    return _request_view(r)


@router.get("/mda-requests")
def list_mda_requests(requested_by: str | None = None, limit: int = PageLimit, offset: int = PageOffset,
                      db: Session = Depends(get_session)):
    # Paginated MDARequest resources, optionally of one `requested_by`.
    stmt = select(MDARequest)
    if requested_by:
        stmt = stmt.where(MDARequest.requested_by == requested_by)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_request_view(r) for r in page["items"]]}


@router.get("/mda-requests/{request_id}")
def get_mda_request(request_id: uuid.UUID, db: Session = Depends(get_session)):
    # One MDARequest resource (404 NRM_OBJECT_NOT_FOUND).
    return _request_view(_get(db, MDARequest, request_id, "MDARequest"))


@router.delete("/mda-requests/{request_id}", status_code=204)
def delete_mda_request(request_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """Withdraws the request: no further reports are matched to it. An rApp withdraws only the requests it made
    (403 MDA_REQUEST_ACCESS_DENIED); the console and SMO modules may withdraw any."""
    # An unknown id is 204 as well (idempotent). MDA-REQ-OWNER: the maker rule of `DELETE /subscriptions` (`unsubscribe_analytics`): a caller with the
    # rApp role must be the request's `requested_by`, else 403 and nothing is deleted. A request stored before the rule (its `requested_by` whatever the body
    # said, or none) can be withdrawn by an rApp only if that value is its invoker id; the console or an SMO module removes the others.
    r = db.get(MDARequest, request_id)
    if r is None:
        return
    if role_of(request) == ROLE_RAPP and r.requested_by != invoker_id(request):
        raise framework_error(FrameworkError.MDA_REQUEST_ACCESS_DENIED, detail=f"MDA request {request_id} belongs to {r.requested_by}")
    db.delete(r)
    db.commit()


# ================================================================ MDAReport

# Body of `POST /mda-reports`. `inputSources` must be DME data job ids; `mDARequestRef` addresses one request, and without it
# the report is matched against all open requests.
class MDAReportBody(_Body):
    mDAOutputs: list[ts28104.MDAOutputs]
    # Set when a producer answers one specific request; omitted, the report
    # is matched against every open request.
    mDARequestRef: uuid.UUID | None = None
    mDAFunctionRef: uuid.UUID | None = None
    # Not spec attributes: the managed entities the analysis covers (for
    # scope matching), the DME data jobs it was computed from (the Wave 3
    # "MDAF sources from DME only" rule) and this build's report kind.
    managedEntitiesScope: list[str] | None = None
    inputSources: list[uuid.UUID] = []
    reportKind: ts28104.ReportKind | None = None


def _outputs_of(report: MDAFReport) -> list[dict]:
    """A legacy `POST /reports` report has no typed mDAOutputs; its
    free-form output is shown as MDAOutputEntry pairs under its own
    analytics_type (or the mDAType a producer declared for it)."""
    if report.mda_outputs is not None:
        return report.mda_outputs
    return [{"mDAType": report.mda_type or report.analytics_type,
             "mDAOutputList": [{"mDAOutputIEName": k, "mDAOutputIEValue": v} for k, v in (report.output or {}).items()]}]


def _report_view_for(db: Session):
    """Returns a function that renders a report as the MDAReport resource for this session, including the ids of the requests it was
    delivered to (one query per report). A closure over `db` so a list can render many reports with one session.
    """
    def view(report: MDAFReport) -> dict:
        deliveries = db.scalars(select(MDAReportDelivery).where(MDAReportDelivery.report_id == report.report_id)).all()
        return {"id": str(report.report_id), "attributes": {
            "mDAReportID": str(report.report_id), "mDAOutputs": _outputs_of(report),
            "mDARequestRef": _s(report.mda_request_id), "mDAFunctionRef": _s(report.mda_function_id),
            "reportKind": report.report_kind, "scope": report.scope, "publishedBy": report.published_by,
            "deliveredToRequestRefList": [str(d.mda_request_id) for d in deliveries],
            "generatedAt": _iso(report.generated_at)}}
    return view


def _flat_entries(report: MDAFReport) -> dict[str, dict]:
    """Flattens a report's outputs to `{mDAType: {IE name: value}}` for filtering and thresholds. Typed outputs are used as they
    are, entry pairs are folded into a dict, a prediction list adds one entry per `pmName` with its predicted value, and several
    outputs of the same type are merged (later values win).
    """
    out: dict[str, dict] = {}
    for item in _outputs_of(report):
        flat = dict(item["mDAOutputList"]) if isinstance(item["mDAOutputList"], dict) else {
            e["mDAOutputIEName"]: e.get("mDAOutputIEValue") for e in item["mDAOutputList"]}
        for prediction in flat.get("pmPredictions") or []:
            flat[prediction["pmName"]] = prediction["pmPredictedValue"]
        out.setdefault(item["mDAType"], {}).update(flat)
    return out


def _threshold_crossed(state: dict, entry: dict, value) -> tuple[bool, dict]:
    """The same edge-triggered test as `main._threshold_crossed`, on one threshold of one request filter, but pure: it returns
    `(crossed, new_state)` and leaves the caller to store the state. `entry["hysteresis"]` is optional here (default 0).
    """
    ie, direction, threshold, hysteresis = (entry["monitoredMDAOutputIE"], entry["thresholdDirection"],
                                            entry["thresholdValue"], entry.get("hysteresis", 0))
    previous = state.get(ie)
    if direction in ("UP", "UP_AND_DOWN") and value >= threshold and previous != "ABOVE":
        return True, {**state, ie: "ABOVE"}
    if direction in ("DOWN", "UP_AND_DOWN") and value <= threshold and previous != "BELOW":
        return True, {**state, ie: "BELOW"}
    if value < threshold - hysteresis:
        return False, {**state, ie: "BELOW"}
    if value > threshold + hysteresis:
        return False, {**state, ie: "ABOVE"}
    return False, state


def _request_matches(request: MDARequest, report: MDAFReport, entries: dict[str, dict], now: datetime.datetime) -> bool:
    """Decides whether `report` satisfies `request`, and updates the request's threshold state as a side effect.

    A request must be active and its `managedEntitiesScope` (if it has one and the report has one) must overlap the report's
    (a request or a report without one passes). Then
    it matches when at least one requested output type is in the report and passes its filters: a `timeOut` in the past fails the
    output, a `filterValue` must equal the text of the IE value, and a `threshold` must have fired on this report (numeric values
    only; a non-numeric value never fires). An `areaScope` is not evaluated. `request.threshold_state` is replaced with the
    state after all thresholds were looked at, whether or not the request matched; the caller commits it.
    """
    if not _is_active(request, now):
        return False
    scope = (request.analytics_scope or {}).get("managedEntitiesScope")
    report_scope = (report.scope or {}).get("managedEntitiesScope")
    if scope and report_scope is not None and not set(scope) & set(report_scope):
        return False
    state = dict(request.threshold_state or {})
    matched = False
    for wanted in request.requested_mda_outputs:
        values = entries.get(wanted["mDAType"])
        if values is None:
            continue
        filters = wanted.get("mDAOutputIEFilters") or []
        ok, gated = True, False
        for f in filters:
            if f.get("timeOut") and as_utc(datetime.datetime.fromisoformat(f["timeOut"].replace("Z", "+00:00"))) <= now:
                ok = False
                break
            value = values.get(f["mDAOutputIEName"])
            if f.get("filterValue") is not None and str(value) != f["filterValue"]:
                ok = False
                break
            for threshold in f.get("threshold") or []:
                # A threshold gates the output: it passes only if the threshold fired on this report. `gated` is set again for
                # each threshold, so with several thresholds the last one evaluated decides.
                gated = True
                if isinstance(value, (int, float)):
                    crossed, state = _threshold_crossed(state, threshold, value)
                    if crossed:
                        gated = False
        if ok and not gated:
            matched = True
    request.threshold_state = state
    return matched


def _deliver(db: Session, report: MDAFReport, view: dict) -> None:
    """Delivers the report to the MDA requests it satisfies and commits the whole publish transaction.

    Candidates: the one request the report names (`mda_request_id`), delivered without further checks, or every request for which
    `_request_matches` is true. For each, a delivery row is written and, per the request's method, an outbox row (NOTIFICATION:
    `notifyMDAReport` with the report; FILE: `notifyFileReady` with the file's path) is queued; STREAMING only records the delivery.
    `notified` records that a row was queued, not that it was sent (`enqueue` drops a destination the SSRF guard refuses without
    telling this code). Sending happens after the commit and is retried by the outbox.
    """
    now = datetime.datetime.now(datetime.UTC)
    entries = _flat_entries(report)
    candidates: list[MDARequest | None]
    if report.mda_request_id is not None:
        candidates = [db.get(MDARequest, report.mda_request_id)]
    else:
        candidates = list(db.scalars(select(MDARequest)).all())
    for request in candidates:
        if request is None:
            continue
        if report.mda_request_id is None and not _request_matches(request, report, entries, now):
            continue
        delivery = MDAReportDelivery(report_id=report.report_id, mda_request_id=request.mda_request_id,
                                     reporting_method=request.reporting_method)
        db.add(delivery)
        if request.reporting_method == "NOTIFICATION":
            enqueue(db, request.reporting_target, {
                "notificationType": "notifyMDAReport", "mDARequestRef": str(request.mda_request_id), **view})
            delivery.notified = True  # enqueued, to be sent once this transaction commits
        elif request.reporting_method == "FILE":
            enqueue(db, request.reporting_target, {
                "notificationType": "notifyFileReady", "mDARequestRef": str(request.mda_request_id),
                "fileInfoList": [{"fileLocation": f"/mdaf/mda-reports/{report.report_id}/file",
                                  "fileContent": "MDAReport", "fileDataType": "Analytics"}]})
            delivery.notified = True
    db.commit()


def _forward_drift_to_mlmf(report: MDAFReport) -> list[str]:
    """Passes a DRIFT report's numeric outputs to the AIMgF MLMF subscriptions of the model named by `mLModelRef`, as performance
    report metrics, and returns the subscription ids it reached. Best-effort: any error (AIMgF down, non-200) skips that output
    and never fails the publish. Runs after the report is committed.

    MDAF forwards on its own account (`on_own_account`: no `X-R1-On-Behalf-Of`): routing a drift signal to the model's subscriptions is the platform's act, not the
    publishing rApp's, and the publisher is often an analytics rApp that does not own the model, which AIMgF's owner rule would otherwise refuse
    (MLLF-NODEGROUP-OWNER). Who may publish a DRIFT report at all is `check_publisher`'s rule.
    """
    with on_own_account():
        return _forward_drift_to_subscriptions(report)


def _forward_drift_to_subscriptions(report: MDAFReport) -> list[str]:
    """The body of `_forward_drift_to_mlmf`: for each output with `mLModelRef`, reads the model's MLMF subscriptions and posts the numeric IEs to each; returns
    the subscription ids reached. Every error is swallowed per output."""
    forwarded = []
    for values in _flat_entries(report).values():
        model_ref = values.get("mLModelRef")
        if not model_ref:
            continue
        metrics = {k: v for k, v in values.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        try:
            subs = _r1.get("/aimgf/mlmf/subscriptions", params={"model_id": str(model_ref)})
            if subs.status_code != 200:
                continue
            for sub in subs.json().get("items", []):
                _r1.post(f"/aimgf/mlmf/subscriptions/{sub['subscriptionId']}/reports", json=metrics)
                forwarded.append(sub["subscriptionId"])
        # Deliberately broad: forwarding is an extra and must not turn a stored report into a failed request.
        except Exception:  # noqa: BLE001, S112 — drift forwarding never fails the publish
            continue
    return forwarded


def _infer_kind(outputs: list[dict]) -> str:
    """PREDICTION if any output is one of the prediction MDA types, else ANALYTICS. DRIFT is never inferred; the caller must say so."""
    return "PREDICTION" if any(o["mDAType"] in ts28104.PREDICTION_MDA_TYPES for o in outputs) else "ANALYTICS"


@router.post("/mda-reports", status_code=201)
def publish_mda_report(body: MDAReportBody, request: Request, db: Session = Depends(get_session)):
    # Stores a spec-shaped report. 201 with the MDAReport resource. 403 PRODUCER_NOT_REGISTERED for an rApp not registered for the first output's mDAType
    # (call flow 08c, as for POST /reports). 422 for empty `mDAOutputs` or an unknown DME source; 404
    # NRM_OBJECT_NOT_FOUND for an unknown `mDARequestRef` or `mDAFunctionRef`. The report's `analytics_type` and `mda_type` are the type of
    # the first output; all outputs' entries are also flattened into the free-form `output` (a later output overwrites a same-named
    # entry of an earlier one). Order: checks, add and flush, subscribers of that type notified, delivery to requests (which commits),
    # then drift forwarding for a DRIFT report. The notification payload is rendered before the delivery rows exist, the response
    # after the commit, so only the response lists `deliveredToRequestRefList`.
    if not body.mDAOutputs:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="mDAOutputs must not be empty")
    if body.mDARequestRef is not None:
        _get(db, MDARequest, body.mDARequestRef, "MDARequest")
    if body.mDAFunctionRef is not None:
        _get(db, MDAFunction, body.mDAFunctionRef, "MDAFunction")
    outputs = ts28104.dump(body.mDAOutputs)
    published_by = check_publisher(request, outputs[0]["mDAType"])
    _validate_input_sources_are_real_dme_artifacts(body.inputSources)
    flat = {}
    for o in body.mDAOutputs:
        flat.update(o.entries())
    report = MDAFReport(analytics_type=outputs[0]["mDAType"], mda_type=outputs[0]["mDAType"], mda_outputs=outputs,
                        output=json.loads(json.dumps(flat, default=str)), input_sources=body.inputSources,
                        scope={"managedEntitiesScope": body.managedEntitiesScope} if body.managedEntitiesScope else None,
                        report_kind=body.reportKind or _infer_kind(outputs),
                        mda_request_id=body.mDARequestRef, mda_function_id=body.mDAFunctionRef, published_by=published_by)
    db.add(report)
    db.flush()
    # Existing analytics_type subscribers (keyed by the first mDAType) are
    # notified exactly as for a legacy report; then MDARequest delivery, whose
    # commit makes the report and every notification one transaction (PR-MSG-1.8).
    _notify_report_subscribers(db, report)
    view = _report_view_for(db)(report)
    _deliver(db, report, view)
    if report.report_kind == "DRIFT":
        _forward_drift_to_mlmf(report)
    return _report_view_for(db)(report)


def deliver_legacy_report(db: Session, report: MDAFReport) -> None:
    """Entry point for `POST /reports` in `main.py`: matches a producer-push report against open MDA requests and delivers it. Commits,
    as `_deliver` does.
    """
    _deliver(db, report, _report_view_for(db)(report))


@router.get("/mda-reports")
def list_mda_reports(mda_type: str | None = None, report_kind: str | None = None, mda_request_id: uuid.UUID | None = None,
                     managed_entity: str | None = None, limit: int = PageLimit, offset: int = PageOffset,
                     db: Session = Depends(get_session)):
    """Filters: mDAType, report kind (ANALYTICS/PREDICTION/DRIFT), the
    request it was delivered to, and a managed entity in its scope."""
    stmt = select(MDAFReport)
    if mda_type:
        stmt = stmt.where((MDAFReport.mda_type == mda_type) | (MDAFReport.analytics_type == mda_type))
    if report_kind:
        stmt = stmt.where(MDAFReport.report_kind == report_kind)
    if mda_request_id:
        stmt = stmt.where(MDAFReport.report_id.in_(
            select(MDAReportDelivery.report_id).where(MDAReportDelivery.mda_request_id == mda_request_id)))
    # All matching rows are loaded, newest first; the `managed_entity` filter and the paging are applied to that list in Python,
    # so the cost grows with the number of matching reports.
    rows = db.scalars(stmt.order_by(MDAFReport.generated_at.desc())).all()
    if managed_entity:
        rows = [r for r in rows if managed_entity in ((r.scope or {}).get("managedEntitiesScope") or [])]
    view = _report_view_for(db)
    page = paginate_list(rows, limit, offset)
    return {**page, "items": [view(r) for r in page["items"]]}


@router.get("/mda-reports/{report_id}")
def get_mda_report(report_id: uuid.UUID, db: Session = Depends(get_session)):
    # One MDAReport resource (404 NRM_OBJECT_NOT_FOUND).
    return _report_view_for(db)(_get(db, MDAFReport, report_id, "MDAReport"))


@router.get("/mda-reports/{report_id}/file")
def download_mda_report_file(report_id: uuid.UUID, db: Session = Depends(get_session)):
    """FILE reportingMethod: the report as a downloadable JSON file."""
    report = _get(db, MDAFReport, report_id, "MDAReport")
    body = json.dumps(_report_view_for(db)(report), indent=2).encode()
    return Response(content=body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="mda-report-{report_id}.json"'})
