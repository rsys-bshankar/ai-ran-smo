"""NFO (Network Function Orchestrator): deployment descriptors and the lifecycle of NF deployments, as the O2-DMS-style
service of the SMO.

Where it sits: one FastAPI app behind R1 Termination at `/nfo`. Callers are AIMgF (model runtimes), rApp Management, SO SMOS,
SA SMOS (heal), Onboarding (descriptors) and the GUI. It makes one outgoing call: `GET /focom/inventory` in `instantiate`,
to learn the O-Cloud id for placement. The design records are `HISTORY.md` §5 (the deployment state machine and guards) and
`OI-3-nfo-abnormal` (asynchronous terminate); the NFO + FOCOM LLD sections 2 and 4 describe the flows.

What it owns: descriptors, deployments and their state (`statemachine.py`), the operation history and the resource link
rows. It deploys nothing for real: Instantiate, Scale and the default Terminate complete in the same request instead of
driving Helm (the "Phase 1 elision" comments mark each place). The deployment manager reports how an asynchronous
terminate ends through `receive_dms_notification`.

Before editing: every state change goes through `NFO_FSM.fire`, so a new transition belongs in `statemachine.py` first. Each `fire` is given the
deployment (`deployment=`), so the transition is published on the event bus as `org.oran.smo.nf-deployment.state-changed` (call flow 15e).
`NFDeployment` is versioned, so two concurrent writers give one of them a 409 CONCURRENT_MODIFICATION. The docstrings of
the routes and request models are published in `docs/openapi/nfo.json`.
"""

import uuid
from typing import Literal

import httpx

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared import events
from smo_shared.r1_client import R1Client
from smo_shared.statemachine import IllegalTransition
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id
from smo_shared.pagination import PageLimit, PageOffset, paginate
from smo_shared.versioning import install_concurrency_handler
from smo_shared.idempotency import idempotent

from .models import LCMOperation, NFDeployment, NFDeploymentDescriptor, NFOCloudResource
from .statemachine import DeploymentEvent, DeploymentState, NFO_FSM

app = FastAPI(title="NFO SMOS (O2dms)")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
install_concurrency_handler(app)  # a stale write (PR-ST-2) is a 409, not a 500
apply_r1_gateway_security(app)
apply_correlation_id(app)


install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)


# Body of `POST /deployments`: the descriptor to deploy, a unique deployment name and an optional resource type used for the FOCOM inventory lookup.
class InstantiateRequest(BaseModel):
    nfDeploymentDescriptorId: uuid.UUID
    name: str
    requiredResourceTypeId: str | None = None


# Body of `POST /descriptors`; `packageId` is absent for a descriptor created for a model runtime, which has no onboarded package.
class CreateDescriptorRequest(BaseModel):
    packageId: uuid.UUID | None = None
    name: str
    workloadTemplate: dict = {}
    requiredResourceTypeId: str | None = None


@app.post("/descriptors", status_code=201)
def create_descriptor(body: CreateDescriptorRequest, db: Session = Depends(get_session)):
    """CreateDescriptor — NFO+FOCOM LLD section 2: an NFDeploymentDescriptor
    derived from an onboarded package's TOSCA Definitions/, called from
    Onboarding's OnboardPackage flow once validation succeeds, closing the
    gap where nfDeploymentDescriptorId previously referenced nothing
    concrete. `packageId` is optional since Wave 2 (AI Platform Service
    Decomposition, docs/ARCHITECTURE.md (AIMgF)): AIMgF's own Runtime
    Lifecycle now also creates a descriptor per model runtime, and a
    model runtime has no onboarded ApplicationPackage behind it — every
    package-derived descriptor (Onboarding's own flow, unchanged) still
    always sets it.
    """
    # Stores a descriptor and returns its id (201). No validation beyond the body's shape: the descriptor is taken as given and
    # `packageId` is not checked against Onboarding (it is a cross-module reference, see `models.py`).
    descriptor = NFDeploymentDescriptor(
        package_id=body.packageId, name=body.name,
        required_resource_type_id=body.requiredResourceTypeId,
        workload_template=body.workloadTemplate,
    )
    db.add(descriptor)
    db.commit()
    return {"nfDeploymentDescriptorId": str(descriptor.nf_deployment_descriptor_id)}


