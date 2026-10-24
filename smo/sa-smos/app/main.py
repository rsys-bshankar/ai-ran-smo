"""SA SMOS (Service Assurance SMOS): assurance monitors, threshold evaluation and remedial actions.

What it is: the FastAPI app of the `sa-smos` module (R1 route `/sa-smos` via R1 Termination). A monitor holds requirement thresholds and at most one target. A monitor that names an MDAF
analytics type is subscribed to it at registration: MDAF pushes each report of that type here (`POST /monitors/{id}/reports`), the report is read back from MDAF and evaluated, and a new
breach runs the monitor's automatic action (set by an admin) or is recorded ESCALATED for the operator (call flow 04b). An operator can also evaluate metrics and run a remedial action by hand;
each action is dispatched to the module that can perform it and recorded RESOLVED or ESCALATED with who asked, why and what came of it (04c, 04d). The generic O1-CM intent handler is a
second, independent role in `o1cm.py` and is mounted at the end of this file.

Where it sits: called by the GUI BFF and by MDAF's notification outbox; calls MDAF (subscribe, read a report), SO SMOS (to resolve a deployment), NFO (heal), rApp Management (rollback,
versions), AIMgF (retrain) and RAN NF OAM (config job) through `R1Client`. Design: SMO Design v1.3 section 3.14 and SO/SA SMOS LLD section 2; README sections 1.5 and 2; call flow 04.

Dispatch is chosen by the monitor's target first, then by the action type (see `_dispatch`): a coordination-group monitor always retrains the group (recorded RETRAIN); ROLLBACK needs a
rApp-instance target, because only rApp Management keeps a version history (OI-1-sa-rollback); RECONNECT means healing the monitor's NFO workload, resolved from the rApp instance or from
the order's completed DEPLOY step; CONFIG_CHANGE writes the monitor's remedy change list; SCALE always escalates (NFO scaling takes no target size, OI-7-nfo-scale-size).

What it owns: the `assurance_monitor` and `remedial_action` tables (`models.py`). One remedial action per monitor runs at a time (the monitor row is locked). It does not own orders,
deployments, rApp versions, reports or intents.
"""

import datetime
import os
import uuid

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from smo_shared import mtls
from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error, problem
from smo_shared.r1_client import R1Client
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id
from smo_shared.pagination import PageLimit, PageOffset, paginate

from .models import AssuranceMonitor, RemedialAction

app = FastAPI(title="SA SMOS")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
apply_r1_gateway_security(app)
apply_correlation_id(app)


install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)

# The address MDAF's outbox posts a monitor's reports to (`<base>/monitors/{id}/reports`); https under mTLS. SA_SMOS_REPORTS_URL overrides the base.
REPORTS_URL_BASE = mtls.http_url(os.environ.get("SA_SMOS_REPORTS_URL", "http://sa-smos:8000")).rstrip("/")
ACTION_TYPES = ("CONFIG_CHANGE", "SCALE", "RECONNECT", "ROLLBACK", "RETRAIN")   # what an operator may ask for
AUTO_ACTIONS = ("CONFIG_CHANGE", "RECONNECT", "ROLLBACK", "RETRAIN")              # what a breach may run by itself (SCALE only ever escalates)
LOOP = "sa-smos"                                                                  # `requested_by` of what the loop does by itself
# Fixed namespace of the uuid5 that derives a report-driven remedial action's id from its monitor, report and action type (SA-REPLAY). The id names the action's
# Idempotency-Key downstream, so changing this constant makes a report re-delivered across the change run its action again.
REPORT_ACTION_NAMESPACE = uuid.UUID("3c7e5d2a-9b14-5f6e-a8c1-2d4b6f8a0e17")


def _now() -> datetime.datetime:
    """The current time, UTC."""
    return datetime.datetime.now(datetime.UTC)


