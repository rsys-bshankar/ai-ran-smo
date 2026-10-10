"""DME (Data Management and Exposure).

R1AP clause 7, R1AP's own original design — least mature R1 service group
(3 of 6 APIs alpha). Foundational Platform LLD section 3 is authoritative;
this is where the LLD's headline finding gets built: DataJob (section 3.3)
never had a schema OR endpoints in v1.3 at all.
"""

import datetime
import uuid

import httpx
import jsonschema
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.r1_client import R1Client
from smo_shared.invoker import invoker_id
from smo_shared.roles import ROLE_INTERNAL, role_of
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id, get_correlation_id
from smo_shared.pagination import PageLimit, PageOffset, paginate, paginate_list
from smo_shared import scope as authz_scope
from smo_shared.outbox import enqueue
from smo_shared.webhook import get_webhook
from smo_shared.timeutil import as_utc

from .models import (
    DELIVERY_METHODS, LIFECYCLE_STAGES, SOURCE_DOMAINS, DataJob, DataOffer, DataRecord, DmeActionRecord,
    DMEProducer, DMEProducerType, DMEType, DMETypeSubscription,
)

app = FastAPI(title="DME — Data Management and Exposure")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
apply_r1_gateway_security(app)
apply_correlation_id(app)

_r1 = R1Client()


install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)


# The body of producer and type registration. The type is identified by (namespace, name, version); `producerId` is the producer's own identity string. `producerHealthCallbackUrl` is called
# for the status reads and `jobCallbackUrl` receives job starts and stops. `sourceDomain`, when given, must be LIVE_RAN or DIGITAL_TWIN (422 otherwise).
class DMETypeRegistration(BaseModel):
    namespace: str
    name: str
    version: str
    typeName: str
    producerId: str
    dataProductionSchema: dict
    collectionSpec: dict | None = None
    producerHealthCallbackUrl: str
    jobCallbackUrl: str
    # Wave 3 (docs/ARCHITECTURE.md (DME)): source provenance. Both
    # optional — a producer that doesn't declare its domain skips the
    # Digital-Twin-inference eligibility check entirely (same permissive
    # shape as _validate_delivery_method's own offer check).
    sourceDomain: str | None = None  # LIVE_RAN | DIGITAL_TWIN
    sourceContext: dict | None = None


# The body of create and update of a data job. `dataDeliveryMethod` must be a known method and, when an offer exists for the type, one an offer committed to. `productionJobDefinition` is
# checked against the type's schema, and `lifecycleStage`, when given, must be a known stage; the allowed values are in the trailing comments.
class DataJobRequest(BaseModel):
    dataDeliveryMode: str  # ONE_TIME | CONTINUOUS
    dmeTypeId: uuid.UUID
    productionJobDefinition: dict = {}
    dataDeliveryMethod: str  # PULL_HTTP | PUSH_HTTP | STREAMING_KAFKA
    deliveryDetails: dict = {}
    consumerId: str
    lifecycleStage: str | None = None  # TRAINING | TESTING | EMULATION | INFERENCE | CLOSED_LOOP_FEEDBACK
    # GUI-9.8: how often, in seconds, the consumer expects a delivery. Optional; with it the job reports `late` once no record arrived for two intervals.
    expectedIntervalSeconds: int | None = Field(default=None, gt=0)


# The body of a data record: an arbitrary JSON object, stored as it is for the job.
class DataRecordRequest(BaseModel):
    payload: dict


class ActionRequest(BaseModel):
    """DME's O1 action-mediation request — mirrors ran-nf-oam's own
    WriteConfigRequest.changes shape ({managedElementRef,
    managedFunctionRef?, attributeChanges?, operation?}), plus an
    optional real ProvMnS IOC class name per change (className, e.g.
    'GNBDUFunction'/'NRCellDU' — specs/O1_Adaptor/
    O1_Adaptor_MnS_Hierarchy_Mapping_v4.xlsx) for audit/documentation
    purposes; ran-nf-oam itself still resolves purely on
    managedElementRef, unchanged this wave.
    """

    requestedBy: str
    changes: list[dict]
    scope: str = "single-ME"
    msacRole: str | None = None
    sourceContext: dict | None = None
    # Wave 10.1 (W10-18): a caller-chosen idempotency key. Re-sending an
    # action with an actionId DME already recorded is IGNORED — never
    # forwarded twice — so a retried or replayed decision can't double-write.
    actionId: uuid.UUID | None = None
    # PR-AI-13: why the rApp is acting, forwarded to RAN NF OAM, which keeps it as the decision record of the job: {inputsRef?, modelVersion?, rationale?,
    # actionId?} (a reference to the inputs, never the data). The action id above is added when the rApp gave one and this does not name it.
    decision: dict | None = None


# The body of a data offer. `dataDeliveryMethods` must be a non-empty list of known methods; the first one is the method the framework commits to.
class DataOfferRequest(BaseModel):
    dmeTypeId: uuid.UUID
    dataDeliveryMode: str
    productionJobDefinition: dict = {}
    dataDeliveryMethods: list[str]
    dataAvailabilityNotificationUri: str | None = None
    dataOfferTerminationNotificationUri: str


# The body of a type subscription: where to POST the notification when a type is registered or removed, and the subscriber's name for filtering the list.
class TypeSubscriptionRequest(BaseModel):
    notificationDestination: str
    owner: str


DME_TYPE_NOT_OWNER = ("DME_TYPE_NOT_OWNER", 403)       # kept here, not in smo_shared.errors, as ran-nf-oam keeps its approval codes


def _redefines(t: DMEType, body: DMETypeRegistration) -> bool:
    """True when registering `body` would change the stored definition of the existing type `t` (its name, schema, collection spec or source), as opposed to re-stating it."""
    return (t.type_name, t.data_production_schema, t.collection_spec, t.source_domain, t.source_context) != (
        body.typeName, body.dataProductionSchema, body.collectionSpec, body.sourceDomain, body.sourceContext)