@app.delete("/descriptors/{descriptor_id}", status_code=204)
def delete_descriptor(descriptor_id: uuid.UUID, db: Session = Depends(get_session)):
    """DeleteDescriptor — the counterpart of CreateDescriptor, called by Onboarding when its package is deleted (call flow 06c, 06d).

    204 once the descriptor is gone, also for an unknown id (idempotent). 409 NFDEPLOYMENT_DESCRIPTOR_ALREADY_DEPLOYED while a deployment still uses
    it: terminate that deployment first.
    """
    # The deployment check is the same SELECT Instantiate's duplication guard runs; a terminated deployment is removed (`_remove_deployment`), so only a
    # live one, or one still TERMINATING or ABNORMAL, refuses the delete.
    descriptor = db.get(NFDeploymentDescriptor, descriptor_id)
    if descriptor is None:
        return
    if db.scalar(select(NFDeployment).where(NFDeployment.nf_deployment_descriptor_id == descriptor_id).limit(1)) is not None:
        raise framework_error(FrameworkError.NFDEPLOYMENT_DESCRIPTOR_ALREADY_DEPLOYED,
                              detail=f"NfDeploymentDescriptor {descriptor_id} is used by a deployment")
    db.delete(descriptor)
    db.commit()


@app.post("/deployments", status_code=202)
@idempotent("nfo", status_code=202)
def instantiate(body: InstantiateRequest, request: Request, db: Session = Depends(get_session)):
    """Instantiate — NFO+FOCOM LLD section 4: FOCOM's inventory is queried
    to resolve clusterId before the workload is placed, rather than the
    Phase 1 degenerate cluster being assumed implicitly. HISTORY.md §5: now also enforces the reference's own real guards before
    creating anything — _check_dependencies (the descriptor must exist)
    and _check_duplication (no two deployments may share a name). A
    descriptor may back any number of deployments (call flow 15b). 503
    when FOCOM does not name the O-Cloud to place it on.
    """
    # Creates a deployment and, in this request, takes it to RUNNING. Checks in order: descriptor exists (422
    # NFDEPLOYMENT_DESCRIPTOR_NOT_FOUND), name unused (409 NFDEPLOYMENT_NAME_CONFLICT, a SELECT, not a unique constraint). Then the FOCOM
    # inventory is asked for the O-Cloud id (`_placement`: 503 ENDPOINT_UNREACHABLE and nothing created when FOCOM does not name one). Writes
    # the deployment, an INSTANTIATE operation (COMPLETED) and one resource link in a single commit, with its state changes on the event bus. Repeating the call with the same `Idempotency-Key` header
    # returns the first answer.
    if db.get(NFDeploymentDescriptor, body.nfDeploymentDescriptorId) is None:
        raise framework_error(FrameworkError.NFDEPLOYMENT_DESCRIPTOR_NOT_FOUND,
                               detail=f"no such NFDeploymentDescriptor: {body.nfDeploymentDescriptorId}")
    if db.scalar(select(NFDeployment).where(NFDeployment.name == body.name)) is not None:
        raise framework_error(FrameworkError.NFDEPLOYMENT_NAME_CONFLICT,
                               detail=f"NfDeployment with name {body.name} exists already")
    # Call flow 15b (DOC-CF-1.4): a descriptor is a template and may back any number of deployments (one per rApp instance of a package);
    # the reference's "deployed once" guard is not kept. Deleting the descriptor is still refused while any of them exists.
    cluster_id = _placement(body.requiredResourceTypeId)

    deployment = NFDeployment(
        nf_deployment_descriptor_id=body.nfDeploymentDescriptorId,
        name=body.name,
        cluster_id=cluster_id,
        required_resource_type_id=body.requiredResourceTypeId,
        state=DeploymentState.INITIAL,
    )
    db.add(deployment)
    db.flush()
    # Added in INITIAL and flushed (which assigns the id the operation row below needs), then moved through INSTANTIATING to
    # RUNNING in this request. The intermediate states are never committed, so no caller can observe them.
    deployment.state = NFO_FSM.fire(DeploymentState.INITIAL, DeploymentEvent.INSTANTIATE, deployment=deployment)
    op = LCMOperation(nf_deployment_id=deployment.nf_deployment_id, operation_type="INSTANTIATE", status="IN_PROGRESS")
    db.add(op)

    # Phase 1: meaningfully real (docker run --gpus passthrough when
    # requiredResourceTypeId is set) — elided here; deployment moves to
    # RUNNING once the container starts.
    deployment.state = NFO_FSM.fire(deployment.state, DeploymentEvent.INSTANTIATE_COMPLETE, deployment=deployment)
    op.status = "COMPLETED"
    db.add(NFOCloudResource(nf_deployment_id=deployment.nf_deployment_id, resource_ref=cluster_id))
    db.commit()
    return {"nfDeploymentId": str(deployment.nf_deployment_id), "state": deployment.state, "clusterId": cluster_id}


