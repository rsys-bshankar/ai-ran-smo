"""Life-cycle flows of RAN NF OAM: zero-touch onboarding of a new element (PR-MGT-14) and software campaigns (PR-MGT-15).

Both are opt in. Nothing here runs, and no row is made, until an operator defines an onboarding template or starts a campaign: a registration that finds no
template behaves exactly as before, and a software job started with `POST /software-management-jobs` carries no campaign.

**Onboarding** (`/onboarding-templates`, `/element-onboarding`). A template is the initial configuration of an element type: a list of CM changes (without the
element: it is the new element's own), the software version the element is expected to run, and two switches (`autoApply`, `requireBaseline`). Registering an element
(`POST /o1-adaptor-endpoints`, the discovery of this build) matches it to the most specific enabled template (same entity type; one that names the element's vendor
beats one that does not) and makes an `element_onboarding` row, `OnboardingState`: DISCOVERED, then TEMPLATE_SELECTED (or NO_TEMPLATE). Applying the template is an ordinary
CM config job (`_execute_write`: MSAC, schema check, dispatch, snapshots), made by an operator (`POST /element-onboarding/{ref}/apply`) or, for an `autoApply` template,
when the element first reports in (its first heartbeat). The job's end decides the state: ONBOARDED, or FAILED with the reason, and a major alarm. The software baseline
check compares the version the element reported (at registration, or when applying) with the template's `softwareBaseline`: MATCH, MISMATCH (flagged on the row and as a
warning alarm) or NOT_CHECKED; with `requireBaseline` a mismatch (or an unknown version) stops the apply, otherwise it only flags.

**Software campaigns** (`/software-campaigns`). A campaign is a list of elements (named, or chosen by entity type, vendor, region and tenant), cut into waves of
`waveSize` elements. A wave is one software management job per element (`software_management_job.campaign_id`), started together. When every job of the wave has
ended the health gate runs (the hook of MGT-5.3, `HEALTH_GATES`): a failed job, or more than `gateMaxNewAlarms` new critical or major alarms on the wave's elements since the wave
started, fails it. A failed gate halts the campaign (`onGateFailure` "halt", the operator continues, aborts or rolls back) or undoes what was done ("rollback"). A pause between
waves (`wavePauseSeconds`) is a halt with a time, continued by the sweep (`advance_due`, run by the worker). The jobs are the existing ones, advanced as before by
`POST /software-management-jobs/{id}/advance` (in this build the adaptor's report, MGT-15 does not change that); a job that belongs to a campaign tells its campaign, which
decides what comes next. A rollback is one revert job per completed job (`rollback_of`), all at once or, with `rollbackOrder` "reverse", the last wave first and each earlier
wave when the one after it has ended (MGT-15.7). A campaign made with `jobTimeoutSeconds` has its running jobs failed by the sweep when their wave (or rollback step) is that old
(`expire_jobs`); one made without never is. `GET /software-campaigns/{id}/report` is the outcome per wave and per element.

**Notifications** (`/lifecycle-subscriptions`, MGT-14.7 and MGT-15.6). A subscriber (admin-registered URL, through the transactional outbox) is told `ONBOARDING_FAILED` when an
element's onboarding ends FAILED, `CAMPAIGN_HALTED` when a campaign halts for a failed gate or by an operator (not for the routine pause between waves) and
`CAMPAIGN_ROLLBACK_FAILED` when a rollback ends with a revert job failed. With no subscription nothing is enqueued.
"""

import datetime
import logging
import math
import re
import uuid
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from smo_shared import events
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error, illegal_transition_error
from smo_shared.idempotency import idempotent
from smo_shared.outbox import enqueue
from smo_shared.pagination import PageLimit, PageOffset, paginate
from smo_shared.statemachine import IllegalTransition
from smo_shared.timeutil import as_utc
from smo_shared.webhook import is_safe_webhook_destination

from . import scoping, swm_rpc
from .netconf_client import EditResult
from .ldn import check_ref
from .models import Alarm, ElementOnboarding, LifecycleSubscription, ManagedEntity, OnboardingTemplate, SoftwareCampaign, SoftwareManagementJob, WriteConfigSubChange
from .statemachine import (
    CAMPAIGN_FSM,
    ONBOARDING_FSM,
    SOFTWARE_MANAGEMENT_FSM,
    CampaignEvent,
    CampaignState,
    OnboardingEvent,
    OnboardingState,
    SwmEvent,
    SwmState,
)
from .vendors import effective_services

router = APIRouter()
log = logging.getLogger("ran-nf-oam")

# Codes of this module. Local to it (not in `smo_shared.errors.FrameworkError`) so the shared library, whose error module is under mutation testing, is not touched.
ONBOARDING_TEMPLATE_NOT_FOUND = ("ONBOARDING_TEMPLATE_NOT_FOUND", 404)
ELEMENT_ONBOARDING_NOT_FOUND = ("ELEMENT_ONBOARDING_NOT_FOUND", 404)
SOFTWARE_CAMPAIGN_NOT_FOUND = ("SOFTWARE_CAMPAIGN_NOT_FOUND", 404)
LIFECYCLE_SUBSCRIPTION_NOT_FOUND = ("LIFECYCLE_SUBSCRIPTION_NOT_FOUND", 404)

MAX_CAMPAIGN_ELEMENTS = 5000
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _stamp(moment: datetime.datetime | None) -> str | None:
    return as_utc(moment).isoformat() if moment is not None else None


def start_software_job(db: Session, managed_element_ref: str, ru_instance_id: str | None = None, **campaign: Any) -> SoftwareManagementJob:
    """A software management job, PENDING and started (IN_PROGRESS, phase DOWNLOAD): what `POST /software-management-jobs` does, and what each element of a campaign wave gets.
    Nothing is sent here: the DOWNLOAD RPC goes out after the caller commits (`dispatch_due`, call flow 19b)."""
    job = SoftwareManagementJob(managed_element_ref=managed_element_ref, ru_instance_id=ru_instance_id, status="PENDING", phase="DOWNLOAD", **campaign)
    db.add(job)
    db.flush()
    job_event(db, job, SwmEvent.START)
    return job


_NEXT_PHASE = {SwmEvent.DOWNLOAD_OK: "INSTALL", SwmEvent.INSTALL_OK: "ACTIVATE"}


def job_event(db: Session, job: SoftwareManagementJob, event: SwmEvent, reason: str | None = None) -> None:
    """Every status change of a software job goes through here (call flow 19): the state machine, the next phase after a phase's OK, the reason a job failed,
    the element's recorded software version when a job that names one completes, and `software-job.state-changed` on the event feed. Raises IllegalTransition
    for a job that has ended. The caller commits."""
    before = job.status
    job.status = SOFTWARE_MANAGEMENT_FSM.fire(SwmState(before), event)
    if event in _NEXT_PHASE:
        job.phase = _NEXT_PHASE[event]
    if event == SwmEvent.PHASE_FAILED:
        job.failure_reason = (reason or "the element reported the phase failed")[:300]
    if job.status == SwmState.COMPLETED.value and job.software_version and job.rollback_of is None:
        onboarding = db.get(ElementOnboarding, job.managed_element_ref)
        if onboarding is not None:
            onboarding.software_version = job.software_version
    if job.status != before:
        events.publish(db, "software-job", "state-changed", "/smo/ran-nf-oam", str(job.job_id),
                       {"managedElementRef": job.managed_element_ref, "from": before, "to": job.status, "phase": job.phase, "reason": job.failure_reason,
                        "campaignId": str(job.campaign_id) if job.campaign_id else None})