def _may_redefine_type(db: Session, request: Request, t: DMEType) -> bool:
    """Whether the caller may change the definition of the existing type `t` (SEC-15.10).

    Allowed: a call that did not come through the gateway (no role: a test or an in-process call, trusted as elsewhere in this module); an SMO module or the operator's console
    (role `internal`: the GUI BFF lets only an admin register a type, and RAN NF OAM registers its own KPI types); and the caller that registered the type first
    (`registered_by` equals the invoker id the gateway vouched for). Any other rApp is refused. For a type registered before revision 0039 (`registered_by` is NULL) the
    producers linked to the type stand in for the first one, which is the best this build knows.
    """
    role = role_of(request)
    if role is None or role == ROLE_INTERNAL:
        return True
    caller = invoker_id(request)
    if not caller:
        return False
    if t.registered_by is not None:
        return t.registered_by == caller
    return db.get(DMEProducerType, (caller, t.dme_type_id)) is not None


@app.post("/production-capabilities", status_code=201)
def register_dme_type(body: DMETypeRegistration, request: Request, db: Session = Depends(get_session)):
    """HISTORY.md §7 — DME vs. the real ICS API, Producer/Type conflation
    finding, closed: ICS's own `PUT .../info-producers/{id}` and
    `PUT .../info-types/{id}` are two separate, idempotent create-or-update
    calls against two separate entities, many-to-many. This build keeps
    one wire-compatible request body (no caller needs to change), but now
    upserts a real `DMEProducer` row, upserts a real `DMEType` row keyed
    on (namespace, name, version) rather than erroring on an existing one,
    and links them — so a second producer registering an already-known
    type identity, or the same producer re-registering after a restart,
    both succeed instead of the old global `DME_TYPE_VERSION_CONFLICT`.
    """
    # Route notes. 422 for an unknown `sourceDomain`; then, for a type that is already registered (SEC-15.10), 403 `DME_TYPE_NOT_OWNER` when the request would change its
    # definition (`typeName`, `dataProductionSchema`, `collectionSpec`, `sourceDomain`, `sourceContext`) and the caller is not allowed to (`_may_redefine_type`); the same
    # definition by a second producer, or by the first one again, is the join / idempotent re-registration it always was. The check comes before the producer row is touched, so a
    # refusal changes nothing. A new type records its first caller in `registered_by`. Order after that, as before: producer upsert, type upsert, link, `REGISTERED` notice (new
    # type only), one commit.
    if body.sourceDomain is not None and body.sourceDomain not in SOURCE_DOMAINS:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"unknown sourceDomain {body.sourceDomain!r}")

    existing = db.scalar(select(DMEType).where(DMEType.namespace == body.namespace, DMEType.name == body.name, DMEType.version == body.version))
    if existing is not None and _redefines(existing, body) and not _may_redefine_type(db, request, existing):
        raise framework_error(DME_TYPE_NOT_OWNER, detail=f"the type {body.namespace}/{body.name}/{body.version} was registered by another producer: only that producer, "
                                                          "an SMO module or the operator may change its definition")

    producer = db.get(DMEProducer, body.producerId)
    if producer is None:
        producer = DMEProducer(producer_id=body.producerId, producer_health_callback_url=body.producerHealthCallbackUrl,
                                job_callback_url=body.jobCallbackUrl)
        db.add(producer)
    else:
        producer.producer_health_callback_url = body.producerHealthCallbackUrl
        producer.job_callback_url = body.jobCallbackUrl

    t = db.scalar(select(DMEType).where(DMEType.namespace == body.namespace, DMEType.name == body.name, DMEType.version == body.version))
    is_new_type = t is None
    if t is None:
        t = DMEType(namespace=body.namespace, name=body.name, version=body.version, registered_by=invoker_id(request) or body.producerId)
        db.add(t)
    t.type_name = body.typeName
    t.data_production_schema = body.dataProductionSchema
    t.collection_spec = body.collectionSpec
    t.source_domain = body.sourceDomain
    t.source_context = body.sourceContext
    db.flush()  # populate t.dme_type_id for a brand-new row before the link lookup below

    if db.get(DMEProducerType, (body.producerId, t.dme_type_id)) is None:
        db.add(DMEProducerType(producer_id=body.producerId, dme_type_id=t.dme_type_id))
    if is_new_type:
        # ICS's own notifyTypeRegistered fires from putInfoType (the
        # type's own declaration), not from a producer joining an
        # already-known type — an existing type gaining a second
        # producer, or a producer's idempotent re-registration, is
        # neither event. Enqueued in this transaction (PR-MSG-1.5): the
        # subscribers are told exactly when the type exists.
        _notify_type_subscribers(db, t.dme_type_id, t.data_production_schema, "REGISTERED")
    db.commit()
    return {"registrationId": str(t.dme_type_id)}


@app.get("/dme-types")
def discover_dme_types(data_category: str | None = None, db: Session = Depends(get_session)):
    """HISTORY.md §5: data_category was declared but silently
    never applied to the query. DMEType has no dedicated category
    column — namespace (the grouping half of R1AP's typeName convention,
    e.g. "RAN" in "RAN.CoverageIssue") is the closest concept it does
    have, so that's what this filters on.
    """
    stmt = select(DMEType)
    if data_category:
        stmt = stmt.where(DMEType.namespace == data_category)
    rows = db.scalars(stmt).all()
    return [_type_view(db, r) for r in rows]


@app.get("/production-capabilities")
def list_producers(db: Session = Depends(get_session)):
    """ICS's own `GET /data-producer/v1/info-producers` — previously no
    way at all to see a producer independent of the DmeType rows it
    happened to conflate with.
    """
    return [_producer_view(db, p) for p in db.scalars(select(DMEProducer)).all()]


@app.get("/production-capabilities/{producer_id}")
def get_producer(producer_id: str, db: Session = Depends(get_session)):
    # One registered producer with the ids of the types it supports. 404 PRODUCER_NOT_FOUND. Read only.
    p = db.get(DMEProducer, producer_id)
    if p is None:
        raise framework_error(FrameworkError.PRODUCER_NOT_FOUND, detail="no such producer")
    return _producer_view(db, p)


@app.delete("/production-capabilities", status_code=204)
def deregister_producer(producer_id: str, db: Session = Depends(get_session)):
    """The DME half of rApp Management's producer-reconsideration trigger
    (HISTORY.md §1): when a RAppInstance crashes or terminates,
    its own DME registration is no longer trustworthy and is torn down
    here. Idempotent — a producer_id with nothing registered is a no-op,
    not an error.

    HISTORY.md §7 — DME vs. the real ICS API: this used to also delete
    every DmeType (and dependent DataJob/DataOffer rows) this producer_id
    happened to have registered, because the old schema conflated a type
    with its one-and-only producer. Now that Producer and Type are two
    real, separately-owned entities (ICS's own deleteInfoProducer never
    touches info-types at all — those are only ever removed via their own
    `DELETE /info-types/{id}`, see `delete_dme_type` below), this only
    removes the producer and its producer-type links; a type some other
    producer still supports keeps serving, and a type this was the last
    producer for simply goes DISABLED until deleted or re-registered —
    the real fix for the conflation, not a cosmetic rename.
    """
    p = db.get(DMEProducer, producer_id)
    if p is None:
        return
    db.query(DMEProducerType).filter(DMEProducerType.producer_id == producer_id).delete()
    db.delete(p)
    db.commit()


