"""Inference performance monitoring (MLMF; R1GAP v14 §5.6.9, produced by the consumer rApp and subscribed to by AIMgF): subscriptions, reports
against a KPI guard floor, and the group retrain a breach propagates to the model's coordination group (call flows 02e and 13).

Where it sits: one router included by `main.py`. The coordination group is read from the registry in process (`registry.access.coordination_groups`, S2.1);
a retrain starts each member through `training/jobs._start_training`. A failing group step is reported in the answer body, never as a 500 after the report
was stored (MLMF-REPORT-REPLAY).

Before editing: the route docstrings are published in `docs/openapi/aimgf.json`.
"""

import httpx
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.idempotency import idempotent
from smo_shared.outbox import enqueue
from smo_shared.pagination import PageLimit, PageOffset, paginate

from ..governance.statemachine import ModelLifecycleState, should_trigger_group_retrain
from ..models import MLMFSubscription, ModelLifecycle, PerformanceReport
from ..registry import access as registry
from ..common import _check_rapp_owns_if_known, _get_model_or_none, _owned_model, _r1, _visible_model_ids
from ..governance.lifecycle import _get_or_create_lifecycle
from ..training.jobs import _start_training

logger = logging.getLogger(__name__)

router = APIRouter()


def _is_number(value) -> bool:
    """True for an int or a float (a bool is not a number here)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_numeric(values: dict | None, what: str) -> None:
    """Raises 422 `SCHEMA_VALIDATION_FAILED` naming the first entry of `values` (metric name to value) that is not a number; None or {} passes (call flow 13)."""
    for name, value in (values or {}).items():
        if not _is_number(value):
            raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"{what} {name!r} must be a number, not {value!r}")


def _check_dme_type(dme_type_id: uuid.UUID) -> None:
    """Raises 422 `DME_ARTIFACT_NOT_FOUND` unless DME knows the type (`GET /dme/dme-types/{id}` answers 200), as `_validate_dme_data_job_ids` does for data jobs.

    Proves only that the reference is real: AIMgF does not read the type's data, MDAF and the owner rApp report the metrics (call flow 13a).
    """
    if _r1.get(f"/dme/dme-types/{dme_type_id}").status_code != 200:
        raise framework_error(FrameworkError.DME_ARTIFACT_NOT_FOUND, detail=f"no such DME type {dme_type_id}")


def _evaluate_report(sub: MLMFSubscription, metrics: dict) -> tuple[bool, list[str]]:
    """Returns (breached, missing) for a report against the subscription: breached when a guarded metric the report carries is below its floor or above its
    ceiling; missing, the sorted guarded metrics it does not carry, which do not breach (call flow 13b). The values are numbers (`_check_numeric` ran first)."""
    floor, ceiling = sub.guard_kpi_floor or {}, sub.guard_kpi_ceiling or {}
    breached = any(k in metrics and metrics[k] < v for k, v in floor.items()) or any(k in metrics and metrics[k] > v for k, v in ceiling.items())
    return breached, sorted((set(floor) | set(ceiling)) - set(metrics))


def _refuse_if_retired(db: Session, model_id: uuid.UUID, action: str) -> None:
    """Call flow 26e (DOC-CF-1.7): raises 409 `LIFECYCLE_ILLEGAL_TRANSITION` when AIMgF has the model RETIRED. A model with no lifecycle row is not retired. Reads only."""
    lifecycle = db.get(ModelLifecycle, model_id)
    if lifecycle is not None and lifecycle.model_lifecycle_state == ModelLifecycleState.RETIRED:
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION, detail=f"the model is RETIRED: cannot {action}")


@router.post("/mlmf/subscriptions", status_code=201)
@idempotent("aimgf", status_code=201)
def subscribe_performance_monitoring(request: Request, model_id: uuid.UUID, metric_types: list[str], dme_type_id: uuid.UUID, guard_kpi_floor: dict | None = None,
                                      notification_destination: str | None = None, guard_kpi_ceiling: dict | None = None, db: Session = Depends(get_session)):
    """MLMF — new sub-function, AI/ML Workflow LLD section 2. Distinct
    domain from RAN Analytics' MDAF (model performance, not RAN behavior).

    HISTORY.md §7's `MLMFSubscription` finding, closed: `notification_destination`
    (optional, matching every other subscription-shaped resource's own
    permissive shape — a purely poll-based consumer may still omit it).
    """
    # Route notes: 201 `{"subscriptionId"}`. `model_id`, `dme_type_id` and `notification_destination` are query parameters; `metric_types`, `guard_kpi_floor` and
    # `guard_kpi_ceiling` are read from the JSON body. Checks in order (call flow 13a): every floor and ceiling a number (422 `SCHEMA_VALIDATION_FAILED`); the model
    # in the registry for every caller (404 `MODEL_NOT_FOUND`), and for a caller with the rApp role its owner (403 `MODEL_ACCESS_DENIED` unless it owns the model or the
    # model has no owner; SEC-15.3c); the DME type known to DME (422 `DME_ARTIFACT_NOT_FOUND`).
    # `notification_destination` is stored as given and screened by the outbox's SSRF guard when a report is queued, not here. A RETIRED model is 409 `LIFECYCLE_ILLEGAL_TRANSITION` (call flow 26e). One commit.
    # `@idempotent("aimgf")` (SDK-RETRY-IDEMPOTENCY): a keyed repeat gets the first subscriptionId back instead of a second subscription.
    _check_numeric(guard_kpi_floor, "the floor of")
    _check_numeric(guard_kpi_ceiling, "the ceiling of")
    _owned_model(db, request, model_id, "subscribe to the performance of")   # call flow 13a: every caller's model is looked up, an rApp's ownership checked
    _refuse_if_retired(db, model_id, "subscribe to its performance")   # call flow 26e: a retired model serves nothing to monitor
    _check_dme_type(dme_type_id)
    sub = MLMFSubscription(model_id=model_id, metric_types=metric_types, dme_type_id=dme_type_id, guard_kpi_floor=guard_kpi_floor,
                            guard_kpi_ceiling=guard_kpi_ceiling, notification_destination=notification_destination)
    db.add(sub)
    db.commit()
    return {"subscriptionId": str(sub.subscription_id)}


@router.delete("/mlmf/subscriptions/{subscription_id}", status_code=204)
def unsubscribe_performance_monitoring(request: Request, subscription_id: uuid.UUID, db: Session = Depends(get_session)):
    """HISTORY.md §7's `MLMFSubscription` finding, closed: previously
    this subscription could only be created and read, never torn down —
    idempotent, matching every other subscription-shaped resource's own
    unsubscribe route (DME/MDAF/Intent Service).
    """
    # Route notes: 204 whether or not the subscription existed. Its reports go with it (the database cascades the delete). An rApp is refused with 403
    # `MODEL_ACCESS_DENIED` for a subscription on a model another rApp owns (SEC-15.3c); a model the registry no longer knows does not stop an rApp from removing what was recorded for it.
    sub = db.get(MLMFSubscription, subscription_id)
    if sub is not None:
        _check_rapp_owns_if_known(db, request, sub.model_id, "remove a subscription to the performance of")
        db.delete(sub)
        db.commit()


def _find_coordination_group_for_model(db: Session, model_id: uuid.UUID) -> dict | None:
    """Returns the registry's coordination group (the JSON object of `GET /coordination-groups`) that lists `model_id` as a member, or None.

    Reads every group in process (`registry.access.coordination_groups`); if the model is in several groups the first one, in creation order, wins.
    """
    return next((g for g in registry.coordination_groups(db) if str(model_id) in set(g["memberModelIds"])), None)


@router.post("/mlmf/subscriptions/{subscription_id}/reports")
@idempotent("aimgf", status_code=200)
def report_performance(request: Request, subscription_id: uuid.UUID, metrics: dict, db: Session = Depends(get_session)):
    """`docs/call-flows/13-mlmf-subscription-lifecycle.md`'s own gap,
    closed: a report against an unsubscribed or never-existed
    subscription used to raise an unhandled `AttributeError` (a bare
    500) — `sub` was read directly with no null-check. Now a clean
    404, matching every comparable cross-reference elsewhere in this
    build.
    """
    # Route notes. 404 `MLMF_SUBSCRIPTION_NOT_FOUND`; 409 `LIFECYCLE_ILLEGAL_TRANSITION` for a RETIRED model (call flow 26e); 403 `MODEL_ACCESS_DENIED` for an rApp that does not own the subscribed model (SEC-15.3c; a report can start a group retrain, so it is
    # the owner's to file; MDAF files drift reports as an SMO module and is not asked). The body is the metrics object (metric name to number); a value that is not a
    # number is 422 `SCHEMA_VALIDATION_FAILED` with nothing stored. A report breaches when a guarded metric it carries is below its floor or above its ceiling; a
    # guarded metric it does not carry is listed in `missingMetrics` and does not breach (call flow 13b: MDAF's drift reports carry only their own outputs). The
    # report and its subscriber notification (`{reportId, modelId, metrics, breachedFloor, missingMetrics}`) are committed first. Only then, for a breach, is the model's
    # coordination group read from the registry and `should_trigger_group_retrain` asked with `breached_count=1`. When it fires, `_trigger_group_retrain` starts a
    # run for each PROMOTED member, committing each on its own; a member NFO refuses is skipped, not fatal (call flow 02e). Answers `reportId`, `breachedFloor`
    # and, for a group member, `groupRetrainTriggered` and, when it fired, `retrainedModelIds` and `retrainSkipped` (`[{modelId, reason}]`); `missingMetrics` always.
    # Once the report is committed the answer is 200 whatever the group step does (MLMF-REPORT-REPLAY): a failure there (NFO not answering, a registry read failing,
    # WEIGHTED_TRIGGERS or an unknown `retrainPropagation`) is answered as `groupRetrainError` `{code, detail}` (`_group_retrain_step`). A failure before the
    # commit still raises, stores nothing and releases the Idempotency-Key.
    # `@idempotent("aimgf")` (SDK-RETRY-IDEMPOTENCY): a keyed repeat is answered from the first answer, so no second report, notification or group retrain. The
    # path is part of the key's fingerprint, so the same key on another subscription's reports is 422 IDEMPOTENCY_KEY_REUSED.
    sub = db.get(MLMFSubscription, subscription_id)
    if sub is None:
        raise framework_error(FrameworkError.MLMF_SUBSCRIPTION_NOT_FOUND, detail="no such MLMF subscription")
    _check_rapp_owns_if_known(db, request, sub.model_id, "report the performance of")
    _refuse_if_retired(db, sub.model_id, "report its performance")   # call flow 26e
    _check_numeric(metrics, "the metric")
    breached, missing = _evaluate_report(sub, metrics)
    report = PerformanceReport(subscription_id=subscription_id, metrics=metrics, breached_floor=breached, missing_metrics=missing)
    db.add(report)
    db.flush()  # the report's id, for the notification

    # HISTORY.md §7's `MLMFSubscription` finding, closed: the subscriber's
    # push is a transactional-outbox row (PR-MSG-1.7), sent once the report
    # commits — an unreachable subscriber never fails the report call that
    # triggered it, and a crash after the commit no longer loses the push.
    enqueue(db, sub.notification_destination, {
        "reportId": str(report.id), "modelId": str(sub.model_id), "metrics": metrics, "breachedFloor": breached, "missingMetrics": missing,
    })
    # Committed before the group step: the report and its notification are kept even if the retrain propagation fails, and `_trigger_group_retrain` commits on
    # its own.
    db.commit()

    result = {"reportId": str(report.id), "breachedFloor": breached, "missingMetrics": missing}
    if breached:
        _group_retrain_step(db, sub.model_id, result)
    return result


def _group_retrain_step(db: Session, model_id: uuid.UUID, result: dict) -> None:
    """Runs the group-retrain step of a breached MLMF report, after the report is committed, and writes its outcome into `result` (the route's answer). Never raises.

    For a model in a coordination group of the registry it sets `groupRetrainTriggered` (the group's `retrainPropagation` decides, LLD sections 4.3 and 4.4) and, when it fired,
    `retrainedModelIds` and `retrainSkipped` (call flow 02e); a model in no group is not retrained by a breach. Any failure of the step (NFO not answering, a registry read failing, a
    reserved or unknown `retrainPropagation`, anything else) rolls back what the step had not committed, is logged at WARNING and is answered as
    `groupRetrainError` `{code, detail}` (`code` UPSTREAM_FAILED for a transport error, INTERNAL_ERROR otherwise), beside the members already started.
    It never raises because the report is stored: a 500 here released the Idempotency-Key, and a repeat stored a second report (MLMF-REPORT-REPLAY).
    """
    started: list[uuid.UUID] = []
    skipped: list[dict] = []
    try:
        group = _find_coordination_group_for_model(db, model_id)
        if group is None:
            return
        triggered = should_trigger_group_retrain(group["retrainPropagation"], member_count=len(group["memberModelIds"]), breached_count=1)
        result["groupRetrainTriggered"] = triggered
        if triggered:
            _trigger_group_retrain(db, group, started, skipped)
    # Deliberately broad: whatever fails after the report's commit must not turn its answer into a 500 (see the docstring).
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        code = FrameworkError.UPSTREAM_FAILED[0] if isinstance(exc, httpx.HTTPError) else FrameworkError.INTERNAL_ERROR[0]
        logger.warning("group retrain after the MLMF report of model %s failed: %s %s: %s", model_id, code, type(exc).__name__, exc)
        result["groupRetrainError"] = {"code": code, "detail": f"{type(exc).__name__}: {exc}"}
    if result.get("groupRetrainTriggered"):
        result["retrainedModelIds"] = [str(mid) for mid in started]
        result["retrainSkipped"] = skipped


@router.get("/mlmf/subscriptions")
def list_performance_subscriptions(request: Request, model_id: uuid.UUID | None = None, limit: int = PageLimit, offset: int = PageOffset,
                                    db: Session = Depends(get_session)):
    # Route notes: paginated; `model_id` filters in SQL. Answers each subscription with its floor and destination. For a caller with the rApp role (SEC-15.3c): naming a model
    # another rApp owns is 403 `MODEL_ACCESS_DENIED` (a model the registry does not know gives the empty list as before), and without `model_id` only the subscriptions of the models it may see
    # are listed (`_visible_model_ids`: owned, owner-less or unknown to the registry). The console, an SMO module (MDAF reads its subscriptions this way) and a call with no role see all.
    stmt = select(MLMFSubscription)
    if model_id:
        _check_rapp_owns_if_known(db, request, model_id, "list the subscriptions to the performance of")
        stmt = stmt.where(MLMFSubscription.model_id == model_id)
    else:
        visible = _visible_model_ids(request, db)
        if visible is not None:
            stmt = stmt.where(MLMFSubscription.model_id.in_(visible))
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"subscriptionId": str(sub.subscription_id), "modelId": str(sub.model_id), "metricTypes": sub.metric_types,
             "dmeTypeId": str(sub.dme_type_id), "guardKpiFloor": sub.guard_kpi_floor, "guardKpiCeiling": sub.guard_kpi_ceiling,
             "notificationDestination": sub.notification_destination} for sub in page["items"]]}


@router.get("/mlmf/subscriptions/{subscription_id}/reports")
def list_performance_reports(request: Request, subscription_id: uuid.UUID, limit: int = PageLimit, offset: int = PageOffset,
                              db: Session = Depends(get_session)):
    # Route notes: 404 `MLMF_SUBSCRIPTION_NOT_FOUND` for an unknown subscription; 403 `MODEL_ACCESS_DENIED` for an rApp that does not own the subscribed model (SEC-15.3c); newest first.
    sub = db.get(MLMFSubscription, subscription_id)
    if sub is None:
        raise framework_error(FrameworkError.MLMF_SUBSCRIPTION_NOT_FOUND, detail="no such MLMF subscription")
    _check_rapp_owns_if_known(db, request, sub.model_id, "read the performance reports of")
    stmt = select(PerformanceReport).where(PerformanceReport.subscription_id == subscription_id).order_by(PerformanceReport.reported_at.desc())
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_performance_report_view(r) for r in page["items"]]}


@router.get("/mlmf/reports")
def list_recent_performance_reports(request: Request, breached_only: bool = False, limit: int = PageLimit, offset: int = PageOffset,
                                     db: Session = Depends(get_session)):
    # Route notes: reports across all subscriptions, newest first; `breached_only` keeps the ones with a breach. Paginated. For a caller with the rApp role only the reports of
    # subscriptions on the models it may see are listed (SEC-15.3c, `_visible_model_ids`).
    stmt = select(PerformanceReport)
    visible = _visible_model_ids(request, db)
    if visible is not None:
        stmt = stmt.join(MLMFSubscription, MLMFSubscription.subscription_id == PerformanceReport.subscription_id).where(MLMFSubscription.model_id.in_(visible))
    if breached_only:
        stmt = stmt.where(PerformanceReport.breached_floor.is_(True))
    page = paginate(db, stmt.order_by(PerformanceReport.reported_at.desc()), limit, offset)
    return {**page, "items": [_performance_report_view(r) for r in page["items"]]}


def _trigger_group_retrain(db: Session, group: dict, retrained_model_ids: list[uuid.UUID], skipped: list[dict]) -> None:
    """Starts a RE_TRAINING run for every PROMOTED member of the coordination group; appends each member it started to `retrained_model_ids` and each it could not
    start to `skipped` as it goes, so the caller keeps them when a later member raises.

    `group` is the registry's group object (`memberModelIds`). A member the registry no longer knows (`_get_model_or_none`) or that is not PROMOTED (already TRAINING from an earlier trigger, mid-certification, never
    certified) is passed over, never forced: a group retrain is a PROMOTED model's retrain, not a first cycle or a recovery. Each run goes through `_start_training` (the same lifecycle gate, NFO runtime and
    MLTrainingProcess as a requested retrain) with producer `aimgf:group-retrain`. Best effort per member (call flow 02e, DOC-CF-1.6): each started run is committed on its own, and a member whose start
    is refused (NFO refuses or does not answer its runtime, 502/503) is rolled back alone and listed as `{modelId, reason}` in `skipped`, so one member never stops the others.
    Anything else (a failing read for a member) raises, and `_group_retrain_step` answers it.
    """
    for raw_member_id in group["memberModelIds"]:
        member_id = uuid.UUID(raw_member_id)
        if _get_model_or_none(db, member_id) is None:
            continue
        lifecycle = _get_or_create_lifecycle(db, member_id)
        if lifecycle.model_lifecycle_state != ModelLifecycleState.PROMOTED:
            continue
        # HISTORY.md OI-6.2 / Wave 4: the same start path (NFO
        # runtime, MLTrainingProcess, CREATE_TRAINING) a directly-requested
        # retrain gets via request_training.
        try:
            _start_training(db, model_id=member_id, group_id=None, producer_id="aimgf:group-retrain",
                            ml_training_type="RE_TRAINING")
        except HTTPException as exc:
            db.rollback()
            reason = exc.detail.get("title") if isinstance(exc.detail, dict) else str(exc.detail)
            logger.warning("group retrain: member %s not started (%s)", member_id, reason)
            skipped.append({"modelId": str(member_id), "reason": reason})
            continue
        db.commit()
        retrained_model_ids.append(member_id)


def _performance_report_view(r: PerformanceReport) -> dict:
    """Returns a PerformanceReport as the dict the MLMF report routes answer."""
    return {"reportId": str(r.id), "subscriptionId": str(r.subscription_id), "metrics": r.metrics,
            "breachedFloor": r.breached_floor, "missingMetrics": r.missing_metrics or [], "reportedAt": r.reported_at.isoformat()}