def _is_number(value) -> bool:
    """True for an int or float that is not a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_thresholds(thresholds: dict) -> None:
    """422 SCHEMA_VALIDATION_FAILED unless every threshold is a number (a minimum) or an object with a numeric `min` and/or `max` (min not above max)."""
    for metric, bound in thresholds.items():
        if _is_number(bound):
            continue
        if (isinstance(bound, dict) and bound and set(bound) <= {"min", "max"} and all(_is_number(v) for v in bound.values())
                and bound.get("min", float("-inf")) <= bound.get("max", float("inf"))):
            continue
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                              detail=f"threshold {metric!r} must be a number (a minimum) or {{\"min\": n, \"max\": n}}, not {bound!r}")


def _evaluate(thresholds: dict, metrics: dict) -> tuple[dict, list[str]]:
    """(breaches, missing): each metric whose value is below its minimum or above its maximum, mapped to its threshold, and the metrics with no numeric value.
    A missing metric is reported, never a breach: no data is not bad data (as for MLMF, call flow 13)."""
    breaches, missing = {}, []
    for metric, bound in thresholds.items():
        value = metrics.get(metric)
        if not _is_number(value):
            missing.append(metric)
            continue
        low, high = (bound, None) if _is_number(bound) else (bound.get("min"), bound.get("max"))
        if (low is not None and value < low) or (high is not None and value > high):
            breaches[metric] = bound
    return breaches, missing


def _monitor(db: Session, monitor_id: uuid.UUID) -> AssuranceMonitor:
    """The monitor, or 404 ASSURANCE_MONITOR_NOT_FOUND."""
    monitor = db.get(AssuranceMonitor, monitor_id)
    if monitor is None:
        raise framework_error(FrameworkError.ASSURANCE_MONITOR_NOT_FOUND, detail="no such AssuranceMonitor")
    return monitor


def _lock_monitor(db: Session, monitor_id: uuid.UUID) -> AssuranceMonitor:
    """The monitor, locked for this transaction (`SELECT ... FOR UPDATE NOWAIT` on Postgres): one remedial action, and one pushed report, per monitor at a time.
    409 REMEDIAL_ACTION_IN_PROGRESS at once when another holds it; 404 as `_monitor`. SQLite has no row locks."""
    try:
        monitor = db.get(AssuranceMonitor, monitor_id, with_for_update={"nowait": True}, populate_existing=True)
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":       # lock_not_available; anything else is not ours to answer
            raise
        db.rollback()
        raise framework_error(FrameworkError.REMEDIAL_ACTION_IN_PROGRESS,
                              detail=f"a remedial action of monitor {monitor_id} is running; try again when it has ended") from exc
    if monitor is None:
        raise framework_error(FrameworkError.ASSURANCE_MONITOR_NOT_FOUND, detail="no such AssuranceMonitor")
    return monitor


@app.post("/monitors", status_code=201)
def register_assurance_monitor(target_order_id: uuid.UUID | None = None, target_coordination_group_id: uuid.UUID | None = None,
                                target_rapp_instance_id: uuid.UUID | None = None, analytics_subscription_id: uuid.UUID | None = None,
                                analytics_type: str | None = None, thresholds: dict | None = None, db: Session = Depends(get_session)):
    """At most one target: a service order (NF deployment remediation), a
    model coordination group (retrain), or a rApp instance (remediation
    through rApp Management, including ROLLBACK). With `analytics_type`,
    SA SMOS subscribes to that MDAF analytics type and evaluates every
    report MDAF pushes (call flow 04a)."""
    # Maintainer notes (not published). Takes the targets, `analytics_subscription_id` and `analytics_type` as query parameters and the thresholds as the JSON body ({metric: minimum} or
    # {metric: {min, max}}). More than one target is a 422 COORDINATION_GROUP_MISMATCH (the code is reused; the database CHECK `one_target_only` is the backstop); a bad threshold, or both
    # `analytics_type` and `analytics_subscription_id`, a 422 SCHEMA_VALIDATION_FAILED. No target is allowed. The target ids are not checked against the modules that own them.
    # With `analytics_type` the subscription is made at MDAF before the monitor is committed; MDAF refusing it or not answering is 502 UPSTREAM_FAILED and nothing is stored.
    targets = [t for t in (target_order_id, target_coordination_group_id, target_rapp_instance_id) if t is not None]
    if len(targets) > 1:
        raise framework_error(FrameworkError.COORDINATION_GROUP_MISMATCH,
                              detail="at most one of targetOrderId/targetCoordinationGroupId/targetRappInstanceId")
    _check_thresholds(thresholds or {})
    if analytics_type is not None and analytics_subscription_id is not None:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                              detail="give analytics_type (SA SMOS subscribes at MDAF) or analytics_subscription_id (a reference), not both")
    monitor = AssuranceMonitor(monitor_id=uuid.uuid4(), target_order_id=target_order_id, target_coordination_group_id=target_coordination_group_id,
                                target_rapp_instance_id=target_rapp_instance_id, analytics_subscription_id=analytics_subscription_id,
                                analytics_type=analytics_type, requirement_thresholds=thresholds or {})
    if analytics_type is not None:
        monitor.analytics_subscription_id = _subscribe(monitor.monitor_id, analytics_type)
    db.add(monitor)
    db.commit()
    return {"monitorId": str(monitor.monitor_id), "analyticsSubscriptionId": str(monitor.analytics_subscription_id) if monitor.analytics_subscription_id else None}


def _subscribe(monitor_id: uuid.UUID, analytics_type: str) -> uuid.UUID:
    """Subscribes the monitor to every MDAF report of `analytics_type`, MDAF to push each one to `<REPORTS_URL_BASE>/monitors/{id}/reports`; returns the subscription id.
    502 UPSTREAM_FAILED when MDAF refuses or does not answer."""
    try:
        resp = R1Client().post("/mdaf/subscriptions", params={"analytics_type": analytics_type, "requested_by": f"{LOOP}:monitor/{monitor_id}"},
                               json={"notificationDestination": f"{REPORTS_URL_BASE}/monitors/{monitor_id}/reports"})
    except httpx.HTTPError as exc:
        raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"MDAF did not answer the subscription ({type(exc).__name__})") from exc
    if resp.status_code >= 300:
        raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"MDAF refused the subscription: {_problem_detail(resp)}")
    return uuid.UUID(resp.json()["subscriptionId"])


class RemedyBody(BaseModel):
    """Body of `PUT /monitors/{id}/remedy`: the change list CONFIG_CHANGE sends to RAN NF OAM (`POST /config-jobs` `changes`); null or empty clears it."""
    changes: list[dict] | None = None


@app.put("/monitors/{monitor_id}/remedy")
def set_remedy(monitor_id: uuid.UUID, body: RemedyBody, db: Session = Depends(get_session)):
    """Sets the configuration a CONFIG_CHANGE action writes for this monitor (call flow 04c). Each change names its element (`managedElementRef`); RAN NF OAM checks the rest when it is sent."""
    # Maintainer notes (not published). 404; 422 SCHEMA_VALIDATION_FAILED for a change that is not an object naming its managedElementRef, or for clearing the remedy while the automatic
    # action is CONFIG_CHANGE (it would have nothing to write). Answers the monitor view.
    monitor = _monitor(db, monitor_id)
    changes = body.changes or None
    for change in changes or []:
        if not isinstance(change.get("managedElementRef"), str) or not change["managedElementRef"]:
            raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="every change names its element (managedElementRef)")
    if changes is None and monitor.auto_action == "CONFIG_CHANGE":
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="the automatic action is CONFIG_CHANGE: change it before clearing the remedy")
    monitor.remedy_changes = changes
    db.commit()
    return _monitor_view(monitor)


class AutomationBody(BaseModel):
    """Body of `PUT /monitors/{id}/automation`: the action a breach runs by itself, or null for none (a breach is then escalated to the operator)."""
    autoAction: str | None = None


@app.put("/monitors/{monitor_id}/automation")
def set_automation(monitor_id: uuid.UUID, body: AutomationBody, db: Session = Depends(get_session)):
    """Sets what a breach found in a pushed report runs by itself (call flow 04b). Admin-only in the console. The action must suit the target: RETRAIN, and only RETRAIN, for a
    coordination-group monitor; ROLLBACK for a rApp-instance monitor; RECONNECT for an order or rApp-instance monitor; CONFIG_CHANGE once a remedy is set."""
    # Maintainer notes (not published). 404; 422 SCHEMA_VALIDATION_FAILED for an action outside AUTO_ACTIONS or one the target cannot take. Answers the monitor view.
    monitor = _monitor(db, monitor_id)
    action = body.autoAction
    if action is not None:
        group = monitor.target_coordination_group_id is not None
        why = (f"{action} is not an automatic action (one of {', '.join(AUTO_ACTIONS)})" if action not in AUTO_ACTIONS
               else "a coordination-group monitor retrains (RETRAIN)" if group and action != "RETRAIN"
               else "RETRAIN needs a coordination-group monitor" if action == "RETRAIN" and not group
               else "ROLLBACK needs a rApp-instance monitor" if action == "ROLLBACK" and monitor.target_rapp_instance_id is None
               else "RECONNECT needs an order or rApp-instance monitor" if action == "RECONNECT" and monitor.target_order_id is None and monitor.target_rapp_instance_id is None
               else "CONFIG_CHANGE needs a remedy (PUT /monitors/{id}/remedy)" if action == "CONFIG_CHANGE" and not monitor.remedy_changes
               else None)
        if why:
            raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=why)
    monitor.auto_action = action
    db.commit()
    return _monitor_view(monitor)


@app.post("/monitors/{monitor_id}/evaluate")
def evaluate_thresholds(monitor_id: uuid.UUID, current_metrics: dict, db: Session = Depends(get_session)):
    """Compares the metrics given with the monitor's thresholds (call flow 04b, by hand): each metric below its minimum or above its maximum is a breach, a metric not given (or
    not a number) is listed as missing, never a breach. Nothing is stored or run.
    """
    # Maintainer notes (not published). 404 ASSURANCE_MONITOR_NOT_FOUND for an unknown monitor. Answers {monitorId, breaches: {metric: threshold}, missing: [metric]}.
    monitor = _monitor(db, monitor_id)
    breaches, missing = _evaluate(monitor.requirement_thresholds, current_metrics)
    return {"monitorId": str(monitor.monitor_id), "breaches": breaches, "missing": missing}


class ReportNotification(BaseModel):
    """What MDAF's outbox posts for a report (`mdaf/app/main.py` `_notify_report_subscribers`). Only `reportId` is trusted: the report itself is read back from MDAF."""
    reportId: uuid.UUID
    analyticsType: str | None = None
    output: dict | None = None
    inputSources: list | None = None