def dispatch_due(db: Session, limit: int = 50) -> int:
    """Sends the RPC of the current phase of every running software job whose phase has not been sent yet (call flow 19b): after the commit that started a
    job or moved its phase on, and by the worker as a backstop. Each send is claimed first (`dispatched_phase` set in an UPDATE that only one process wins,
    committed), so a phase is sent once. A send the element refuses, or that cannot reach it, fails the job (`PHASE_FAILED`, the reason kept), and a campaign
    job's campaign is told. Returns the number of RPCs sent."""
    due = db.scalars(select(SoftwareManagementJob.job_id).where(
        SoftwareManagementJob.status == SwmState.IN_PROGRESS.value,
        or_(SoftwareManagementJob.dispatched_phase.is_(None), SoftwareManagementJob.dispatched_phase != SoftwareManagementJob.phase))
        .order_by(SoftwareManagementJob.job_id).limit(limit)).all()
    db.commit()
    sent = 0
    for job_id in due:
        claimed = cast(CursorResult, db.execute(update(SoftwareManagementJob).where(
            SoftwareManagementJob.job_id == job_id, SoftwareManagementJob.status == SwmState.IN_PROGRESS.value,
            or_(SoftwareManagementJob.dispatched_phase.is_(None), SoftwareManagementJob.dispatched_phase != SoftwareManagementJob.phase))
            .values(dispatched_phase=SoftwareManagementJob.phase).execution_options(synchronize_session=False)))
        db.commit()
        if claimed.rowcount != 1:
            continue                                          # another process sent it, or the job moved on meanwhile
        job = db.get_one(SoftwareManagementJob, job_id)
        db.refresh(job)
        try:
            outcome = swm_rpc.send_phase(db, job, f"{job_id}:{job.phase}")
        except Exception as exc:                              # noqa: BLE001  the job is claimed and committed: a send that breaks fails the job, never the caller's request
            log.warning("software job %s: sending %s raised %s", job_id, job.phase, type(exc).__name__)
            outcome = EditResult(False, "NETCONF_UNREACHABLE", type(exc).__name__)
        sent += 1
        if outcome.applied:
            continue
        db.refresh(job)
        if job.status != SwmState.IN_PROGRESS.value:
            continue
        job_event(db, job, SwmEvent.PHASE_FAILED, f"{swm_rpc.RPC_OF_PHASE[job.phase]} not accepted: {outcome.reason}" + (f" ({outcome.detail})" if outcome.detail else ""))
        db.commit()
        if job.campaign_id is not None:
            on_job_advanced(db, job.campaign_id)
    return sent


def _raise_alarm(db: Session, ref: str, source_id: str, severity: str, alarm_type: str, cause: str, problem: str) -> None:
    """One alarm per source id while it is raised: a repeat of the same finding does not add another."""
    standing = db.scalar(select(Alarm.alarm_id).where(Alarm.source_alarm_id == source_id, Alarm.managed_element_ref == ref, Alarm.severity != "cleared").limit(1))
    if standing is None:
        db.add(Alarm(source_alarm_id=source_id, managed_element_ref=ref, severity=severity, alarm_type=alarm_type, probable_cause=cause, specific_problem=problem))


# ---------------------------------------------------------------- notifications (MGT-14.7, MGT-15.6)

# The body of POST /lifecycle-subscriptions: where to send the notices (`callbackUri`, 1 to 2000 characters; whether it is an acceptable destination is checked by the route, not here)
# and which events, a list of the three event names (an unknown name is a 422; empty means all three). Unknown fields are refused. It has no docstring on purpose: a
# docstring would be published in docs/openapi.
class LifecycleSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    callbackUri: str = Field(min_length=1, max_length=2000)
    events: list[Literal["ONBOARDING_FAILED", "CAMPAIGN_HALTED", "CAMPAIGN_ROLLBACK_FAILED"]] = Field(default_factory=list, description="narrow to these events; empty means all three")


def _subscription_view(sub: LifecycleSubscription) -> dict:
    return {"subscriptionId": str(sub.subscription_id), "callbackUri": sub.callback_uri, "events": sub.events or [], "createdAt": _stamp(sub.created_at)}


@router.post("/lifecycle-subscriptions", status_code=201)
def subscribe_to_lifecycle_events(body: LifecycleSubscriptionRequest, db: Session = Depends(get_session)):
    """MGT-14.7/15.6: be told (a POST to `callbackUri`, through the outbox) when an element's onboarding fails (`ONBOARDING_FAILED`), a software campaign halts
    (`CAMPAIGN_HALTED`: a failed gate or an operator's halt, not the routine pause between waves) or its rollback fails (`CAMPAIGN_ROLLBACK_FAILED`). `events`
    narrows it, empty means all three. A destination the SSRF guard refuses is a 422 here rather than a silent drop later."""
    if not is_safe_webhook_destination(body.callbackUri):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="callbackUri is not an acceptable destination")
    sub = LifecycleSubscription(callback_uri=body.callbackUri, events=sorted(set(body.events)))
    db.add(sub)
    db.commit()
    return _subscription_view(sub)


@router.get("/lifecycle-subscriptions")
def list_lifecycle_subscriptions(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Answers the page envelope (`items`, `limit`, `offset` and `total` or `hasMore`) of the subscriptions, oldest first, each as `_subscription_view` shows it: the callback, the events
    # (an empty list means all three) and when it was made. An empty page when nobody subscribed. Reads only; no write, no commit and no outbox row.
    page = paginate(db, select(LifecycleSubscription).order_by(LifecycleSubscription.created_at), limit, offset)
    return {**page, "items": [_subscription_view(s) for s in page["items"]]}


@router.delete("/lifecycle-subscriptions/{subscription_id}", status_code=204)
def unsubscribe_from_lifecycle_events(subscription_id: uuid.UUID, db: Session = Depends(get_session)):
    # Deletes the subscription and commits, answering 204 with no body; notices already in the outbox for it are still delivered. An id that does not exist is a 404
    # LIFECYCLE_SUBSCRIPTION_NOT_FOUND (and nothing is written); an id that is not a UUID is a 422 from the path validation.
    sub = db.get(LifecycleSubscription, subscription_id)
    if sub is None:
        raise framework_error(LIFECYCLE_SUBSCRIPTION_NOT_FOUND, detail=f"no subscription {subscription_id}")
    db.delete(sub)
    db.commit()
    return Response(status_code=204)


def _notify_lifecycle(db: Session, event_type: str, href: str, fields: dict) -> None:
    """One outbox row per subscriber that wants `event_type`, in the transaction that records the event, so the notice exists exactly when the failure or halt does and
    is sent only after the commit. Nothing is enqueued, and nothing is read but one empty query, when nobody subscribed. The fields are the platform's own
    wording (a reason code, a count, an element reference), never an exception's text."""
    event = {"href": href, "eventType": event_type, **fields, "occurredAt": _now().isoformat()}
    for sub in db.scalars(select(LifecycleSubscription)).all():
        if not sub.events or event_type in sub.events:
            enqueue(db, sub.callback_uri, event)



# ---------------------------------------------------------------- onboarding templates (MGT-14.1)

class TemplateChange(BaseModel):
    """One change of a template: the element is the new element's own, so there is no `managedElementRef`."""
    model_config = ConfigDict(extra="forbid")
    managedFunctionRef: str | None = Field(default=None, max_length=500)
    attributeChanges: dict[str, Any] = Field(default_factory=dict)
    operation: Literal["merge", "replace", "create", "delete", "remove"] = "merge"

    @field_validator("managedFunctionRef")
    @classmethod
    def _well_formed_ref(cls, value: str | None) -> str | None:
        return check_ref(value)


# Request body of `PUT /onboarding-templates/{name}`: the element type (and optionally vendor) the template is for, the CM changes to apply (1 to 200), and the software baseline with its two switches.
# `requireBaseline` without a `softwareBaseline` is refused by the model validator.
class OnboardingTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entityType: str = Field(min_length=1, max_length=100)
    vendorName: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    changes: list[TemplateChange] = Field(min_length=1, max_length=200)
    softwareBaseline: str | None = Field(default=None, min_length=1, max_length=100)
    requireBaseline: bool = False
    autoApply: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def _baseline_needed(self):
        if self.requireBaseline and self.softwareBaseline is None:
            raise ValueError("requireBaseline needs a softwareBaseline")
        return self


def _template_view(t: OnboardingTemplate) -> dict:
    return {"name": t.name, "description": t.description, "entityType": t.entity_type, "vendorName": t.vendor_name, "softwareBaseline": t.software_baseline,
            "requireBaseline": t.require_baseline, "autoApply": t.auto_apply, "enabled": t.enabled, "changes": t.changes,
            "createdAt": _stamp(t.created_at), "updatedAt": _stamp(t.updated_at)}


def _template_or_404(db: Session, name: str) -> OnboardingTemplate:
    template = db.get(OnboardingTemplate, name) if _NAME.match(name) else None
    if template is None:
        raise framework_error(ONBOARDING_TEMPLATE_NOT_FOUND, detail=f"no onboarding template {name!r}")
    return template


@router.put("/onboarding-templates/{name}")
def put_onboarding_template(name: str, body: OnboardingTemplateRequest, db: Session = Depends(get_session)):
    """MGT-14.1: define (or replace) the template `name`. Takes effect for the elements registered after it: an element already matched keeps its row until it is
    selected again. 422 for a name that is not 1 to 100 letters, digits, `.` `_` `-`."""
    if not _NAME.match(name):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="a template name is 1 to 100 letters, digits, '.', '_' or '-', starting with a letter or digit")
    now = _now()
    template = db.get(OnboardingTemplate, name)
    if template is None:
        template = OnboardingTemplate(name=name, created_at=now)
        db.add(template)
    template.description, template.entity_type, template.vendor_name = body.description, body.entityType, body.vendorName
    template.software_baseline, template.require_baseline, template.auto_apply, template.enabled = body.softwareBaseline, body.requireBaseline, body.autoApply, body.enabled
    template.changes = [c.model_dump(exclude_defaults=False) for c in body.changes]
    template.updated_at = now
    db.commit()
    return _template_view(template)