def _placement(resource_type: str | None) -> str:
    """The O-Cloud id FOCOM's inventory names for the deployment (`GET /focom/inventory?resource_type=`, its `oCloudId`). Call flow 15b: 503
    ENDPOINT_UNREACHABLE when FOCOM does not answer, answers an error or names none, so a workload is never placed on a cluster nobody named."""
    try:
        resp = R1Client().get("/focom/inventory", params={"resource_type": resource_type or ""})   # the unit tests patch `R1Client.get`
    except httpx.HTTPError:
        resp = None
    cloud = resp.json().get("oCloudId") if resp is not None and resp.status_code == 200 else None
    if not cloud:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail="FOCOM did not name an O-Cloud to place the deployment on; nothing was created")
    return cloud


@app.delete("/deployments/{nf_deployment_id}", status_code=204)
def terminate(nf_deployment_id: uuid.UUID, async_uninstall: bool = False, db: Session = Depends(get_session)):
    """Terminate — mirrors the reference's own state dispatch
    (lcm_nfdeployment_uninstall, dms_lcm_nfdeployment.py): INITIAL/
    ABNORMAL go straight to DELETING (no chart was ever installed, or it's
    already broken); every in-flight/running state is uninstalled first
    (TERMINATING); calling Terminate again on an already-TERMINATING
    deployment is a no-op (the reference's own `elif ... Uninstalling:
    pass`); calling it on one already DELETING flips it to ABNORMAL instead
    of deleting twice — the reference's own defensive catch-all.

    OI-3-nfo-abnormal: by default the Phase 1 elision completes the
    uninstall and the deletion at once — TERMINATING -> DELETING -> the
    record removed — as every SMO caller (rApp Management, AIMgF, SO SMOS)
    expects: 204, and the descriptor is free to deploy again.
    `async_uninstall=true` instead leaves the deployment TERMINATING (or
    DELETING) with its TERMINATE operation IN_PROGRESS and answers 202: the
    deployment manager reports how it ends through
    `POST /deployments/{id}/dms-notifications`, which is how DELETING and
    ABNORMAL are reached and observed.
    """
    # Removes a deployment (204) or starts removing it (202 with `async_uninstall=true`). An unknown id is 204 as well (idempotent).
    # What happens depends on the state: INITIAL and ABNORMAL go straight to DELETING; INSTANTIATING, RUNNING and UPDATING go to
    # TERMINATING first; TERMINATING again is a no-op; DELETING (a second Terminate racing the first) turns the deployment ABNORMAL
    # and records a FAILED TERMINATE operation. Synchronously the uninstall and the deletion both complete here and the whole
    # deployment, its resource links and its operation history are removed in one commit. With `async_uninstall` the deployment
    # stays TERMINATING (or DELETING) with a TERMINATE operation IN_PROGRESS, and `receive_dms_notification` finishes it.
    d = db.get(NFDeployment, nf_deployment_id)
    if d is None:
        return
    # Read before the transition fires: TERMINATING + TERMINATE is a legal self-edge in the state machine, so the new state
    # alone cannot tell a repeat from a first call.
    was_terminating = d.state == DeploymentState.TERMINATING
    new_state = NFO_FSM.fire(DeploymentState(d.state), DeploymentEvent.TERMINATE, deployment=d)
    if new_state == DeploymentState.ABNORMAL:
        db.add(LCMOperation(nf_deployment_id=nf_deployment_id, operation_type="TERMINATE", status="FAILED"))
        d.state = new_state
        d.abnormal_reason = "TERMINATE while DELETING"
        db.commit()
        return _accepted(d) if async_uninstall else None
    if was_terminating:
        return _accepted(d) if async_uninstall else None
    d.state = new_state
    if async_uninstall:
        db.add(LCMOperation(nf_deployment_id=nf_deployment_id, operation_type="TERMINATE", status="IN_PROGRESS"))
        db.commit()
        return _accepted(d)
    # Phase 1 elision: the uninstall (Helm) and the resource release both
    # complete synchronously.
    if d.state == DeploymentState.TERMINATING:
        d.state = NFO_FSM.fire(DeploymentState(d.state), DeploymentEvent.UNINSTALL_COMPLETE, deployment=d)
    db.add(LCMOperation(nf_deployment_id=nf_deployment_id, operation_type="TERMINATE", status="COMPLETED"))
    _remove_deployment(db, d)
    db.commit()