@app.post("/monitors/{monitor_id}/reports")
def receive_report(monitor_id: uuid.UUID, body: ReportNotification, db: Session = Depends(get_session)):
    """A report MDAF pushed for this monitor's analytics type (call flow 04b). The report is read back from MDAF, evaluated, and the evaluation kept as `lastEvaluation`. A breach
    after a report without one (or the first report) runs the monitor's automatic action, or is recorded ESCALATED for the operator when it has none; a breach that persists runs
    nothing more. A report already evaluated is answered as before."""
    # Maintainer notes (not published). Answers 200 {monitorId, reportId, status: EVALUATED | DUPLICATE | IGNORED, breaches, missing, action?}. IGNORED when the monitor watches no analytics
    # type, or another one, or MDAF no longer has the report (404). 503 when another remedial action of the monitor runs or MDAF cannot be read, so the outbox sends it again (it retries a
    # 5xx, not a 4xx); 404 for an unknown monitor (not retried). The evaluation and any action commit together.
    try:
        monitor = _lock_monitor(db, monitor_id)
    except HTTPException as exc:
        if exc.status_code == 409:
            raise problem(503, "REMEDIAL_ACTION_IN_PROGRESS", "a remedial action of this monitor is running; the report is taken when it has ended") from exc
        raise
    answer: dict[str, object] = {"monitorId": str(monitor_id), "reportId": str(body.reportId)}
    last = monitor.last_evaluation or {}
    if monitor.analytics_type is None or (body.analyticsType is not None and body.analyticsType != monitor.analytics_type):
        return {**answer, "status": "IGNORED", "detail": f"the monitor watches {monitor.analytics_type or 'no analytics type'}"}
    if last.get("reportId") == str(body.reportId):
        return {**answer, "status": "DUPLICATE", "breaches": last.get("breaches", {}), "missing": last.get("missing", [])}
    try:
        resp = R1Client().get(f"/mdaf/reports/{body.reportId}")
    except httpx.HTTPError as exc:
        raise problem(503, "UPSTREAM_FAILED", f"MDAF did not answer the report read ({type(exc).__name__}); send it again") from exc
    if resp.status_code == 404:
        return {**answer, "status": "IGNORED", "detail": "MDAF has no such report"}
    if resp.status_code != 200:
        raise problem(503, "UPSTREAM_FAILED", f"MDAF answered {resp.status_code} to the report read; send it again")
    report = resp.json()
    if report.get("analyticsType") != monitor.analytics_type:
        return {**answer, "status": "IGNORED", "detail": f"the report is of {report.get('analyticsType')}, the monitor watches {monitor.analytics_type}"}
    breaches, missing = _evaluate(monitor.requirement_thresholds, report.get("output") or {})
    was_breached = bool(last.get("breached"))
    monitor.last_evaluation = {"at": _now().isoformat(), "reportId": str(body.reportId), "breached": bool(breaches), "breaches": breaches, "missing": missing}
    answer.update({"status": "EVALUATED", "breaches": breaches, "missing": missing})
    if breaches and not was_breached:
        found = "breach of " + ", ".join(sorted(breaches)) + f" in report {body.reportId}"
        if monitor.auto_action:
            action = _run(db, monitor, monitor.auto_action, requested_by=LOOP, auto=True, report_id=body.reportId, why=found)
        else:
            action = _record(db, monitor, "ESCALATE", "ESCALATED", requested_by=LOOP, report_id=body.reportId, detail=f"{found}; the monitor has no automatic action")
        answer["action"] = _action_view(action)
    db.commit()
    return answer