@router.get("/onboarding-templates")
def list_onboarding_templates(entity_type: str | None = None, limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Paged list, optionally only the templates of one `entity_type`. Reads only.
    stmt = select(OnboardingTemplate)
    if entity_type:
        stmt = stmt.where(OnboardingTemplate.entity_type == entity_type)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_template_view(t) for t in page["items"]]}


@router.get("/onboarding-templates/{name}")
def get_onboarding_template(name: str, db: Session = Depends(get_session)):
    # 404 ONBOARDING_TEMPLATE_NOT_FOUND for an unknown name, and also for a name that is not shaped like a template name.
    return _template_view(_template_or_404(db, name))


@router.delete("/onboarding-templates/{name}", status_code=204)
def delete_onboarding_template(name: str, db: Session = Depends(get_session)):
    """Removing a template does not touch the elements already matched to it: their row keeps the name, and applying it again is 404 until they are selected again."""
    db.delete(_template_or_404(db, name))
    db.commit()


# ---------------------------------------------------------------- onboarding of an element (MGT-14.2 to 14.5)

def _matching_template(db: Session, me: ManagedEntity) -> OnboardingTemplate | None:
    """The enabled template for this element: its entity type, and its vendor if the template names one; a template that names the vendor beats one that does not."""
    candidates = db.scalars(select(OnboardingTemplate).where(OnboardingTemplate.enabled.is_(True), OnboardingTemplate.entity_type == me.entity_type)).all()
    fits = sorted((t for t in candidates if t.vendor_name is None or t.vendor_name == me.vendor_name), key=lambda t: (t.vendor_name is None, t.name))
    return fits[0] if fits else None


def _check_baseline(db: Session, row: ElementOnboarding, template: OnboardingTemplate | None, version: str | None) -> None:
    """MGT-14.4: compare the software version the element reported with the template's baseline; a mismatch is flagged on the row and as a warning alarm."""
    if version is not None:
        row.software_version = version
    row.software_baseline = template.software_baseline if template is not None else None
    if row.software_baseline is None or row.software_version is None:
        row.software_check = "NOT_CHECKED"
    elif row.software_baseline == row.software_version:
        row.software_check = "MATCH"
    else:
        row.software_check = "MISMATCH"
        _raise_alarm(db, row.managed_element_ref, f"onboarding-software:{row.managed_element_ref}", "warning", "OPERATIONAL_VIOLATION",
                     "SOFTWARE_BASELINE_MISMATCH", f"runs {row.software_version}, the template expects {row.software_baseline}")


def _select(db: Session, row: ElementOnboarding, me: ManagedEntity, name: str | None = None, version: str | None = None) -> None:
    """MGT-14.2: the template for the element (the named one, else the best match) goes on the row, with the baseline check."""
    template: OnboardingTemplate | None
    if name is not None:
        template = _template_or_404(db, name)
        if template.entity_type != me.entity_type:
            raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                                  detail=f"template {name!r} is for entity type {template.entity_type!r}, the element is {me.entity_type!r}")
    else:
        template = _matching_template(db, me)
    try:
        row.status = ONBOARDING_FSM.fire(OnboardingState(row.status), OnboardingEvent.TEMPLATE_MATCHED if template is not None else OnboardingEvent.NO_MATCH)
    except IllegalTransition as exc:
        raise illegal_transition_error(exc, f"onboarding of {row.managed_element_ref}") from None
    row.template_name = template.name if template is not None else None
    row.config_job_id = None
    row.detail = None if template is not None else f"no enabled template for entity type {me.entity_type!r}"
    _check_baseline(db, row, template, version)
    row.updated_at = _now()


def on_registered(db: Session, me: ManagedEntity, software_version: str | None = None) -> ElementOnboarding | None:
    """MGT-14.2: a newly registered element is matched against the templates, in the registration's own transaction. No template defined, no row: the
    registration is then exactly what it was before this existed."""
    if db.scalar(select(OnboardingTemplate.name).where(OnboardingTemplate.enabled.is_(True)).limit(1)) is None:
        return None
    row = ElementOnboarding(managed_element_ref=me.managed_element_ref, status=OnboardingState.DISCOVERED.value, created_at=_now(), updated_at=_now())
    db.add(row)
    _select(db, row, me, version=software_version)
    return row


def _onboarding_view(row: ElementOnboarding) -> dict:
    return {"managedElementRef": row.managed_element_ref, "status": row.status, "templateName": row.template_name, "softwareVersion": row.software_version,
            "softwareBaseline": row.software_baseline, "softwareCheck": row.software_check, "configJobId": str(row.config_job_id) if row.config_job_id else None,
            "detail": row.detail, "createdAt": _stamp(row.created_at), "updatedAt": _stamp(row.updated_at)}


def _onboarding_or_404(db: Session, ref: str, request: Request | None = None) -> ElementOnboarding:
    """The onboarding row of an element, or 404 ELEMENT_ONBOARDING_NOT_FOUND. When `request` is given the caller's scope is checked first (403 SCOPE_DENIED), so an out-of-scope element is refused before its row is looked up.
    """
    if request is not None:
        scoping.require_elements(db, scoping.request_scope(request), [ref])          # PR-SEC-10: 403 for an element outside the caller's scope
    row = db.get(ElementOnboarding, ref)
    if row is None:
        raise framework_error(ELEMENT_ONBOARDING_NOT_FOUND, detail=f"no onboarding of {ref}: it was registered before a template existed; select a template first")
    return row


def _fail(db: Session, row: ElementOnboarding, detail: str) -> None:
    """Ends an apply as FAILED. Rolls back the caller's open transaction first, so nothing the failed attempt left uncommitted is kept, and reloads the row, then moves it with APPLY_FAILED, keeps the reason, raises a major alarm (once while it stands), queues the `ONBOARDING_FAILED` notices and commits, all in the one transaction that records the failure.
    """
    db.rollback()
    db.refresh(row)
    row.status = ONBOARDING_FSM.fire(OnboardingState(row.status), OnboardingEvent.APPLY_FAILED)
    row.detail, row.updated_at = detail[:1000], _now()
    _raise_alarm(db, row.managed_element_ref, f"onboarding:{row.managed_element_ref}", "major", "PROCESSING_ERROR_ALARM", "ONBOARDING_FAILED", detail[:200])
    _notify_lifecycle(db, "ONBOARDING_FAILED", f"/ran-nf-oam/element-onboarding/{row.managed_element_ref}",
                      {"managedElementRef": row.managed_element_ref, "templateName": row.template_name,
                       "configJobId": str(row.config_job_id) if row.config_job_id else None, "detail": row.detail})
    db.commit()


_MAIN: Any = None


def bind_main(module: Any) -> None:
    """main.py hands over its own module object once, after it has defined everything this file needs from it. Looking `app.main` up again at call time can find
    another module's `app.main` when several services are loaded in one process (the contract tests do), which made `POST /element-onboarding/{ref}/apply` fail
    now and then in CI with "module 'app.main' has no attribute 'WriteConfigRequest'"."""
    global _MAIN
    _MAIN = module


def _main_module() -> Any:
    if _MAIN is not None:
        return _MAIN
    from . import main                                                                       # late: main includes this router (only when nothing was bound)
    return main