def _accepted(d: NFDeployment) -> JSONResponse:
    """The 202 body of an asynchronous terminate (id and current state)."""
    return JSONResponse(status_code=202, content={"nfDeploymentId": str(d.nf_deployment_id), "state": d.state})


def _remove_deployment(db: Session, d: NFDeployment) -> None:
    """Deletes a deployment's resource links, its operation history and the deployment itself, in the caller's transaction
    (the caller commits). Both child tables have foreign keys to the deployment, so they go first. The end is published on the event bus
    (`DELETING` to `DELETED`, event `DELETE_COMPLETE`; call flow 15e): it is not a state machine transition, the deployment is gone.
    """
    events.publish(db, "nf-deployment", "state-changed", "/smo/nfo", str(d.nf_deployment_id),
                   {"machine": "DeploymentState", "from": str(d.state), "event": "DELETE_COMPLETE", "to": "DELETED"})
    db.query(NFOCloudResource).filter_by(nf_deployment_id=d.nf_deployment_id).delete()
    # LCMOperation.nf_deployment_id has a real FK, same as NFOCloudResource
    # above — deleting the deployment without clearing its own operation
    # history (including a TERMINATE row just added) violates it.
    # `Query.delete()` issues its DELETE immediately against the database,
    # and this session is `autoflush=False` (smo_shared/db.py), so it
    # never sees a pending, unflushed TERMINATE row — only a `flush()`
    # first makes it visible to the very next statement. Caught running
    # a genuine deploy -> terminate sequence against real Postgres —
    # SQLite's test harness doesn't enforce FK constraints by default.
    db.flush()
    db.query(LCMOperation).filter_by(nf_deployment_id=d.nf_deployment_id).delete()
    db.delete(d)


# Body of `POST /deployments/{id}/dms-notifications`: what the deployment manager reports, with an optional reason for a failure.
class DmsNotification(BaseModel):
    """OI-3-nfo-abnormal: the deployment manager's (O2 DMS) report on a
    deployment. `detail` says why, for a failure."""
    event: Literal["UNINSTALL_COMPLETE", "UNINSTALL_FAILED", "DELETE_COMPLETE", "DELETE_FAILED", "RUNTIME_FAILURE"]
    detail: str | None = None