@app.post("/monitors/{monitor_id}/remedial-actions", status_code=201)
def execute_remedial_action(monitor_id: uuid.UUID, action_type: str, requested_by: str | None = None, requester_is_admin: bool = False,
                            db: Session = Depends(get_session)):
    """ExecuteRemedialAction by hand (call flow 04c). Dispatch per SO/SA SMOS
    LLD section 2.1: CONFIG_CHANGE writes the monitor's remedy, SCALE
    escalates, RECONNECT heals, ROLLBACK is resolved for a
    rApp-instance-scoped monitor and refused (409
    ROLLBACK_HISTORY_UNAVAILABLE) for any other; a coordination-group
    monitor retrains whatever is asked (recorded RETRAIN).

    A refusal or no answer from the module that acts is not an error here:
    the action is recorded ESCALATED with the reason in `detail`. A RESOLVED
    rollback has started an upgrade back; `result` names the replacement
    instance, which rApp Management resolves like any upgrade.
    """
    # Maintainer notes (not published). Answers 201 with the action view; 404 ASSURANCE_MONITOR_NOT_FOUND, 409 ROLLBACK_HISTORY_UNAVAILABLE, 409 REMEDIAL_ACTION_IN_PROGRESS (the monitor is
    # locked by another action or report), 422 COORDINATION_GROUP_MISMATCH (reused) for an unknown action type or RETRAIN on a monitor without a group. `requested_by` is who asked (the console
    # pins it to `smo-gui:<user>`); `requester_is_admin` is accepted and no longer stored (it was written as `auto_executed`, which now means "run by a report").
    if action_type not in ACTION_TYPES:
        raise framework_error(FrameworkError.COORDINATION_GROUP_MISMATCH, detail=f"unknown actionType {action_type}")
    monitor = _lock_monitor(db, monitor_id)
    if action_type == "RETRAIN" and monitor.target_coordination_group_id is None:
        raise framework_error(FrameworkError.COORDINATION_GROUP_MISMATCH, detail="RETRAIN needs a coordination-group monitor")
    if action_type == "ROLLBACK" and monitor.target_coordination_group_id is None and monitor.target_rapp_instance_id is None:
        raise framework_error(
            FrameworkError.ROLLBACK_HISTORY_UNAVAILABLE,
            detail="ROLLBACK needs a rApp-instance-scoped monitor (targetRappInstanceId): only rApp Management "
                   "keeps a version history; NFO keeps none for a bare NF deployment.",
        )
    action = _run(db, monitor, action_type, requested_by=requested_by, auto=False)
    db.commit()
    return _action_view(action)