def apply_template(db: Session, row: ElementOnboarding, requested_by: str, software_version: str | None = None) -> ElementOnboarding:
    """MGT-14.3: write the template to the element as a config job, and end in ONBOARDED or FAILED. The state APPLYING is committed first, so a second apply of the
    same element (another replica, a double click) is refused as an illegal transition instead of writing twice."""
    main = _main_module()

    try:
        row.status = ONBOARDING_FSM.fire(OnboardingState(row.status), OnboardingEvent.APPLY)
    except IllegalTransition as exc:
        raise illegal_transition_error(exc, f"onboarding of {row.managed_element_ref}") from None
    template = db.get(OnboardingTemplate, row.template_name) if row.template_name else None
    if template is None:                                                                     # nothing is committed yet: the row keeps its state
        raise framework_error(ONBOARDING_TEMPLATE_NOT_FOUND, detail=f"the template {row.template_name!r} of {row.managed_element_ref} does not exist (any more); select one")
    _check_baseline(db, row, template, software_version)
    row.detail, row.config_job_id, row.updated_at = None, None, _now()
    db.commit()                                                                              # APPLYING is visible, and a rollback below cannot undo it
    ref = row.managed_element_ref
    if template.require_baseline and row.software_check != "MATCH":
        found = row.software_version or "not reported"
        _fail(db, row, f"SOFTWARE_BASELINE_MISMATCH: the template requires {template.software_baseline}, the element runs {found}")
        return row
    body = main.WriteConfigRequest(requestedBy=f"onboarding:{template.name}", accessScope="managed-element",
                                   changes=[{"managedElementRef": ref, **({"managedFunctionRef": c["managedFunctionRef"]} if c.get("managedFunctionRef") else {}),
                                             "attributeChanges": c.get("attributeChanges", {}), "operation": c.get("operation", "merge")} for c in template.changes])
    try:
        result = main._execute_write(body, db)
    except HTTPException as exc:
        title = exc.detail.get("title", "refused") if isinstance(exc.detail, dict) else "refused"
        why = exc.detail.get("detail") if isinstance(exc.detail, dict) else None
        _fail(db, row, f"the configuration was refused ({title}){': ' + why if why else ''}")
        return row
    except Exception as exc:
        log.exception("onboarding of %s: the config job failed unexpectedly", ref)
        _fail(db, row, f"internal error while writing the configuration ({type(exc).__name__})")
        raise
    row = db.get_one(ElementOnboarding, ref)
    row.config_job_id = uuid.UUID(result["jobId"])
    if result["status"] == "COMPLETED":
        row.status = ONBOARDING_FSM.fire(OnboardingState(row.status), OnboardingEvent.APPLIED)
        row.detail, row.updated_at = None, _now()
        db.commit()
        return row
    db.commit()                                                                              # the job id is kept with the failure
    first = db.scalars(select(WriteConfigSubChange).where(WriteConfigSubChange.job_id == row.config_job_id, WriteConfigSubChange.status == "REJECTED")
                       .order_by(WriteConfigSubChange.position)).first()
    why = f": {first.managed_function_ref or first.managed_element_ref}: {first.rejection_reason}" if first is not None else ""
    _fail(db, row, f"config job {row.config_job_id} ended {result['status']}{why}")
    return row


def on_first_heartbeat(db: Session, managed_element_ref: str) -> None:
    """MGT-14.3: the element has reported in for the first time; an `autoApply` template is applied now. Never fails the heartbeat: the outcome is on the row."""
    row = db.get(ElementOnboarding, managed_element_ref)
    if row is None or row.status != OnboardingState.TEMPLATE_SELECTED.value:
        return
    template = db.get(OnboardingTemplate, row.template_name) if row.template_name else None
    if template is None or not template.auto_apply or not template.enabled:
        return
    try:
        apply_template(db, row, "onboarding:auto")
    except Exception:                                                                        # the row says FAILED (apply_template); the heartbeat is still a heartbeat
        log.exception("onboarding of %s: the automatic apply failed", managed_element_ref)


# Request body of `POST /element-onboarding/{ref}/select`: an optional template to use instead of the best match, and the software version the element runs.
class SelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str | None = Field(default=None, description="a template to use instead of the best match")
    softwareVersion: str | None = Field(default=None, min_length=1, max_length=100)


# Request body of `POST /element-onboarding/{ref}/apply`: who applies the template, and optionally the software version the element runs (for the baseline check).
class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestedBy: str = Field(min_length=1, max_length=200)
    softwareVersion: str | None = Field(default=None, min_length=1, max_length=100)


@router.get("/element-onboarding")
def list_element_onboarding(request: Request, status: Literal["DISCOVERED", "NO_TEMPLATE", "TEMPLATE_SELECTED", "APPLYING", "ONBOARDED", "FAILED"] | None = None,
                            software_check: Literal["NOT_CHECKED", "MATCH", "MISMATCH"] | None = None, region: str | None = scoping.RegionFilter,
                            site_cluster: str | None = scoping.SiteClusterFilter, limit: int = PageLimit, offset: int = PageOffset,
                            db: Session = Depends(get_session)):
    """MGT-14.5: the onboarding rows, narrowed by `status`, `software_check` and (PR-GUI-9.3) the `region` and `site_cluster` of the row's element.
    A caller with a scope claim sees only the rows of the elements inside it."""
    stmt = scoping.scoped_to_elements(select(ElementOnboarding), scoping.request_scope(request), ElementOnboarding.managed_element_ref)
    stmt = scoping.narrowed_to_place(stmt, ElementOnboarding.managed_element_ref, region, site_cluster)
    if status:
        stmt = stmt.where(ElementOnboarding.status == status)
    if software_check:
        stmt = stmt.where(ElementOnboarding.software_check == software_check)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_onboarding_view(r) for r in page["items"]]}


@router.get("/element-onboarding/{managed_element_ref}")
def get_element_onboarding(managed_element_ref: str, request: Request, db: Session = Depends(get_session)):
    # 403 SCOPE_DENIED outside the caller's scope; 404 ELEMENT_ONBOARDING_NOT_FOUND when the element was never matched (select a template first).
    return _onboarding_view(_onboarding_or_404(db, managed_element_ref, request))


@router.post("/element-onboarding/{managed_element_ref}/select")
def select_template(managed_element_ref: str, body: SelectRequest, request: Request, db: Session = Depends(get_session)):
    """MGT-14.2: match the element against the templates (again). The way in for an element registered before any template existed, and after a template changed.
    `template` names one to use instead of the best match (it must be for the element's entity type). Not while the template is being applied (409)."""
    scoping.require_elements(db, scoping.request_scope(request), [managed_element_ref])
    me = db.get(ManagedEntity, managed_element_ref)
    if me is None:
        raise framework_error(FrameworkError.O1_ENDPOINT_NOT_FOUND, detail=f"unknown managed element {managed_element_ref}")
    row = db.get(ElementOnboarding, managed_element_ref)
    if row is None:
        row = ElementOnboarding(managed_element_ref=managed_element_ref, status=OnboardingState.DISCOVERED.value, created_at=_now(), updated_at=_now())
        db.add(row)
    _select(db, row, me, name=body.template, version=body.softwareVersion)
    db.commit()
    return _onboarding_view(row)


@router.post("/element-onboarding/{managed_element_ref}/apply", status_code=202)
def apply_element_template(managed_element_ref: str, body: ApplyRequest, request: Request, db: Session = Depends(get_session)):
    """MGT-14.3/14.5: write the selected template to the element as a config job. 202 with the row: ONBOARDED, or FAILED with the reason (and `configJobId`).
    409 when the element has no template selected or is being applied; 403 outside the caller's scope. `softwareVersion` is what the element runs, for the baseline check."""
    row = _onboarding_or_404(db, managed_element_ref, request)
    return _onboarding_view(apply_template(db, row, body.requestedBy, body.softwareVersion))


# ---------------------------------------------------------------- software campaigns (MGT-15)

class CampaignSelector(BaseModel):
    """Which elements: all that match every key given."""
    model_config = ConfigDict(extra="forbid")
    entityType: str | None = Field(default=None, max_length=100)
    vendorName: str | None = Field(default=None, max_length=100)
    region: str | None = Field(default=None, max_length=100)
    tenant: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _not_empty(self):
        if not any((self.entityType, self.vendorName, self.region, self.tenant)):
            raise ValueError("a selector names at least one of entityType, vendorName, region, tenant")
        return self