@app.post("/deployments/{nf_deployment_id}/dms-notifications")
def receive_dms_notification(nf_deployment_id: uuid.UUID, body: DmsNotification, db: Session = Depends(get_session)):
    """OI-3-nfo-abnormal — how an asynchronous Terminate ends, and how a
    broken workload is reported:

    - `UNINSTALL_COMPLETE`: TERMINATING -> DELETING (resources being released);
    - `DELETE_COMPLETE`: DELETING -> the deployment is removed (answers `state: DELETED`);
    - `UNINSTALL_FAILED` / `DELETE_FAILED`: -> ABNORMAL, the TERMINATE operation FAILED;
    - `RUNTIME_FAILURE`: INSTANTIATING / RUNNING / UPDATING -> ABNORMAL.

    An ABNORMAL deployment keeps the reason in `abnormalReason`; Heal
    recovers it (-> RUNNING) and Terminate retires it (-> DELETING).
    404 for an unknown deployment, 409 NFDEPLOYMENT_ILLEGAL_OPERATION for an
    event its state can't take.
    """
    # Applies one report of the deployment manager. 404 NFDEPLOYMENT_NOT_FOUND; 409 NFDEPLOYMENT_ILLEGAL_OPERATION when the
    # current state cannot take the event (nothing is changed). DELETE_COMPLETE is handled here and not by the state machine
    # because it ends the deployment: it needs state DELETING and removes the whole deployment. Every other event is a state
    # machine transition; a transition into ABNORMAL stores the reason (`EVENT: detail`) and marks an open TERMINATE operation FAILED.
    # One commit. The caller is not distinguished from any other authenticated R1 caller.
    d = db.get(NFDeployment, nf_deployment_id)
    if d is None:
        raise framework_error(FrameworkError.NFDEPLOYMENT_NOT_FOUND, detail="no such NfDeployment")
    if body.event == "DELETE_COMPLETE":
        if d.state != DeploymentState.DELETING:
            raise framework_error(FrameworkError.NFDEPLOYMENT_ILLEGAL_OPERATION,
                                  detail=f"DELETE_COMPLETE for a deployment in state {d.state}, not DELETING")
        _remove_deployment(db, d)
        db.commit()
        return {"nfDeploymentId": str(nf_deployment_id), "state": "DELETED"}
    try:
        d.state = NFO_FSM.fire(DeploymentState(d.state), DeploymentEvent(body.event), deployment=d)
    except IllegalTransition as exc:
        raise framework_error(FrameworkError.NFDEPLOYMENT_ILLEGAL_OPERATION,
                              detail=f"{body.event} for a deployment in state {d.state}") from exc
    if d.state == DeploymentState.ABNORMAL:
        d.abnormal_reason = f"{body.event}: {body.detail}" if body.detail else body.event
        op = _open_terminate(db, nf_deployment_id)
        if op is not None:
            op.status = "FAILED"
    db.commit()
    return {"nfDeploymentId": str(nf_deployment_id), "state": d.state, "abnormalReason": d.abnormal_reason}


def _open_terminate(db: Session, nf_deployment_id: uuid.UUID) -> LCMOperation | None:
    """The TERMINATE operation of the deployment that is still IN_PROGRESS, or None."""
    return db.scalar(select(LCMOperation).where(LCMOperation.nf_deployment_id == nf_deployment_id,
                                                LCMOperation.operation_type == "TERMINATE",
                                                LCMOperation.status == "IN_PROGRESS"))


@app.get("/deployments/{nf_deployment_id}")
def get_deployment(nf_deployment_id: uuid.UUID, db: Session = Depends(get_session)):
    # Returns one deployment (404 NFDEPLOYMENT_NOT_FOUND); the view includes `abnormalReason` for an ABNORMAL one.
    d = db.get(NFDeployment, nf_deployment_id)
    if d is None:
        raise framework_error(FrameworkError.NFDEPLOYMENT_NOT_FOUND, detail="no such NfDeployment")
    return _deployment_view(d)