def _run(db: Session, monitor: AssuranceMonitor, action_type: str, *, requested_by: str | None, auto: bool,
         report_id: uuid.UUID | None = None, why: str | None = None) -> RemedialAction:
    """Dispatches `action_type` for the locked monitor and adds its RemedialAction (not committed); returns it. A transport error is an ESCALATED action whose outcome downstream is
    unknown.

    A report-driven action (`report_id` given) has the id uuid5(REPORT_ACTION_NAMESPACE, "<monitor>:<report>:<action type>"), so a report MDAF re-delivers after a crash before
    the commit sends the same Idempotency-Key and the module that acts replays its first answer (SA-REPLAY); one already recorded is returned and nothing is sent. A manual
    action gets a new uuid4, and so a new key, every time: asking again is a new request.
    """
    action_id = uuid.uuid5(REPORT_ACTION_NAMESPACE, f"{monitor.monitor_id}:{report_id}:{action_type}") if report_id else uuid.uuid4()
    recorded = db.get(RemedialAction, action_id)
    if recorded is not None:
        return recorded
    try:
        kind, outcome, result, detail = _dispatch(R1Client(), monitor, action_type, action_id)
    except httpx.HTTPError as exc:
        kind = "RETRAIN" if monitor.target_coordination_group_id is not None else action_type
        outcome, result, detail = "ESCALATED", None, f"no answer from the module that acts ({type(exc).__name__}): its outcome is unknown"
    if why:
        detail = f"{why}: {detail}" if detail else why
    return _record(db, monitor, kind, outcome, requested_by=requested_by, auto=auto, report_id=report_id, detail=detail, result=result, action_id=action_id)