@app.delete("/dme-types/{dme_type_id}", status_code=204)
def delete_dme_type(dme_type_id: uuid.UUID, db: Session = Depends(get_session)):
    """ICS's own `DELETE /data-producer/v1/info-types/{infoTypeId}`
    (deleteInfoType) — 409 ("has one or several active producers") if any
    producer still supports it, otherwise deletes it and every dependent
    DataJob/DataOffer (the cleanup `deregister_producer` used to do
    unconditionally, now correctly gated on the type actually having zero
    producers left, not merely on ONE producer having left).
    """
    t = db.get(DMEType, dme_type_id)
    if t is None:
        raise framework_error(FrameworkError.DME_TYPE_NOT_FOUND, detail="no such DME type")
    if db.scalar(select(DMEProducerType).where(DMEProducerType.dme_type_id == dme_type_id).limit(1)) is not None:
        raise framework_error(FrameworkError.DME_TYPE_HAS_ACTIVE_PRODUCERS, detail="type has one or several active producers")
    db.query(DataJob).filter(DataJob.dme_type_id == dme_type_id).delete()
    db.query(DataOffer).filter(DataOffer.dme_type_id == dme_type_id).delete()
    schema = t.data_production_schema
    db.delete(t)
    _notify_type_subscribers(db, dme_type_id, schema, "DEREGISTERED")
    db.commit()


@app.get("/production-capabilities/{producer_id}/status")
def query_producer_status(producer_id: str, db: Session = Depends(get_session)):
    """ICS's own GET .../info-producers/{id}/status
    (ProducerController.getInfoProducerStatus) returns a single
    ENABLED/DISABLED operational_state per producer, derived from the
    same producer-availability signal typeStatus itself now uses
    (ProducerStatusInfo, producer.isAvailable()). 404 if the producer has
    nothing registered at all, matching ICS's own getProducer-not-found
    behavior.
    """
    p = db.get(DMEProducer, producer_id)
    if p is None:
        raise framework_error(FrameworkError.PRODUCER_NOT_FOUND, detail="no such producer")
    operational_state = "ENABLED" if _producer_is_healthy(p.producer_health_callback_url) else "DISABLED"
    return {"producerId": producer_id, "operationalState": operational_state}


@app.post("/type-subscriptions", status_code=201)
def subscribe_type_changes(body: TypeSubscriptionRequest, db: Session = Depends(get_session)):
    """HISTORY.md §5: ICS's own `/info-type-subscription`
    (InfoTypeSubscriptions/ConsumerCallbacks) — a consumer notified
    whenever any DmeType is registered or removed. Entirely absent from
    this build until now. ICS's own PUT is create-or-update against a
    caller-supplied subscriptionId; this build's id is server-generated
    (same adaptation already made for every other subscription in this
    codebase — RAN Analytics, FOCOM).
    """
    sub = DMETypeSubscription(notification_destination=body.notificationDestination, owner=body.owner)
    db.add(sub)
    db.commit()
    return {"subscriptionId": str(sub.subscription_id)}


@app.get("/type-subscriptions")
def list_type_subscriptions(owner: str | None = None, limit: int = PageLimit, offset: int = PageOffset,
                             db: Session = Depends(get_session)):
    # The type subscriptions as a page, optionally only those of one `owner`. Read only.
    stmt = select(DMETypeSubscription)
    if owner:
        stmt = stmt.where(DMETypeSubscription.owner == owner)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_subscription_view(s) for s in page["items"]]}


@app.get("/type-subscriptions/{subscription_id}")
def get_type_subscription(subscription_id: uuid.UUID, db: Session = Depends(get_session)):
    # One type subscription. 404 TYPE_SUBSCRIPTION_NOT_FOUND. Read only.
    sub = db.get(DMETypeSubscription, subscription_id)
    if sub is None:
        raise framework_error(FrameworkError.TYPE_SUBSCRIPTION_NOT_FOUND, detail="no such subscription")
    return _subscription_view(sub)


@app.delete("/type-subscriptions/{subscription_id}", status_code=204)
def unsubscribe_type_changes(subscription_id: uuid.UUID, db: Session = Depends(get_session)):
    # Deletes the subscription. Idempotent: 204 whether or not it existed.
    sub = db.get(DMETypeSubscription, subscription_id)
    if sub is not None:
        db.delete(sub)
        db.commit()


def _notify_type_subscribers(db: Session, dme_type_id: uuid.UUID, job_data_schema: dict, status: str) -> None:
    """ICS's own ConsumerCallbacks.notifyTypeRegistered/notifyTypeRemoved
    (InfoTypeSubscriptions) — POSTs to every subscriber's
    notificationDestination whenever any DmeType is registered or
    removed. Unfiltered, matching the reference: ICS's own subscription
    has no per-type scoping at all — every subscriber hears about every
    type change. Best-effort, same pattern as every other subscriber
    notification in this build.
    """
    for sub in db.scalars(select(DMETypeSubscription)).all():
        enqueue(db, sub.notification_destination, {
            "infoTypeId": str(dme_type_id), "jobDataSchema": job_data_schema, "status": status,
        })


def _subscription_view(s: DMETypeSubscription) -> dict:
    return {"subscriptionId": str(s.subscription_id), "notificationDestination": s.notification_destination, "owner": s.owner}