@app.post("/deployments/{nf_deployment_id}/heal")
def heal(nf_deployment_id: uuid.UUID, db: Session = Depends(get_session)):
    """HISTORY.md §5: previously a pure stub with no state
    transition of any kind. Self-healing recovery isn't modeled by the
    reference at all (no Heal command exists there); this closes the
    state-transition gap directly: legal from ABNORMAL (genuine
    recovery) or RUNNING (idempotent — already healthy). Real K8s
    pod-health remediation behind it stays out of scope
    (D-DEPLOY-NFO-3, unchanged).
    """
    # Recovers an ABNORMAL deployment to RUNNING (or accepts a RUNNING one as already healthy). 404 NFDEPLOYMENT_NOT_FOUND; 409
    # NFDEPLOYMENT_ILLEGAL_OPERATION from any other state. Clears `abnormalReason` and records a HEAL operation as COMPLETED; no
    # workload is touched. One commit, and the versioned row gives a concurrent writer a 409 CONCURRENT_MODIFICATION.
    d = db.get(NFDeployment, nf_deployment_id)
    if d is None:
        raise framework_error(FrameworkError.NFDEPLOYMENT_NOT_FOUND, detail="no such NfDeployment")
    try:
        d.state = NFO_FSM.fire(DeploymentState(d.state), DeploymentEvent.HEAL, deployment=d)
    except IllegalTransition as exc:
        raise framework_error(FrameworkError.NFDEPLOYMENT_ILLEGAL_OPERATION,
                               detail=f"cannot heal a deployment in state {d.state}") from exc
    d.abnormal_reason = None
    db.add(LCMOperation(nf_deployment_id=nf_deployment_id, operation_type="HEAL", status="COMPLETED"))
    db.commit()
    return {"nfDeploymentId": str(nf_deployment_id), "state": d.state}


@app.post("/deployments/{nf_deployment_id}/scale")
@idempotent("nfo")
def scale(nf_deployment_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """HISTORY.md §5: previously a pure stub with no state
    transition of any kind. Scale is a replica-count change, the same
    conceptual operation as the reference's Update (RUNNING->UPDATING),
    so it drives that same edge — legal only from RUNNING.
    """
    # Takes a RUNNING deployment through UPDATING and back to RUNNING within the request and records a SCALE operation as COMPLETED.
    # It takes no target size and changes no replica count (the Helm upgrade is elided). 404; 409 NFDEPLOYMENT_ILLEGAL_OPERATION
    # unless RUNNING. Repeating with the same `Idempotency-Key` returns the first answer.
    d = db.get(NFDeployment, nf_deployment_id)
    if d is None:
        raise framework_error(FrameworkError.NFDEPLOYMENT_NOT_FOUND, detail="no such NfDeployment")
    try:
        d.state = NFO_FSM.fire(DeploymentState(d.state), DeploymentEvent.UPDATE, deployment=d)
    except IllegalTransition as exc:
        raise framework_error(FrameworkError.NFDEPLOYMENT_ILLEGAL_OPERATION,
                               detail=f"cannot scale a deployment in state {d.state}") from exc
    op = LCMOperation(nf_deployment_id=nf_deployment_id, operation_type="SCALE", status="IN_PROGRESS")
    db.add(op)
    # Phase 1 elision, same pattern as Instantiate: real Helm upgrade
    # (adjusting replica count) happens here; completes synchronously
    # rather than staying observably UPDATING.
    d.state = NFO_FSM.fire(d.state, DeploymentEvent.UPDATE_COMPLETE, deployment=d)
    op.status = "COMPLETED"
    db.commit()
    return {"nfDeploymentId": str(nf_deployment_id), "state": d.state}


@app.get("/deployments/{nf_deployment_id}/resources")
def query_deployment_resources(nf_deployment_id: uuid.UUID, db: Session = Depends(get_session)):
    """The reference's own NfOCloudVResource, genuinely queryable rather
    than just a column nothing ever reads.
    """
    # Lists the resource links of a deployment. An unknown deployment gives an empty list, not a 404.
    rows = db.scalars(select(NFOCloudResource).where(NFOCloudResource.nf_deployment_id == nf_deployment_id)).all()
    return [{"resourceLinkId": str(r.resource_link_id), "resourceRef": r.resource_ref, "vresourceType": r.vresource_type} for r in rows]


@app.get("/operations/{operation_id}")
def query_operation_status(operation_id: uuid.UUID, db: Session = Depends(get_session)):
    # Returns the status of one LCM operation (404 LCM_OPERATION_NOT_FOUND).
    op = db.get(LCMOperation, operation_id)
    if op is None:
        raise framework_error(FrameworkError.LCM_OPERATION_NOT_FOUND, detail="no such LCM operation")
    return {"operationId": str(op.operation_id), "status": op.status}


@app.get("/deployments/{nf_deployment_id}/placement")
def query_cluster_placement(nf_deployment_id: uuid.UUID, db: Session = Depends(get_session)):
    # Returns the cluster (O-Cloud) id a deployment was placed on (404 NFDEPLOYMENT_NOT_FOUND).
    d = db.get(NFDeployment, nf_deployment_id)
    if d is None:
        raise framework_error(FrameworkError.NFDEPLOYMENT_NOT_FOUND, detail="no such NfDeployment")
    return {"nfDeploymentId": str(d.nf_deployment_id), "clusterId": d.cluster_id}


@app.get("/deployments")
def list_deployments(state: str | None = None, limit: int = PageLimit, offset: int = PageOffset,
                      db: Session = Depends(get_session)):
    """List read over NFDeployment — every other deployment route is
    keyed by an id the caller already holds, so there was no way to see
    the workload fleet (or which deployments sit ABNORMAL) at all.
    """
    stmt = select(NFDeployment)
    if state:
        stmt = stmt.where(NFDeployment.state == state)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_deployment_view(d) for d in page["items"]]}