def _record(db: Session, monitor: AssuranceMonitor, action_type: str, outcome: str, *, requested_by: str | None, auto: bool = False,
            report_id: uuid.UUID | None = None, detail: str | None = None, result: dict | None = None, action_id: uuid.UUID | None = None) -> RemedialAction:
    """Adds one RemedialAction for the monitor (not committed)."""
    action = RemedialAction(action_id=action_id or uuid.uuid4(), monitor_id=monitor.monitor_id, action_type=action_type, auto_executed=auto, outcome=outcome,
                            created_at=_now(), requested_by=requested_by, detail=detail, result=result, report_id=report_id)
    db.add(action)
    db.flush()
    return action


def _dispatch(r1: R1Client, monitor: AssuranceMonitor, action_type: str, action_id: uuid.UUID) -> tuple[str, str, dict | None, str | None]:
    """(action type recorded, outcome, result, detail) of one remedial action, decided by the monitor's target first, then by the action type (see the module docstring).
    May raise httpx.HTTPError (no answer), which `_run` records. Every POST carries `Idempotency-Key: sa-smos-action:<action_id>`; each target is `@idempotent`.
    """
    key = {"Idempotency-Key": f"sa-smos-action:{action_id}"}
    # A coordination-group monitor retrains whatever action type was asked for: the NF-oriented actions below have no meaning for a model group.
    if monitor.target_coordination_group_id is not None:
        resp = r1.post("/aimgf/training-jobs", json={"modelCoordinationGroupId": str(monitor.target_coordination_group_id), "producerId": LOOP}, headers=key)
        if resp.status_code < 300:
            return "RETRAIN", "RESOLVED", _json(resp), None
        return "RETRAIN", "ESCALATED", None, f"AIMgF refused the retrain: {_problem_detail(resp)}"
    if action_type == "CONFIG_CHANGE":
        if not monitor.remedy_changes:
            return action_type, "ESCALATED", None, "no remedy is set for this monitor (PUT /monitors/{id}/remedy): nothing to write"
        resp = r1.post("/ran-nf-oam/config-jobs", json={"requestedBy": LOOP, "scope": "cell", "changes": monitor.remedy_changes}, headers=key)
        if resp.status_code >= 300:
            return action_type, "ESCALATED", None, f"RAN NF OAM refused the write: {_problem_detail(resp)}"
        job = _json(resp) or {}
        result = {k: job.get(k) for k in ("jobId", "status", "approvalId") if job.get(k) is not None}
        if job.get("status") == "COMPLETED":
            return action_type, "RESOLVED", result, None
        if job.get("status") == "PENDING_APPROVAL":
            return action_type, "ESCALATED", result, f"the write is held for approval {job.get('approvalId')}"
        return action_type, "ESCALATED", result, f"config job {job.get('jobId')} ended {job.get('status')}"
    if action_type == "SCALE":
        return action_type, "ESCALATED", None, "NFO scaling takes no target size (OI-7-nfo-scale-size): an operator scales"
    if action_type == "ROLLBACK":
        resp = r1.post(f"/rapp-mgmt/instances/{monitor.target_rapp_instance_id}/rollback", headers=key)
        if resp.status_code < 300:
            return action_type, "RESOLVED", _json(resp), None
        return action_type, "ESCALATED", None, _problem_detail(resp)
    # RECONNECT: the rApp instance's workload, else the order's DEPLOY step
    if monitor.target_rapp_instance_id is not None:
        nf_deployment_id, source = _resolve_rapp_workload(r1, monitor.target_rapp_instance_id), "the rApp instance has no workload"
    else:
        nf_deployment_id, source = _resolve_deployed_nf(r1, monitor.target_order_id), "no completed DEPLOY step of the monitor's order"
    if nf_deployment_id is None:
        return action_type, "ESCALATED", None, f"nothing to heal: {source}"
    heal = r1.post(f"/nfo/deployments/{nf_deployment_id}/heal", headers=key)
    if heal.status_code < 300:
        return action_type, "RESOLVED", {"nfDeploymentId": nf_deployment_id}, None
    return action_type, "ESCALATED", {"nfDeploymentId": nf_deployment_id}, f"NFO refused the heal: {_problem_detail(heal)}"