# Request body of `POST /software-campaigns`. Exactly one of `managedElementRefs` and `selector` is given (the model validator refuses both or neither) and a repeated reference is dropped, keeping the first. `waveSize` omitted puts every element in one wave.
class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestedBy: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    softwareVersion: str | None = Field(default=None, min_length=1, max_length=100)
    # call flow 19: the package every job of the campaign downloads (`software-download`'s remote-file-path); without it the element is sent no path
    remoteFilePath: str | None = Field(default=None, min_length=1, max_length=1000)
    managedElementRefs: list[str] | None = Field(default=None, min_length=1, max_length=MAX_CAMPAIGN_ELEMENTS)
    selector: CampaignSelector | None = None
    waveSize: int | None = Field(default=None, ge=1)
    wavePauseSeconds: int = Field(default=0, ge=0)
    gateMaxNewAlarms: int = Field(default=0, ge=0)
    onGateFailure: Literal["halt", "rollback"] = "halt"
    jobTimeoutSeconds: int | None = Field(default=None, ge=1, le=7 * 86400, description="MGT-15.7: a software job still running this long after its wave (or rollback step) started is failed by the sweep; absent: no timeout")
    rollbackOrder: Literal["all", "reverse"] = Field(default="all", description="MGT-15.7: `all` starts every revert job at once; `reverse` undoes the last wave first and the next only when it has ended")
    dryRun: bool = False

    @model_validator(mode="after")
    def _elements_or_selector(self):
        """Model validator: exactly one of the two ways to name elements, and a named list de-duplicated in order."""
        if (self.managedElementRefs is None) == (self.selector is None):
            raise ValueError("name the elements (managedElementRefs) or select them (selector), one of the two")
        if self.managedElementRefs is not None:
            self.managedElementRefs = list(dict.fromkeys(self.managedElementRefs))
        return self


# Request body of the halt, continue, abort and rollback routes of a campaign: who asks, and for `continue` whether to go on although the pause between waves has not elapsed.
class CampaignAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestedBy: str = Field(min_length=1, max_length=200)
    force: bool = False                       # continue: go on although the pause has not elapsed


def _log(c: SoftwareCampaign, event: str, detail: str | None = None, wave: int | None = None, by: str | None = None, job: uuid.UUID | None = None) -> None:
    """The campaign's event log (reassigned: a JSON column does not track an in-place change). `job` is set on the one event that is about a job (JOB_TIMED_OUT)."""
    entry = {"at": _now().isoformat(), "event": event, "wave": wave if wave is not None else c.current_wave, "detail": detail, "by": by}
    if job is not None:
        entry["job"] = str(job)
    c.wave_log = [*c.wave_log, entry]


def _campaign_event(db: Session, c: SoftwareCampaign, event_type: str, reason: str | None, detail: str | None) -> None:
    """MGT-15.6: tell the subscribers of `event_type` (see `_notify_lifecycle`)."""
    _notify_lifecycle(db, event_type, f"/ran-nf-oam/software-campaigns/{c.campaign_id}",
                      {"campaignId": str(c.campaign_id), "name": c.name, "status": c.status, "wave": c.current_wave, "waveCount": c.wave_count, "reason": reason, "detail": detail})


def _wave_elements(c: SoftwareCampaign, wave: int) -> list[str]:
    size = c.wave_size or len(c.elements) or 1
    return list(c.elements[(wave - 1) * size: wave * size])


def _jobs(db: Session, c: SoftwareCampaign, wave: int | None = None, reverts: bool = False) -> list[SoftwareManagementJob]:
    """The software jobs of a campaign, by wave then element: its own jobs, or with `reverts` the revert jobs (those with `rollback_of`); one wave when `wave` is given.
    """
    stmt = select(SoftwareManagementJob).where(SoftwareManagementJob.campaign_id == c.campaign_id)
    stmt = stmt.where(SoftwareManagementJob.rollback_of.is_not(None) if reverts else SoftwareManagementJob.rollback_of.is_(None))
    if wave is not None:
        stmt = stmt.where(SoftwareManagementJob.campaign_wave == wave)
    return list(db.scalars(stmt.order_by(SoftwareManagementJob.campaign_wave, SoftwareManagementJob.managed_element_ref)).all())


def _in_flight(job: SoftwareManagementJob) -> bool:
    return job.status in (SwmState.PENDING.value, SwmState.IN_PROGRESS.value)


def _start_wave(db: Session, c: SoftwareCampaign, wave: int) -> None:
    """Makes `wave` the current wave and starts one software job per element of it (flushed, not committed: the caller's transaction commits the wave with the state change that led to it). The wave clock for `jobTimeoutSeconds` restarts, and any pause is cleared.
    """
    c.current_wave, c.wave_started_at, c.next_wave_at = wave, _now(), None
    for ref in _wave_elements(c, wave):
        start_software_job(db, ref, campaign_id=c.campaign_id, campaign_wave=wave, software_version=c.software_version, remote_file_path=c.remote_file_path)
    _log(c, "WAVE_STARTED", f"{len(_wave_elements(c, wave))} element(s)")
    db.flush()


# MGT-15.2: the health gate hook, as MGT-5.3's. Each gate looks at the wave that has just ended and returns why it fails, or None.
def _gate_failed_jobs(db: Session, c: SoftwareCampaign, jobs: list[SoftwareManagementJob], started: datetime.datetime) -> str | None:
    failed = [j for j in jobs if j.status == SwmState.FAILED.value]
    if failed:
        return f"{len(failed)} software job(s) of wave {c.current_wave} failed (first: {failed[0].managed_element_ref}, in phase {failed[0].phase})"
    return None


def _gate_new_alarms(db: Session, c: SoftwareCampaign, jobs: list[SoftwareManagementJob], started: datetime.datetime) -> str | None:
    """Health gate: fails when more critical or major alarms than `gateMaxNewAlarms` were raised, since the wave started, on the elements of the wave. Returns the reason, or None.
    """
    elements = {j.managed_element_ref for j in jobs}
    raised = db.scalar(select(func.count()).select_from(Alarm).where(
        Alarm.managed_element_ref.in_(elements), Alarm.raised_at >= started, Alarm.severity.in_(("critical", "major")))) or 0
    if raised > c.gate_max_new_alarms:
        return f"{raised} new critical or major alarm(s) on the elements of wave {c.current_wave} (limit {c.gate_max_new_alarms})"
    return None


HEALTH_GATES = [_gate_failed_jobs, _gate_new_alarms]


def _finish(c: SoftwareCampaign) -> None:
    c.status = CAMPAIGN_FSM.fire(CampaignState(c.status), CampaignEvent.FINISH)
    c.finished_at, c.next_wave_at, c.halted_reason, c.halted_detail = _now(), None, None, None
    _log(c, "COMPLETED")


def _halt(db: Session, c: SoftwareCampaign, reason: str, detail: str | None, next_at: datetime.datetime | None = None, by: str | None = None) -> None:
    """Moves a RUNNING campaign to HALTED with a reason, logs it and queues the `CAMPAIGN_HALTED` notice, except for `WAVE_PAUSE`, the routine pause between waves. `next_at` is when a pause ends. The caller commits.
    """
    c.status = CAMPAIGN_FSM.fire(CampaignState(c.status), CampaignEvent.HALT)
    c.halted_reason, c.halted_detail, c.next_wave_at = reason, detail, next_at
    _log(c, "HALTED", f"{reason}{': ' + detail if detail else ''}", by=by)
    log.warning("software campaign %s halted after wave %s of %s: %s %s", c.campaign_id, c.current_wave, c.wave_count, reason, detail or "")
    if reason != "WAVE_PAUSE":                                                # the pause between waves is routine, not news
        _campaign_event(db, c, "CAMPAIGN_HALTED", reason, detail)


def _undone(reverts: list[SoftwareManagementJob]) -> dict[uuid.UUID, str]:
    """For each job a revert was started for: COMPLETED if one revert completed, IN_PROGRESS while one runs, else FAILED."""
    state: dict[uuid.UUID, str] = {}
    rank = {"COMPLETED": 2, "IN_PROGRESS": 1, "FAILED": 0}
    for original, r in ((x.rollback_of, x) for x in reverts if x.rollback_of is not None):
        mine = "COMPLETED" if r.status == SwmState.COMPLETED.value else "IN_PROGRESS" if _in_flight(r) else "FAILED"
        if original not in state or rank[mine] > rank[state[original]]:
            state[original] = mine
    return state