def _validate_delivery_method(db: Session, dme_type_id: uuid.UUID, method: str) -> None:
    """Raises 409 DELIVERY_METHOD_NOT_OFFERED when `method` is not one of `DELIVERY_METHODS` or, for a type that has at least one offer, when no offer committed to it.
    A type with no offer at all accepts any known method. Read only.
    """
    if method not in DELIVERY_METHODS:
        raise framework_error(FrameworkError.DELIVERY_METHOD_NOT_OFFERED, detail=f"unknown method {method}")
    # Cross-check against the actual DataOffer(s) for this dmeTypeId, not just
    # the global wire-value set — a consumer requesting a method no offer for
    # this type ever committed to was previously accepted without complaint.
    # A type with no DataOffer at all skips this (not every DmeType requires
    # one in this build), so this only tightens the case where an offer exists.
    offers = db.scalars(select(DataOffer).where(DataOffer.dme_type_id == dme_type_id)).all()
    if offers and not any(o.data_delivery_method_committed == method for o in offers):
        raise framework_error(FrameworkError.DELIVERY_METHOD_NOT_OFFERED, detail=f"{method} not committed by any DataOffer for this dmeTypeId")


def _validate_job_definition_schema(db: Session, dme_type_id: uuid.UUID, definition: dict) -> None:
    """HISTORY.md §5: ICS's own InfoJobs.validateJsonObjectAgainstSchema
    (org.everit.json.schema, called from validatePutInfoJob) — productionJobDefinition
    was accepted as an arbitrary dict, never checked against the DmeType's own
    dataProductionSchema (R1AP's actual contract for what a valid job
    definition looks like). A dmeTypeId with no registered DmeType at all
    skips this, same permissive shape as _validate_delivery_method's own
    offer check — nothing else in create_data_job enforces the type's
    existence either.
    """
    dme_type = db.get(DMEType, dme_type_id)
    if dme_type is None:
        return
    try:
        jsonschema.validate(instance=definition, schema=dme_type.data_production_schema)
    except jsonschema.ValidationError as e:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=e.message) from e
    except jsonschema.SchemaError as e:
        # The registered dataProductionSchema itself is malformed — not the
        # caller's fault, but there's no meaningful way to validate against it.
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"registered dataProductionSchema is invalid: {e.message}") from e


def _validate_lifecycle_eligibility(db: Session, dme_type_id: uuid.UUID, lifecycle_stage: str | None) -> None:
    """Wave 3 (docs/ARCHITECTURE.md (DME)): the one data-source
    eligibility rule Phase-1 actually needs — a Digital Twin may feed
    Training/Emulation, never Inference. A dmeTypeId with no registered
    DMEType, or a type/job that never declared sourceDomain/
    lifecycleStage, skips this — same permissive shape as
    _validate_job_definition_schema's own type-existence check.
    """
    if lifecycle_stage is not None and lifecycle_stage not in LIFECYCLE_STAGES:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"unknown lifecycleStage {lifecycle_stage!r}")
    dme_type = db.get(DMEType, dme_type_id)
    if dme_type is None or dme_type.source_domain != "DIGITAL_TWIN" or lifecycle_stage != "INFERENCE":
        return
    raise framework_error(FrameworkError.DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE,
                           detail="a Digital Twin source may feed Training/Emulation, never Inference")


@app.post("/data-jobs", status_code=202)
def create_data_job(body: DataJobRequest, db: Session = Depends(get_session)):
    # Creates an ACTIVE job. Order of checks: the delivery method (409 DELIVERY_METHOD_NOT_OFFERED), the production definition against the registered type's schema (422 SCHEMA_VALIDATION_FAILED), then
    # the lifecycle stage and the rule that a Digital Twin source may not feed Inference (422 SCHEMA_VALIDATION_FAILED, DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE). The schema and eligibility checks are
    # skipped for a `dmeTypeId` that is not registered (the code does not check that it exists). For a registered type, one outbox row per producer of the type is written in the same transaction,
    # so each producer is told after the commit. Answers 202 with the job id.
    _validate_delivery_method(db, body.dmeTypeId, body.dataDeliveryMethod)
    _validate_job_definition_schema(db, body.dmeTypeId, body.productionJobDefinition)
    _validate_lifecycle_eligibility(db, body.dmeTypeId, body.lifecycleStage)
    job = DataJob(
        data_delivery_mode=body.dataDeliveryMode,
        dme_type_id=body.dmeTypeId,
        production_job_definition=body.productionJobDefinition,
        data_delivery_method=body.dataDeliveryMethod,
        delivery_details=body.deliveryDetails,
        consumer_id=body.consumerId,
        status="ACTIVE",
        lifecycle_stage=body.lifecycleStage,
        expected_interval_seconds=body.expectedIntervalSeconds,
    )
    _set_late_after(job)
    db.add(job)
    db.flush()  # the job's id, for the producers' payloads
    dme_type = db.get(DMEType, body.dmeTypeId)
    if dme_type is not None:
        _push_job_to_producers(db, dme_type, job)
    db.commit()
    return {"dataJobId": str(job.data_job_id)}


@app.get("/data-jobs/{data_job_id}")
def get_data_job(data_job_id: uuid.UUID, db: Session = Depends(get_session)):
    # One data job. 404 DATA_JOB_NOT_FOUND. Read only.
    job = db.get(DataJob, data_job_id)
    if job is None:
        raise framework_error(FrameworkError.DATA_JOB_NOT_FOUND, detail="no such data job")
    return _job_view(job)


@app.put("/data-jobs/{data_job_id}")
def update_data_job(data_job_id: uuid.UUID, body: DataJobRequest, db: Session = Depends(get_session)):
    """PutIndividualInfoJob (ICS's ConsumerController.java) — DME had no
    update-in-place semantics at all, only POST-create/DELETE. ICS's own
    PUT is create-or-update against a caller-supplied jobId (201 new /
    200 updated); this build's data_job_id is always server-generated
    (see create_data_job), so this endpoint only ever updates an
    existing job — 404 on an unknown id, matching this module's other
    GET/DELETE-by-id routes. ICS itself also rejects changing a job's
    type mid-update ("Cannot modify job type", 409 there) — the
    equivalent identity fields here are dmeTypeId/consumerId/
    dataDeliveryMode, all fixed at creation and immutable via this
    endpoint (same adaptation AI/ML Workflow's update_model already
    made for its own identity fields).
    """
    job = db.get(DataJob, data_job_id)
    if job is None:
        raise framework_error(FrameworkError.DATA_JOB_NOT_FOUND, detail="no such data job")
    if job.dme_type_id != body.dmeTypeId or job.consumer_id != body.consumerId or job.data_delivery_mode != body.dataDeliveryMode:
        raise framework_error(FrameworkError.DATA_JOB_TARGET_IMMUTABLE, detail="dmeTypeId/consumerId/dataDeliveryMode cannot change on update")
    _validate_delivery_method(db, body.dmeTypeId, body.dataDeliveryMethod)
    _validate_job_definition_schema(db, body.dmeTypeId, body.productionJobDefinition)
    _validate_lifecycle_eligibility(db, body.dmeTypeId, body.lifecycleStage)
    job.production_job_definition = body.productionJobDefinition
    job.data_delivery_method = body.dataDeliveryMethod
    job.delivery_details = body.deliveryDetails
    job.lifecycle_stage = body.lifecycleStage
    job.expected_interval_seconds = body.expectedIntervalSeconds
    _set_late_after(job)
    dme_type = db.get(DMEType, job.dme_type_id)
    if dme_type is not None:
        # ICS re-runs startInfoSubscriptionJob on every PUT, new or
        # updated — the producer is re-notified with the new job
        # definition, not just on first creation.
        _push_job_to_producers(db, dme_type, job)
    db.commit()
    return _job_view(job)