def _json(resp) -> dict | None:
    """The response's JSON object, or None when it has none."""
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@app.post("/monitors/{monitor_id}/escalate")
def escalate_to_operator(monitor_id: uuid.UUID, reason: str, requested_by: str | None = None, db: Session = Depends(get_session)):
    """Puts the monitor in the operator's escalation queue with nothing dispatched (call flow 04d): an ESCALATE action, ESCALATED, with the reason kept as its `detail`."""
    # Maintainer notes (not published). 404 for an unknown monitor. `requested_by` is pinned by the console. Answers the action view plus `reason`.
    monitor = _monitor(db, monitor_id)
    action = _record(db, monitor, "ESCALATE", "ESCALATED", requested_by=requested_by, detail=reason)
    db.commit()
    return {**_action_view(action), "reason": reason}


def _resolve_deployed_nf(r1: R1Client, target_order_id: uuid.UUID | None) -> str | None:
    """Returns the NFO deployment id of an order's completed DEPLOY step, or None when it cannot be resolved.

    The monitor stores only `target_order_id`, so the order is read back from SO SMOS (`GET /so-smos/orders/{id}`) and its first DEPLOY step with status COMPLETED supplies
    `result.nfDeploymentId` (the shape `so-smos/app/dispatch.py` records). None for no order, an order SO SMOS does not answer 200 for, or an order without such a step; the caller then escalates.
    """
    if target_order_id is None:
        return None
    resp = r1.get(f"/so-smos/orders/{target_order_id}")
    if resp.status_code != 200:
        return None
    for step in resp.json().get("steps", []):
        if step.get("stepType") == "DEPLOY" and step.get("status") == "COMPLETED":
            return step.get("result", {}).get("nfDeploymentId")
    return None


def _resolve_rapp_workload(r1: R1Client, instance_id: uuid.UUID) -> str | None:
    """Returns the NFO deployment (`workloadRef`) of the rApp instance a monitor targets, or of the instance that replaced it through an upgrade; None unless rApp Management answers 200.

    rApp Management's version history (`GET /rapp-mgmt/instances/{id}/versions`) does the resolving, so a monitor keeps working after an upgrade changes the instance id.
    """
    resp = r1.get(f"/rapp-mgmt/instances/{instance_id}/versions")
    if resp.status_code != 200:
        return None
    return resp.json().get("workloadRef")