def _revert_targets(db: Session, c: SoftwareCampaign, reverts: list[SoftwareManagementJob]) -> list[SoftwareManagementJob]:
    """The completed jobs that are not undone (and have no revert running): what a rollback still has to revert."""
    undone = _undone(reverts)
    return [j for j in _jobs(db, c) if j.status == SwmState.COMPLETED.value and undone.get(j.job_id) not in ("COMPLETED", "IN_PROGRESS")]


def _start_reverts(db: Session, c: SoftwareCampaign, targets: list[SoftwareManagementJob]) -> int:
    """Start the revert jobs. `rollbackOrder` "all": every target at once. "reverse" (MGT-15.7): only the targets of the last wave that has any, so the waves are undone
    last to first and the next one starts when this one has ended. The jobs' clock for `jobTimeoutSeconds` starts now."""
    if c.rollback_order == "reverse" and targets:
        last = max(j.campaign_wave or 0 for j in targets)
        targets = [j for j in targets if (j.campaign_wave or 0) == last]
        _log(c, "ROLLBACK_WAVE_STARTED", f"{len(targets)} job(s)", wave=last)
    if targets:
        c.wave_started_at = _now()
    for job in targets:
        start_software_job(db, job.managed_element_ref, campaign_id=c.campaign_id, campaign_wave=job.campaign_wave, rollback_of=job.job_id)
    db.flush()
    return len(targets)


def _start_rollback(db: Session, c: SoftwareCampaign, why: str, by: str | None = None) -> None:
    """MGT-15.3: one revert job per completed job that is not undone (or whose revert failed), all at once or last wave first (`rollbackOrder`). Nothing to undo ends
    the campaign ROLLED_BACK at once."""
    targets = _revert_targets(db, c, _jobs(db, c, reverts=True))
    c.status = CAMPAIGN_FSM.fire(CampaignState(c.status), CampaignEvent.ROLLBACK)
    c.halted_reason, c.halted_detail, c.next_wave_at, c.finished_at = None, None, None, None
    _log(c, "ROLLBACK_STARTED", f"{why}; {len(targets)} job(s) to undo", by=by)
    if not _start_reverts(db, c, targets):
        _progress_rollback(db, c)


def _progress_rollback(db: Session, c: SoftwareCampaign) -> None:
    """When the revert jobs started so far have all ended: ROLLBACK_FAILED if one failed (nothing further is undone: the waves before it stay as they are), else the
    next step of a reverse rollback, else ROLLED_BACK."""
    reverts = _jobs(db, c, reverts=True)
    if any(_in_flight(r) for r in reverts):
        return
    undone = _undone(reverts)
    failed = [job_id for job_id, state in undone.items() if state == "FAILED"]
    if failed:
        c.status = CAMPAIGN_FSM.fire(CampaignState(c.status), CampaignEvent.ROLLBACK_FAILED)
        _log(c, "ROLLBACK_FAILED", f"{len(failed)} revert job(s) failed")
        _campaign_event(db, c, "CAMPAIGN_ROLLBACK_FAILED", "ROLLBACK_FAILED", f"{len(failed)} revert job(s) failed")
    elif c.rollback_order == "reverse" and _start_reverts(db, c, _revert_targets(db, c, reverts)):
        return
    else:
        c.status = CAMPAIGN_FSM.fire(CampaignState(c.status), CampaignEvent.ROLLBACK_DONE)
        c.finished_at = _now()
        _log(c, "ROLLBACK_DONE", f"{len(undone)} job(s) undone")


def _next_wave_or_finish(db: Session, c: SoftwareCampaign) -> None:
    if c.current_wave >= c.wave_count:
        _finish(c)
    else:
        _start_wave(db, c, c.current_wave + 1)


def _progress_wave(db: Session, c: SoftwareCampaign) -> None:
    """The waves of a RUNNING campaign, from the one in progress: wait until its jobs have ended, run the gate, then finish, halt, undo or start the next wave."""
    while c.status == CampaignState.RUNNING.value:
        jobs = _jobs(db, c, c.current_wave)
        if any(_in_flight(j) for j in jobs):
            return
        started = as_utc(c.wave_started_at) if c.wave_started_at else _now()
        failure = next((f for f in (gate(db, c, jobs, started) for gate in HEALTH_GATES) if f), None)
        if failure:
            _log(c, "GATE_FAILED", failure)
            if c.on_gate_failure == "rollback":
                _start_rollback(db, c, failure)
            else:
                _halt(db, c, "GATE_FAILED", failure)
            return
        _log(c, "GATE_PASSED", f"{len(jobs)} job(s) completed")
        if c.current_wave >= c.wave_count:
            _finish(c)
            return
        if c.wave_pause_seconds > 0:
            _halt(db, c, "WAVE_PAUSE", None, _now() + datetime.timedelta(seconds=c.wave_pause_seconds))
            return
        _start_wave(db, c, c.current_wave + 1)


def progress(db: Session, c: SoftwareCampaign) -> None:
    """Do what the campaign's jobs now allow. Safe to call at any time: it does nothing for a campaign that is waiting."""
    if c.status == CampaignState.RUNNING.value:
        _progress_wave(db, c)
    elif c.status == CampaignState.ROLLING_BACK.value:
        _progress_rollback(db, c)


def on_job_advanced(db: Session, campaign_id: uuid.UUID) -> None:
    """A software job of a campaign has moved: the campaign may be able to go on. After the job's own commit, in its own transaction. If another request moved the
    campaign first (its version changed), the work is theirs, and the sweep (`advance_due`) is the backstop; the job's advance is not undone."""
    campaign = db.get(SoftwareCampaign, campaign_id)
    if campaign is None:
        return
    try:
        progress(db, campaign)
        db.commit()
    except StaleDataError:
        db.rollback()
        log.info("software campaign %s was moved by another request; the sweep will catch up", campaign_id)


def expire_jobs(db: Session, c: SoftwareCampaign) -> int:
    """MGT-15.7: fail the software jobs of a running (or rolling-back) campaign that are still going when `jobTimeoutSeconds` has passed since their wave, or their
    rollback step, started. The adaptor never reported, so the job is failed in the phase it was in; the campaign then decides as for any failed job (the gate halts
    or rolls back; a failed revert ends the rollback ROLLBACK_FAILED). A late report for such a job is refused as an illegal transition. Returns the number failed.
    A campaign made without `jobTimeoutSeconds` is never touched."""
    if c.job_timeout_seconds is None or c.wave_started_at is None or c.status not in (CampaignState.RUNNING.value, CampaignState.ROLLING_BACK.value):
        return 0
    if _now() < as_utc(c.wave_started_at) + datetime.timedelta(seconds=c.job_timeout_seconds):
        return 0
    jobs = _jobs(db, c, c.current_wave) if c.status == CampaignState.RUNNING.value else _jobs(db, c, reverts=True)
    stuck = [j for j in jobs if j.status == SwmState.IN_PROGRESS.value]
    for job in stuck:
        job_event(db, job, SwmEvent.PHASE_FAILED, f"no report within {c.job_timeout_seconds} s")
        _log(c, "JOB_TIMED_OUT", f"{job.managed_element_ref} did not report within {c.job_timeout_seconds} s (phase {job.phase})", wave=job.campaign_wave, job=job.job_id)
    return len(stuck)


def _resume(db: Session, c: SoftwareCampaign, by: str | None) -> None:
    """Moves a HALTED campaign to RUNNING and goes on. After a failed gate or a pause the current wave's gate has been answered (passed, or overridden by the operator), so the next wave starts, or the campaign finishes when it was the last; then `progress` runs whatever the jobs now allow. `by` is who continued it (the sweep passes 'sweep'). The caller commits.
    """
    reason = c.halted_reason
    c.status = CAMPAIGN_FSM.fire(CampaignState.HALTED, CampaignEvent.RESUME)
    c.halted_reason, c.halted_detail, c.next_wave_at = None, None, None
    _log(c, "CONTINUED", f"after {reason}", by=by)
    if reason in ("GATE_FAILED", "WAVE_PAUSE"):                         # the gate of this wave has been answered (an operator overrode it, or it passed)
        _next_wave_or_finish(db, c)
    progress(db, c)