@app.get("/data-jobs/{data_job_id}/status")
def query_data_job_status(data_job_id: uuid.UUID, db: Session = Depends(get_session)):
    # The job's stored status. 404 DATA_JOB_NOT_FOUND. Read only.
    job = db.get(DataJob, data_job_id)
    if job is None:
        raise framework_error(FrameworkError.DATA_JOB_NOT_FOUND, detail="no such data job")
    return {"dataJobId": str(job.data_job_id), "status": job.status}


@app.delete("/data-jobs/{data_job_id}", status_code=204)
def terminate_data_job(data_job_id: uuid.UUID, db: Session = Depends(get_session)):
    """Handles both directions per section 3.7: a Consumer rApp cancelling
    its own job, or the DME framework itself tearing down a job it created
    against a Producer rApp (consumer_id == 'DME_FRAMEWORK').
    """
    job = db.get(DataJob, data_job_id)
    if job is None:
        return
    dme_type = db.get(DMEType, job.dme_type_id)
    db.delete(job)
    if dme_type is not None:
        _stop_job_at_producers(db, dme_type, data_job_id)      # enqueued in the transaction that deletes the job (MSG-1.10)
    db.commit()


@app.delete("/data-jobs", status_code=204)
def terminate_data_jobs_for_consumer(consumer_id: str, db: Session = Depends(get_session)):
    """HISTORY.md §7's DME vs. real ICS finding: the real
    `DELETE /data-consumer/v1/info-jobs?owner=X` (ics-api.yaml's own
    `deleteJobsForOwner`) — every job one consumer owns, torn down in
    one call, not one `terminate_data_job` at a time. Same per-job
    teardown as that route (producer notification included), just
    fanned out across every matching job.
    """
    jobs = db.scalars(select(DataJob).where(DataJob.consumer_id == consumer_id)).all()
    for job in jobs:
        dme_type = db.get(DMEType, job.dme_type_id)
        db.delete(job)
        if dme_type is not None:
            _stop_job_at_producers(db, dme_type, job.data_job_id)
        db.commit()


@app.post("/offers", status_code=201)
def create_data_offer(body: DataOfferRequest, db: Session = Depends(get_session)):
    # Stores an offer, committing to the first of the offered methods. 409 DELIVERY_METHOD_NOT_OFFERED when the list is empty or holds an unknown method. The type is not checked to exist. Answers 201.
    if not body.dataDeliveryMethods or not set(body.dataDeliveryMethods) <= DELIVERY_METHODS:
        raise framework_error(FrameworkError.DELIVERY_METHOD_NOT_OFFERED)
    offer = DataOffer(
        dme_type_id=body.dmeTypeId,
        data_delivery_methods_offered=body.dataDeliveryMethods,
        data_delivery_method_committed=body.dataDeliveryMethods[0],  # framework commits to one, section 3.5
        data_availability_notification_uri=body.dataAvailabilityNotificationUri,
        data_offer_termination_notification_uri=body.dataOfferTerminationNotificationUri,
    )
    db.add(offer)
    db.commit()
    return {"offerId": str(offer.offer_id), "committedMethod": offer.data_delivery_method_committed}


@app.get("/offers/{offer_id}")
def get_data_offer(offer_id: uuid.UUID, db: Session = Depends(get_session)):
    # One offer. 404 DATA_OFFER_NOT_FOUND. Read only.
    offer = db.get(DataOffer, offer_id)
    if offer is None:
        raise framework_error(FrameworkError.DATA_OFFER_NOT_FOUND, detail="no such data offer")
    return _offer_view(offer)


@app.delete("/offers/{offer_id}", status_code=204)
def terminate_data_offer(offer_id: uuid.UUID, db: Session = Depends(get_session)):
    """Section 3.5: drain in-flight notifications before tearing down —
    Phase 1, 'drain' means simply not accepting new availability
    notifications for this offer once termination starts; a real queue
    drain is future work, flagged rather than silently skipped.
    """
    offer = db.get(DataOffer, offer_id)
    if offer is None:
        return
    termination_uri = offer.data_offer_termination_notification_uri
    db.delete(offer)
    enqueue(db, termination_uri, {"dataOfferId": str(offer_id)})  # normal direction; sent once the deletion has committed
    db.commit()


@app.post("/offers/{offer_id}/notify", status_code=204)
def offer_data_availability(offer_id: uuid.UUID, body: dict, db: Session = Depends(get_session)):
    """REVERSED direction (section 3.5) — the Producer rApp calls THIS
    endpoint to tell the framework its offered data is ready. Every other
    DME notification flows the opposite way.
    """
    offer = db.get(DataOffer, offer_id)
    if offer is None:
        raise framework_error(FrameworkError.DME_TYPE_VERSION_CONFLICT, detail="no such offer")
    # Phase 1: framework pulls/receives here — deferred to the actual pull/push
    # transport handler (dme-pull/dme-push routes), this endpoint just acks.


def _producers_for_type(db: Session, dme_type_id: uuid.UUID) -> list[DMEProducer]:
    """ICS's own InfoProducers.getProducersSupportingType — real ICS
    fans job start/stop out to every producer currently registered for a
    type (ProducerCallbacks.startInfoSubscriptionJob/stopInfoJob), not
    just one; confirmed by reading that source directly, not assumed.
    """
    stmt = select(DMEProducer).join(DMEProducerType, DMEProducerType.producer_id == DMEProducer.producer_id).where(
        DMEProducerType.dme_type_id == dme_type_id)
    return list(db.scalars(stmt).all())