def _deployment_view(d: NFDeployment) -> dict:
    """The JSON of a deployment. `workloadRef` is always None here: nothing in NFO sets it."""
    return {"nfDeploymentId": str(d.nf_deployment_id), "name": d.name, "state": d.state, "clusterId": d.cluster_id,
            "nfDeploymentDescriptorId": str(d.nf_deployment_descriptor_id), "workloadRef": d.workload_ref,
            "requiredResourceTypeId": d.required_resource_type_id, "abnormalReason": d.abnormal_reason}


@app.get("/descriptors")
def list_descriptors(package_id: uuid.UUID | None = None, limit: int = PageLimit, offset: int = PageOffset,
                      db: Session = Depends(get_session)):
    """(GUI pass 2) NFDeploymentDescriptors created by Onboarding's validation
    pipeline, or by AIMgF's own Runtime Lifecycle for a model runtime
    (packageId is None for those — see CreateDescriptorRequest's own docstring).
    """
    stmt = select(NFDeploymentDescriptor)
    if package_id:
        stmt = stmt.where(NFDeploymentDescriptor.package_id == package_id)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"nfDeploymentDescriptorId": str(d.nf_deployment_descriptor_id),
             "packageId": str(d.package_id) if d.package_id else None, "name": d.name,
             "requiredResourceTypeId": d.required_resource_type_id, "workloadTemplate": d.workload_template}
            for d in page["items"]]}


@app.get("/deployments/{nf_deployment_id}/operations")
def list_deployment_operations(nf_deployment_id: uuid.UUID, limit: int = PageLimit, offset: int = PageOffset,
                                db: Session = Depends(get_session)):
    """(GUI pass 2) A deployment's LCM operation history (Instantiate/Heal/Scale/
    Terminate); only a single operation id could be looked up before.
    """
    stmt = select(LCMOperation).where(LCMOperation.nf_deployment_id == nf_deployment_id).order_by(LCMOperation.created_at, LCMOperation.operation_id)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"operationId": str(o.operation_id), "operationType": o.operation_type, "status": o.status}
            for o in page["items"]]}