def advance_due(db: Session) -> list[dict]:
    """For the worker (`app/tasks.py`): continue every campaign whose pause between waves has elapsed, and let every running or rolling-back campaign catch up on
    jobs that ended while no request was looking."""
    now = _now()
    moved = []
    due = db.scalars(select(SoftwareCampaign).where(SoftwareCampaign.status == CampaignState.HALTED.value, SoftwareCampaign.halted_reason == "WAVE_PAUSE",
                                                    SoftwareCampaign.next_wave_at <= now).order_by(SoftwareCampaign.next_wave_at)).all()
    for c in due:
        _resume(db, c, "sweep")
        db.commit()
        moved.append(_summary(c))
    for c in db.scalars(select(SoftwareCampaign).where(SoftwareCampaign.status.in_((CampaignState.RUNNING.value, CampaignState.ROLLING_BACK.value)))).all():
        before = (c.status, c.current_wave, len(c.wave_log))
        try:
            expire_jobs(db, c)                                                 # MGT-15.7: a job that never reported fails, and the campaign decides as for any failed job
            progress(db, c)
            if (c.status, c.current_wave, len(c.wave_log)) != before:
                db.commit()
                moved.append(_summary(c))
        except StaleDataError:                                                 # a job reported (or an operator acted) while the sweep looked: the next sweep sees it
            db.rollback()
            log.info("software campaign %s was moved by another request during the sweep; the next sweep catches up", c.campaign_id)
    return moved


def _summary(c: SoftwareCampaign) -> dict:
    return {"campaignId": str(c.campaign_id), "status": c.status, "wave": c.current_wave, "waveCount": c.wave_count, "haltedReason": c.halted_reason}


def _view(c: SoftwareCampaign) -> dict:
    return {**_summary(c), "name": c.name, "requestedBy": c.requested_by, "softwareVersion": c.software_version, "remoteFilePath": c.remote_file_path, "selector": c.selector, "elements": c.elements,
            "waveSize": c.wave_size, "wavePauseSeconds": c.wave_pause_seconds, "gateMaxNewAlarms": c.gate_max_new_alarms, "onGateFailure": c.on_gate_failure,
            "jobTimeoutSeconds": c.job_timeout_seconds, "rollbackOrder": c.rollback_order, "haltedDetail": c.halted_detail, "nextWaveAt": _stamp(c.next_wave_at), "createdAt": _stamp(c.created_at), "finishedAt": _stamp(c.finished_at),
            "events": c.wave_log}


def _campaign_or_404(db: Session, campaign_id: uuid.UUID, request: Request) -> SoftwareCampaign:
    """An unknown campaign, and one with an element outside the caller's scope, are the same 404 (PR-SEC-10: an id the system made)."""
    c = db.get(SoftwareCampaign, campaign_id)
    if c is None or scoping.denied_refs(db, scoping.request_scope(request), c.elements):
        raise framework_error(SOFTWARE_CAMPAIGN_NOT_FOUND, detail=f"no software campaign {campaign_id}")
    return c


def _resolve_elements(db: Session, body: CampaignRequest, request: Request) -> list[str]:
    """The campaign's elements in wave order, or 422/403. Named elements keep the order given; selected ones are in reference order."""
    scope = scoping.request_scope(request)
    if body.managedElementRefs is not None:
        scoping.require_elements(db, scope, body.managedElementRefs)
        known = set(db.scalars(select(ManagedEntity.managed_element_ref).where(ManagedEntity.managed_element_ref.in_(body.managedElementRefs))).all())
        missing = [r for r in body.managedElementRefs if r not in known]
        if missing:
            raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"not registered: {', '.join(missing[:10])}")
        elements = list(body.managedElementRefs)
        unsupported = [r for r in elements if (s := effective_services(db, db.get(ManagedEntity, r))) is not None and "SWM" not in s]
        if unsupported:
            raise framework_error(FrameworkError.O1_SERVICE_NOT_SUPPORTED, detail=f"no software management service: {', '.join(unsupported[:10])}")
    else:
        selector = cast(CampaignSelector, body.selector)          # the request validator guarantees one of the two
        stmt = scoping.scoped_to_elements(select(ManagedEntity), scope, ManagedEntity.managed_element_ref)
        for column, value in ((ManagedEntity.entity_type, selector.entityType), (ManagedEntity.vendor_name, selector.vendorName),
                              (ManagedEntity.region, selector.region), (ManagedEntity.tenant, selector.tenant)):
            if value is not None:
                stmt = stmt.where(column == value)
        rows = db.scalars(stmt.order_by(ManagedEntity.managed_element_ref)).all()
        elements = [m.managed_element_ref for m in rows if (s := effective_services(db, m)) is None or "SWM" in s]
        if not elements:
            raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="the selector matches no element that has a software management service")
        if len(elements) > MAX_CAMPAIGN_ELEMENTS:
            raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"the selector matches more than {MAX_CAMPAIGN_ELEMENTS} elements; narrow it")
    busy = db.scalars(select(SoftwareManagementJob.managed_element_ref).where(
        SoftwareManagementJob.managed_element_ref.in_(elements), SoftwareManagementJob.status.in_((SwmState.PENDING.value, SwmState.IN_PROGRESS.value))).distinct()).all()
    if busy:
        raise framework_error(FrameworkError.SOFTWARE_JOB_IN_PROGRESS, detail=f"a software job is already running on: {', '.join(sorted(busy)[:10])}")
    return elements


@router.post("/software-campaigns", status_code=202, responses={200: {"description": "dryRun: the waves were worked out and nothing was started"}})
@idempotent("ran-nf-oam", status_code=202)
def create_software_campaign(body: CampaignRequest, request: Request, db: Session = Depends(get_session)):
    """MGT-15.1/15.2: start a campaign. The elements (named or selected) are cut into waves of `waveSize` (all in one wave without it); the first wave's software jobs
    are started at once, and the campaign goes on by itself as the jobs end (see the module docstring). 422 for an element that is not registered or has no software
    management service, 409 for one that already has a software job running, 403 for one outside the caller's scope. `dryRun` answers 200 with the waves and starts nothing."""
    elements = _resolve_elements(db, body, request)
    size = body.waveSize or len(elements)
    wave_count = math.ceil(len(elements) / size)
    if body.dryRun:
        return JSONResponse(status_code=200, content={"dryRun": True, "status": "VALIDATED", "waveCount": wave_count,
                                                      "waves": [elements[i * size:(i + 1) * size] for i in range(wave_count)]})
    c = SoftwareCampaign(name=body.name, requested_by=body.requestedBy, status=CampaignState.PENDING.value, software_version=body.softwareVersion,
                         remote_file_path=body.remoteFilePath,
                         selector=body.selector.model_dump(exclude_none=True) if body.selector else None, elements=elements, wave_size=body.waveSize,
                         wave_count=wave_count, wave_pause_seconds=body.wavePauseSeconds, gate_max_new_alarms=body.gateMaxNewAlarms,
                         on_gate_failure=body.onGateFailure, job_timeout_seconds=body.jobTimeoutSeconds, rollback_order=body.rollbackOrder, wave_log=[], created_at=_now())
    db.add(c)
    db.flush()
    c.status = CAMPAIGN_FSM.fire(CampaignState.PENDING, CampaignEvent.START)
    _log(c, "STARTED", f"{len(elements)} element(s) in {wave_count} wave(s)", wave=0, by=body.requestedBy)
    _start_wave(db, c, 1)
    db.commit()
    dispatch_due(db)                                                       # call flow 19b: the first wave's DOWNLOAD RPCs
    return _summary(c)


@router.get("/software-campaigns")
def list_software_campaigns(request: Request, status: Literal["PENDING", "RUNNING", "HALTED", "COMPLETED", "ABORTED", "ROLLING_BACK", "ROLLED_BACK", "ROLLBACK_FAILED"] | None = None,
                            region: str | None = scoping.RegionFilter, site_cluster: str | None = scoping.SiteClusterFilter,
                            limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """MGT-15.1: the campaigns, narrowed by `status`. PR-GUI-9.3: `region` and `site_cluster` keep a campaign when any of its elements (the `elements`
    fixed at creation, which every wave's jobs are made from) is in that place; with `region` alone, also a campaign whose selector chose that region
    (its elements may have moved since). A caller with a scope claim sees only the campaigns none of whose jobs is outside it (PR-SEC-10)."""
    stmt = select(SoftwareCampaign)
    in_place = scoping.json_refs_in_place(db, SoftwareCampaign.elements, region, site_cluster)
    if in_place is not None:
        if region and not site_cluster:
            in_place = in_place | (SoftwareCampaign.selector["region"].as_string() == region)
        stmt = stmt.where(in_place)
    scope = scoping.request_scope(request)
    if scope is not None:
        outside = (select(SoftwareManagementJob.job_id).where(SoftwareManagementJob.campaign_id == SoftwareCampaign.campaign_id,
                                                              ~SoftwareManagementJob.managed_element_ref.in_(scoping.visible_elements(scope))))
        stmt = stmt.where(~outside.exists())
    if status:
        stmt = stmt.where(SoftwareCampaign.status == status)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_summary(c) | {"name": c.name, "softwareVersion": c.software_version, "createdAt": _stamp(c.created_at)} for c in page["items"]]}