def _push_job_to_producers(db: Session, dme_type: DMEType, job: DataJob) -> None:
    """HISTORY.md §5: no job push to producers existed at
    all — create_data_job/terminate_data_job only ever touched our own
    DB. ICS's own ProducerCallbacks.startInfoJob POSTs the job to every
    producer supporting the type (jobCallbackUrl, ProducerJobInfo's wire
    shape); best-effort per producer, same pattern as every other
    DME/FOCOM notification in this build — an unreachable
    producer never fails the consumer-facing call, matching the
    reference's own onErrorResume-and-continue behavior.
    """
    for producer in _producers_for_type(db, dme_type.dme_type_id):
        enqueue(db, producer.job_callback_url, {
            "infoJobIdentity": str(job.data_job_id),
            "infoTypeIdentity": str(dme_type.dme_type_id),
            "infoJobData": job.production_job_definition or {},
            "targetUri": (job.delivery_details or {}).get("targetUri", ""),
            "owner": job.consumer_id,
            "lastUpdated": datetime.datetime.now(datetime.UTC).isoformat(),
        })


def _stop_job_at_producers(db: Session, dme_type: DMEType, data_job_id: uuid.UUID) -> None:
    """ICS's own ProducerCallbacks.stopInfoJob — DELETE to every
    supporting producer's jobCallbackUrl/{jobId}. A DELETE row in the transactional outbox (MSG-1.10): it exists exactly when the job's
    deletion does, and survives a crash after the commit; the caller commits.
    """
    for producer in _producers_for_type(db, dme_type.dme_type_id):
        enqueue(db, f"{producer.job_callback_url}/{data_job_id}", {}, method="DELETE")


def _now() -> datetime.datetime:
    """The current time, timezone-aware UTC (one place, so a test can move it)."""
    return datetime.datetime.now(datetime.UTC)


def _set_late_after(job: DataJob) -> None:
    """GUI-9.8: sets when the job turns LATE: two declared intervals after its last delivery or, before the first one, after now (the job was just
    declared or redeclared). None when the job declares no interval. Two intervals, not one, so one delivery that is a little late is not an alarm."""
    if job.expected_interval_seconds is None:
        job.late_after = None
        return
    since = as_utc(job.last_delivery_at) if job.last_delivery_at is not None else _now()
    job.late_after = since + datetime.timedelta(seconds=2 * job.expected_interval_seconds)


def _job_view(j: DataJob) -> dict:
    """The wire form of a data job. GUI-9.8 keys: `expectedIntervalSeconds`, `lastDeliveryAt` (null until a producer delivered a record) and `late`
    (null when no interval is declared; else whether two intervals passed since the last delivery, or since the job was declared before any)."""
    return {
        "dataJobId": str(j.data_job_id),
        "dataDeliveryMode": j.data_delivery_mode,
        "dmeTypeId": str(j.dme_type_id),
        "productionJobDefinition": j.production_job_definition or {},
        "dataDeliveryMethod": j.data_delivery_method,
        "deliveryDetails": j.delivery_details or {},
        "consumerId": j.consumer_id,
        "status": j.status,
        "lifecycleStage": j.lifecycle_stage,
        "expectedIntervalSeconds": j.expected_interval_seconds,
        "lastDeliveryAt": as_utc(j.last_delivery_at).isoformat() if j.last_delivery_at else None,
        "late": None if j.late_after is None else as_utc(j.late_after) < _now(),
    }


def _offer_view(o: DataOffer) -> dict:
    """One data offer as the JSON the routes return: the methods offered, the committed one and the two notification addresses.
    """
    return {
        "offerId": str(o.offer_id),
        "dmeTypeId": str(o.dme_type_id),
        "dataDeliveryMethodsOffered": o.data_delivery_methods_offered,
        "committedMethod": o.data_delivery_method_committed,
        "dataAvailabilityNotificationUri": o.data_availability_notification_uri,
        "dataOfferTerminationNotificationUri": o.data_offer_termination_notification_uri,
    }


def _type_view(db: Session, t: DMEType) -> dict:
    """One type as the JSON the routes return, with the sorted ids of its producers and its status, which is computed now by calling the producers' health callbacks (see `_computed_type_status`).
    """
    producer_ids = sorted(db.scalars(select(DMEProducerType.producer_id).where(DMEProducerType.dme_type_id == t.dme_type_id)).all())
    return {
        "dmeTypeId": str(t.dme_type_id),
        "dmeTypeIdStruct": t.dme_type_id_struct,
        "typeName": t.type_name,
        "producerIds": producer_ids,
        "typeStatus": _computed_type_status(db, t),  # ADOPT from ICS, section 3.4
        "sourceDomain": t.source_domain,
        "sourceContext": t.source_context,
    }


def _producer_view(db: Session, p: DMEProducer) -> dict:
    """One producer as the JSON the routes return, with the sorted ids of the types it supports."""
    type_ids = sorted(str(i) for i in db.scalars(select(DMEProducerType.dme_type_id).where(DMEProducerType.producer_id == p.producer_id)).all())
    return {
        "producerId": p.producer_id,
        "producerHealthCallbackUrl": p.producer_health_callback_url,
        "jobCallbackUrl": p.job_callback_url,
        "supportedTypeIds": type_ids,
    }


def _computed_type_status(db: Session, t: DMEType) -> str:
    """HISTORY.md §5: this used to check only whether a DataJob
    row was ACTIVE — a dead producer with an active job still reported
    ENABLED, and producerHealthCallbackUrl was stored but never actually
    called. ICS's own typeStatus (ConsumerController.typeStatus) is
    ENABLED if ANY producer supporting the type is available
    (`for (InfoProducer producer : infoProducers.getProducersSupportingType(type))`),
    DISABLED otherwise (confirmed by reading that source directly) — this
    mirrors that signal — computed live at read time rather than via a
    background scheduler, since no scheduler exists anywhere in this
    build (elided, same as the real PM file-collection pipeline
    elsewhere).
    """
    return "ENABLED" if any(_producer_is_healthy(p.producer_health_callback_url) for p in _producers_for_type(db, t.dme_type_id)) else "DISABLED"


def _producer_is_healthy(callback_url: str) -> bool:
    resp = get_webhook(callback_url, timeout=2.0)
    return resp is not None and resp.status_code < 300