def _problem_detail(resp) -> str:
    """The text recorded as `detail` for a refused downstream call: 'TITLE: detail' when the answer carries a problem object, the plain `detail` otherwise, `HTTP <status>` when the body is not JSON.
    """
    try:
        body = resp.json().get("detail")
    except (ValueError, AttributeError):
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        return f"{body.get('title')}: {body.get('detail')}"
    return str(body) if body is not None else f"HTTP {resp.status_code}"


@app.get("/monitors")
def list_assurance_monitors(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """List read over AssuranceMonitor — previously write-only, so the
    thresholds being evaluated were invisible to an operator."""
    # Maintainer notes (not published). Paginated by smo_shared.pagination (ordered by primary key, monitor_id); items are `_monitor_view`.
    page = paginate(db, select(AssuranceMonitor), limit, offset)
    return {**page, "items": [_monitor_view(m) for m in page["items"]]}


@app.get("/monitors/{monitor_id}")
def get_assurance_monitor(monitor_id: uuid.UUID, db: Session = Depends(get_session)):
    # Maintainer notes (not published). 404 ASSURANCE_MONITOR_NOT_FOUND for an unknown monitor.
    return _monitor_view(_monitor(db, monitor_id))


@app.get("/remedial-actions")
def list_remedial_actions(monitor_id: uuid.UUID | None = None, outcome: str | None = None, limit: int = PageLimit,
                           offset: int = PageOffset, db: Session = Depends(get_session)):
    """Every RemedialAction row, optionally per-monitor or per-outcome —
    `outcome=ESCALATED` is the operator's escalation queue (the GUI
    dashboard's SA SMOS tile)."""
    # Maintainer notes (not published). `monitor_id` and `outcome` are exact-match filters, combined with AND; `outcome=ESCALATED` is the operator's queue. Paginated (ordered by primary key, action_id).
    stmt = select(RemedialAction)
    if monitor_id:
        stmt = stmt.where(RemedialAction.monitor_id == monitor_id)
    if outcome:
        stmt = stmt.where(RemedialAction.outcome == outcome)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_action_view(a) for a in page["items"]]}


def _iso(value: datetime.datetime | None) -> str | None:
    """A timestamp as ISO 8601, or None."""
    return value.isoformat() if value else None


def _action_view(a: RemedialAction) -> dict:
    """The JSON view of a remedial action: what was done, how it ended and why, who asked, when, and what the module that acted answered."""
    view = {"actionId": str(a.action_id), "monitorId": str(a.monitor_id), "actionType": a.action_type, "autoExecuted": a.auto_executed, "outcome": a.outcome,
            "createdAt": _iso(a.created_at), "requestedBy": a.requested_by, "detail": a.detail, "reportId": str(a.report_id) if a.report_id else None}
    if a.result is not None:
        view["result"] = a.result
    return view


def _monitor_view(m: AssuranceMonitor) -> dict:
    """The JSON view of a monitor: its id, the four optional references as strings or null, the thresholds, and what closes its loop (analytics type, automatic action, remedy, last evaluation)."""
    return {"monitorId": str(m.monitor_id),
            "targetOrderId": str(m.target_order_id) if m.target_order_id else None,
            "targetCoordinationGroupId": str(m.target_coordination_group_id) if m.target_coordination_group_id else None,
            "targetRappInstanceId": str(m.target_rapp_instance_id) if m.target_rapp_instance_id else None,
            "analyticsSubscriptionId": str(m.analytics_subscription_id) if m.analytics_subscription_id else None,
            "thresholds": m.requirement_thresholds, "analyticsType": m.analytics_type, "autoAction": m.auto_action,
            "remedyChanges": m.remedy_changes, "lastEvaluation": m.last_evaluation, "createdAt": _iso(m.created_at)}


# ---------------------------------------------------------------- Wave 8: generic O1-CM intent handler (W8-07)
# Imported at the end of the file and mounted last; o1cm.py has its own router and its own R1 client and does not import this module. E402 is exempted for this placement.
from .o1cm import router as _o1cm_router  # noqa: E402

app.include_router(_o1cm_router)