@router.get("/software-campaigns/{campaign_id}")
def get_software_campaign(campaign_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    # The campaign with its elements, settings and event log. 404 SOFTWARE_CAMPAIGN_NOT_FOUND for an unknown campaign and for one with an element outside the caller's scope.
    return _view(_campaign_or_404(db, campaign_id, request))


@router.post("/software-campaigns/advance-due")
def advance_due_campaigns(db: Session = Depends(get_session)):
    """MGT-15.2: for a scheduler (the worker runs it): continue campaigns whose pause between waves has elapsed, and catch up running ones."""
    return {"advanced": advance_due(db)}


def _halted(db: Session, campaign_id: uuid.UUID, request: Request, event: CampaignEvent) -> SoftwareCampaign:
    c = _campaign_or_404(db, campaign_id, request)
    if c.status != CampaignState.HALTED.value:
        raise illegal_transition_error(IllegalTransition(CampaignState(c.status), event), f"software campaign {campaign_id}")
    return c


@router.post("/software-campaigns/{campaign_id}/continue", status_code=202)
def continue_software_campaign(campaign_id: uuid.UUID, body: CampaignAction, request: Request, db: Session = Depends(get_session)):
    """MGT-15.2: go on with a halted campaign. After a failed gate this is the operator's decision to go on anyway (the next wave, or the end if it was the last); a campaign
    held by its pause goes on only when the pause has elapsed, unless `force`; one the operator halted has its current wave's gate run now."""
    c = _halted(db, campaign_id, request, CampaignEvent.RESUME)
    if c.halted_reason == "WAVE_PAUSE" and c.next_wave_at and as_utc(c.next_wave_at) > _now() and not body.force:
        raise framework_error(FrameworkError.WAVE_PAUSE_NOT_ELAPSED, detail=f"the pause between waves ends at {as_utc(c.next_wave_at).isoformat()}; send force=true to go on now")
    _resume(db, c, body.requestedBy)
    db.commit()
    dispatch_due(db)
    return _summary(c)


@router.post("/software-campaigns/{campaign_id}/halt")
def halt_software_campaign(campaign_id: uuid.UUID, body: CampaignAction, request: Request, db: Session = Depends(get_session)):
    """MGT-15.2: stop a campaign from starting its next wave. A running campaign halts after its current wave (the jobs in flight go on to their end); one held by its
    pause becomes an operator halt; one halted for another reason stays as it is. Any other state is 409."""
    c = _campaign_or_404(db, campaign_id, request)
    if c.status == CampaignState.RUNNING.value:
        _halt(db, c, "OPERATOR_HALT", f"halted by {body.requestedBy}", by=body.requestedBy)
    elif c.status == CampaignState.HALTED.value and c.halted_reason == "WAVE_PAUSE":
        c.halted_reason, c.halted_detail, c.next_wave_at = "OPERATOR_HALT", f"halted by {body.requestedBy}", None
        _log(c, "HALTED", "OPERATOR_HALT", by=body.requestedBy)
        _campaign_event(db, c, "CAMPAIGN_HALTED", "OPERATOR_HALT", c.halted_detail)
    elif c.status != CampaignState.HALTED.value:
        raise illegal_transition_error(IllegalTransition(CampaignState(c.status), CampaignEvent.HALT), f"software campaign {campaign_id}")
    db.commit()
    return _summary(c)


@router.post("/software-campaigns/{campaign_id}/abort")
def abort_software_campaign(campaign_id: uuid.UUID, body: CampaignAction, request: Request, db: Session = Depends(get_session)):
    """MGT-15.2: end a halted campaign here. The waves that ran stay as they are (undo them with the rollback route); the waves that did not run never will."""
    c = _halted(db, campaign_id, request, CampaignEvent.ABORT)
    c.status = CAMPAIGN_FSM.fire(CampaignState.HALTED, CampaignEvent.ABORT)
    c.finished_at, c.next_wave_at = _now(), None
    _log(c, "ABORTED", f"after {c.halted_reason}", by=body.requestedBy)
    db.commit()
    return _summary(c)


@router.post("/software-campaigns/{campaign_id}/rollback", status_code=202)
def rollback_software_campaign(campaign_id: uuid.UUID, body: CampaignAction, request: Request, db: Session = Depends(get_session)):
    """MGT-15.3: undo a campaign: one revert software job for each job that completed (and is not undone). From a halted, completed or aborted campaign, or one whose
    earlier rollback failed. 422 `ROLLBACK_NOT_POSSIBLE` while a job of the campaign is still running: let it end first. The revert jobs are advanced like any
    software job; the campaign ends ROLLED_BACK when they have all completed, ROLLBACK_FAILED if one failed (roll back again to retry those)."""
    c = _campaign_or_404(db, campaign_id, request)
    if c.status not in (CampaignState.HALTED.value, CampaignState.COMPLETED.value, CampaignState.ABORTED.value, CampaignState.ROLLBACK_FAILED.value):
        raise illegal_transition_error(IllegalTransition(CampaignState(c.status), CampaignEvent.ROLLBACK), f"software campaign {campaign_id}")
    running = [j.managed_element_ref for j in _jobs(db, c) if _in_flight(j)]
    if running:
        raise framework_error(FrameworkError.ROLLBACK_NOT_POSSIBLE, detail=f"software job(s) still running: {', '.join(running[:10])}")
    _start_rollback(db, c, f"requested by {body.requestedBy}", by=body.requestedBy)
    db.commit()
    dispatch_due(db)
    return _summary(c)


@router.get("/software-campaigns/{campaign_id}/report")
def software_campaign_report(campaign_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """MGT-15.4: the outcome. Per wave and element: the job, its phase and status, and whether a revert undid it. The totals, the elements that need attention (a job
    that failed, a revert that failed, an element not yet reached) and the event log (every gate answer and operator action)."""
    c = _campaign_or_404(db, campaign_id, request)
    undone = _undone(_jobs(db, c, reverts=True))
    waves = []
    counts = {"completed": 0, "failed": 0, "inProgress": 0, "reverted": 0}
    attention: list[dict] = []
    started: set[str] = set()
    timed_out = {e["job"] for e in c.wave_log if e.get("event") == "JOB_TIMED_OUT" and e.get("job")}           # MGT-15.7: only a campaign with a timeout has any
    for wave in range(1, c.wave_count + 1):
        entries = []
        for job in _jobs(db, c, wave):
            started.add(job.managed_element_ref)
            revert = undone.get(job.job_id)
            counts["completed" if job.status == SwmState.COMPLETED.value else "failed" if job.status == SwmState.FAILED.value else "inProgress"] += 1
            counts["reverted"] += revert == "COMPLETED"
            entries.append({"managedElementRef": job.managed_element_ref, "jobId": str(job.job_id), "phase": job.phase, "status": job.status, "revert": revert,
                            **({"timedOut": True} if str(job.job_id) in timed_out else {})})
            if job.status == SwmState.FAILED.value:
                attention.append({"managedElementRef": job.managed_element_ref,
                                  "problem": f"software job timed out in phase {job.phase}: the element did not report" if str(job.job_id) in timed_out else f"software job failed in phase {job.phase}"})
            elif revert == "FAILED":
                attention.append({"managedElementRef": job.managed_element_ref, "problem": "the revert job failed"})
        waves.append({"wave": wave, "elements": _wave_elements(c, wave), "started": bool(entries), "jobs": entries})
    not_reached = [r for r in c.elements if r not in started]
    if c.status in (CampaignState.ABORTED.value, CampaignState.ROLLED_BACK.value, CampaignState.ROLLBACK_FAILED.value) or (c.status == CampaignState.COMPLETED.value and not_reached):
        attention += [{"managedElementRef": r, "problem": "never reached: the campaign ended before its wave"} for r in not_reached]
    return {**_view(c), "summary": {"elements": len(c.elements), "started": len(started), "notReached": len(not_reached), **counts}, "waves": waves, "attention": attention}