# ---------------------------------------------------------------- list reads (GUI pass 2)
# Data jobs and offers were only readable by id, so nobody could see which
# consumers were pulling which types, or what producers had offered
# (call flow 01).

@app.get("/data-jobs")
def list_data_jobs(dme_type_id: uuid.UUID | None = None, consumer_id: str | None = None, late: bool | None = None, limit: int = PageLimit,
                   offset: int = PageOffset, db: Session = Depends(get_session)):
    """The data jobs, paginated, optionally of one type and one consumer. GUI-9.8: `late=true` keeps the jobs that are LATE (no delivery for two of
    their declared intervals), `late=false` the jobs that declare an interval and are on time; a job that declares none matches neither."""
    stmt = select(DataJob)
    if dme_type_id:
        stmt = stmt.where(DataJob.dme_type_id == dme_type_id)
    if consumer_id:
        stmt = stmt.where(DataJob.consumer_id == consumer_id)
    if late is not None:
        # `late_after` is kept by `_set_late_after`, so LATE is one comparison with now (no per-row interval arithmetic in SQL)
        stmt = stmt.where(DataJob.late_after < _now()) if late else stmt.where(DataJob.late_after >= _now())
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_job_view(j) for j in page["items"]]}


@app.get("/offers")
def list_data_offers(dme_type_id: uuid.UUID | None = None, limit: int = PageLimit, offset: int = PageOffset,
                      db: Session = Depends(get_session)):
    # The data offers as a page, optionally only those of one type. Read only.
    stmt = select(DataOffer)
    if dme_type_id:
        stmt = stmt.where(DataOffer.dme_type_id == dme_type_id)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_offer_view(o) for o in page["items"]]}


# ---------------------------------------------------------------- Wave 3: real data-plane store
# docs/ARCHITECTURE.md (DME) — previously DME only ever brokered
# job/offer metadata; a producer's actual payload never had anywhere to
# land inside DME itself. Serves both rApp and MDAF consumers, no
# distinction at this layer (docs/ARCHITECTURE.md's DME "two
# paths, not one" — this is the data path, reachable by either).

@app.post("/data-jobs/{data_job_id}/records", status_code=201)
def ingest_data_record(data_job_id: uuid.UUID, body: DataRecordRequest, db: Session = Depends(get_session)):
    # Stores one payload for the job. 404 DATA_JOB_NOT_FOUND; 201 with the record id. The caller is not checked against the job's producers: who may call is decided at the gateway.
    job = db.get(DataJob, data_job_id)
    if job is None:
        raise framework_error(FrameworkError.DATA_JOB_NOT_FOUND, detail="no such data job")
    record = DataRecord(data_job_id=data_job_id, payload=body.payload)
    db.add(record)
    job.last_delivery_at = _now()                # GUI-9.8: the delivery the job's `late` is measured from
    _set_late_after(job)
    db.commit()
    return {"recordId": str(record.record_id)}


@app.get("/data-jobs/{data_job_id}/records")
def fetch_data_records(data_job_id: uuid.UUID, limit: int = PageLimit, offset: int = PageOffset,
                        db: Session = Depends(get_session)):
    # The job's records as a page, newest first. 404 DATA_JOB_NOT_FOUND. Read only.
    job = db.get(DataJob, data_job_id)
    if job is None:
        raise framework_error(FrameworkError.DATA_JOB_NOT_FOUND, detail="no such data job")
    stmt = select(DataRecord).where(DataRecord.data_job_id == data_job_id).order_by(DataRecord.produced_at.desc())
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_record_view(r) for r in page["items"]]}


def _record_view(r: DataRecord) -> dict:
    return {"recordId": str(r.record_id), "dataJobId": str(r.data_job_id), "payload": r.payload,
            "producedAt": r.produced_at.isoformat()}


# ---------------------------------------------------------------- Wave 3: O1 action mediation
# docs/ARCHITECTURE.md (DME) — DME does not speak NETCONF/RESTCONF
# itself; ran-nf-oam already does (netconf_client.py's edit-config RPCs
# against its own real ManagedEntity/O1AdaptorEndpoint registry). This
# route records an rApp's AI/ML decision with its source provenance,
# then forwards to ran-nf-oam's existing POST /config-jobs — a thin
# mediation layer, not a second implementation of O1 dispatch.

DME_TO_RAN_NF_OAM_TIMEOUT_SECONDS = 10.0


@app.post("/actions", status_code=202, responses={200: {"description": "the actionId was already recorded: the replay is IGNORED and nothing is forwarded again"}})
def mediate_action(body: ActionRequest, db: Session = Depends(get_session)):
    # Records an rApp's O1 change request and forwards it to RAN NF OAM as a configuration job (10 s limit). Order: empty `changes` is 422; an `actionId` already recorded is answered 200 `IGNORED` with the
    # original status and nothing is forwarded again; otherwise the record is committed first (status FORWARDED, its default), then the job is requested. RAN NF OAM's answer decides the status: a body that is
    # not JSON or a 5xx is 502 UPSTREAM_FAILED and the record becomes REJECTED; a 4xx is relayed as it came and the record becomes REJECTED; an answer with no job id (the change waits for human approval) is 202 with the
    # status and `approvalId` and no forwarded job; otherwise 202 with the job id and RAN NF OAM's status. A transport failure raised by the call is not caught here, so the record then keeps the
    # status FORWARDED.
    if not body.changes:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="changes must not be empty")
    if body.actionId is not None:
        existing = db.get(DmeActionRecord, body.actionId)
        if existing is not None:
            # Wave 10.1 (W10-18, TC29): a replay of an action already
            # mediated — ignored, the original outcome reported back.
            return JSONResponse(status_code=200, content={
                "actionId": str(existing.action_id), "status": "IGNORED", "originalStatus": existing.status,
                "forwardedJobId": str(existing.forwarded_job_id) if existing.forwarded_job_id else None})
    first = body.changes[0]
    record = DmeActionRecord(
        action_id=body.actionId or uuid.uuid4(),
        requested_by=body.requestedBy,
        managed_element_ref=first.get("managedElementRef") or "",
        class_name=first.get("className"),
        changes=body.changes,
        source_context=body.sourceContext,
        # Wave 10.1 (W10-23): the inbound request's X-Correlation-ID, so an
        # action joins the audit trail of the decision that caused it.
        correlation_id=get_correlation_id(),
    )
    db.add(record)
    db.commit()    # the record is committed before RAN NF OAM is called, so it exists whatever the call does; its status is updated after the answer
    # Wave 9 (W9-02): className is forwarded too — RAN NF OAM's write
    # pre-check validates each change against its vendor's data model.
    # Wave 10.1 (W10-19): DME → RAN NF OAM is bounded at 10 s.
    decision = {**body.decision} if body.decision else None
    if body.actionId is not None and decision is not None:
        decision.setdefault("actionId", str(body.actionId))
    resp = _r1.post("/ran-nf-oam/config-jobs", json={
        "requestedBy": body.requestedBy, "scope": body.scope, "msacRole": body.msacRole, "changes": body.changes,
        **({"decision": decision} if decision is not None else {}),
    }, timeout=DME_TO_RAN_NF_OAM_TIMEOUT_SECONDS)
    try:
        forwarded = resp.json()
    except ValueError:
        forwarded = None
    if forwarded is None or resp.status_code >= 500:
        record.status = "REJECTED"
        db.commit()
        raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"RAN NF OAM answered {resp.status_code} without a usable body")
    if resp.status_code >= 400:
        # Refused at the pre-check (unsupported MnS service, schema
        # violation, MSAC): the rApp gets RAN NF OAM's own 4xx directly,
        # and the action is recorded as REJECTED rather than forwarded.
        record.status = "REJECTED"
        db.commit()
        raise HTTPException(status_code=resp.status_code, detail=forwarded.get("detail"))
    if forwarded.get("jobId") is None:
        # PR-AI-11: RAN NF OAM kept the write for a human to approve (status PENDING_APPROVAL): there is no job yet. The action is recorded as waiting;
        # the rApp follows `approvalId` at RAN NF OAM (GET /rapp-approvals/{id}) for the outcome and the job id.
        record.status = forwarded["status"]
        db.commit()
        return {"actionId": str(record.action_id), "forwardedJobId": None, "status": record.status, "approvalId": forwarded.get("approvalId")}
    record.forwarded_job_id = uuid.UUID(forwarded["jobId"])
    record.status = forwarded["status"]
    db.commit()
    return {"actionId": str(record.action_id), "forwardedJobId": forwarded["jobId"], "status": record.status}


def _visible_elements(scope: authz_scope.Scope) -> set[str]:
    """PR-SEC-10.7: the managed elements a caller with `scope` may touch, asked of the module that owns them (RAN NF OAM; DME holds no element, region or tenant).
    `R1Client` passes the rApp's claim on with the call (`X-R1-On-Behalf-Scope`), so RAN NF OAM answers with the elements inside it. Fails closed: if the claim could not be
    passed on (it would be read as no claim, and the answer would be every element), or RAN NF OAM does not answer, the caller is shown nothing and told why."""
    if authz_scope.get_originator_scope() != scope:
        return set()
    refs: set[str] = set()
    offset = 0
    while True:
        resp = _r1.get("/ran-nf-oam/managed-entities", params={"limit": 500, "offset": offset})
        if resp.status_code != 200:
            raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"RAN NF OAM answered {resp.status_code}: the caller's scope cannot be applied to the actions")
        page = resp.json()
        refs.update(item["managedElementRef"] for item in page["items"])
        offset += len(page["items"])
        if not page["items"] or offset >= page["total"]:
            return refs


def _named_elements(a: DmeActionRecord) -> set[str]:
    """Every managed element an action names: the one the record is filed under and each change's own."""
    names = {a.managed_element_ref} | {c.get("managedElementRef") for c in a.changes if isinstance(c, dict)}
    return {n for n in names if isinstance(n, str) and n}


def _within(a: DmeActionRecord, visible: set[str]) -> bool:
    """Whether a caller who may touch `visible` may see the action: it names at least one element, and every element it names is one of them (the action is
    one request: one element outside is a view of a change to it). An action that names no element has no region or tenant to be inside, so it is not shown."""
    named = _named_elements(a)
    return bool(named) and named <= visible


@app.get("/actions/{action_id}")
def get_action(action_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """One action. PR-SEC-10.7: for a caller with a scope claim, one that names an element outside it is a 404, as if there were no such action."""
    record = db.get(DmeActionRecord, action_id)
    scope = authz_scope.request_scope(request)
    if record is None or (scope is not None and not _within(record, _visible_elements(scope))):
        raise framework_error(FrameworkError.DME_ACTION_NOT_FOUND, detail="no such action")
    return _action_view(record)


@app.get("/actions")
def list_actions(request: Request, managed_element_ref: str | None = None, requested_by: str | None = None, limit: int = PageLimit,
                  offset: int = PageOffset, db: Session = Depends(get_session)):
    """The recorded actions. PR-SEC-10.7: a caller with a scope claim sees the actions all of whose elements are inside it (filtered, never refused; `total` counts what
    it may see). DME has no region or tenant to match, so the elements inside the claim are asked of RAN NF OAM (`GET /ran-nf-oam/managed-entities`, which applies
    the claim it is passed); the cost is that call for each list, in pages of 500. An unscoped caller (an SMO module, the operator's GUI, an rApp with no claim) asks nothing
    and sees every action, as before."""
    stmt = select(DmeActionRecord)
    if managed_element_ref:
        stmt = stmt.where(DmeActionRecord.managed_element_ref == managed_element_ref)
    if requested_by:
        stmt = stmt.where(DmeActionRecord.requested_by == requested_by)
    scope = authz_scope.request_scope(request)
    if scope is None:
        page = paginate(db, stmt, limit, offset)
        return {**page, "items": [_action_view(a) for a in page["items"]]}
    visible = _visible_elements(scope)
    shown = [a for a in db.scalars(stmt.order_by(DmeActionRecord.action_id)) if _within(a, visible)]
    page = paginate_list(shown, limit, offset)
    return {**page, "items": [_action_view(a) for a in page["items"]]}


def _action_view(a: DmeActionRecord) -> dict:
    return {"actionId": str(a.action_id), "requestedBy": a.requested_by, "managedElementRef": a.managed_element_ref,
            "className": a.class_name, "changes": a.changes, "sourceContext": a.source_context,
            "forwardedJobId": str(a.forwarded_job_id) if a.forwarded_job_id else None, "status": a.status,
            "correlationId": a.correlation_id, "createdAt": a.created_at.isoformat()}
