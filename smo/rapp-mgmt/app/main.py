"""rApp Management SMOS.

SMO Design v1.3 section 3.5, extended by Onboarding/rApp Mgmt LLD sections
5-6: CreateInstance wired concretely to NFO via the TOSCA service template,
and UpgradeInstance's auto-rollback made precise (upgrade.py).
"""

import logging
import uuid
from contextlib import suppress
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from smo_shared.logconfig import install_logging
from smo_shared.metrics import count_by, install_metrics, register_query_gauge
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error, illegal_transition_error
from smo_shared import roles
from smo_shared.errors import problem
from smo_shared.invoker import INVOKER_ID_HEADER, on_own_account
from smo_shared.webhook import normalise_base_url
from smo_shared import scope as authz_scope
from smo_shared.r1_client import R1Client  # noqa: F401 — the class every R1 call here uses (tests patch it as app.main.R1Client)
from smo_shared.statemachine import IllegalTransition
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id
from smo_shared.pagination import PageLimit, PageOffset, paginate
from smo_shared.versioning import install_concurrency_handler
from smo_shared.idempotency import idempotent
from smo_shared.timeutil import as_utc

from .models import RAppFaultReport, RAppInstance, RAppPerformanceReport
from .provisioning import (DEPLOYABLE_PACKAGE_STATES, apply_approval_policy, apply_rapp_limits, onboarding_status, provision_instance, register_instance_invoker, register_sme_declarations,  # noqa: F401
                           release_instance_resources, deliver_credentials)
from .statemachine import RAPP_INSTANCE_FSM, InstanceEvent, InstanceState
from .upgrade import (current_instance_id, expire_overdue_upgrade, resolve_upgrade, rollback_target, start_rollback,
                      start_upgrade, version_history)

app = FastAPI(title="rApp Management SMOS")
log = logging.getLogger("rapp-mgmt")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
register_query_gauge("smo_rapp_instances", "rApp instances, by lifecycle state (RUNNING are the active rApps).", ["state"],
                     lambda s: count_by(s, RAppInstance.state, InstanceState))  # PR-OBS-4
install_concurrency_handler(app)  # a stale write (PR-ST-2) is a 409, not a 500
apply_r1_gateway_security(app)
apply_correlation_id(app)


install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)


class ApprovalPolicy(BaseModel):
    """What happens to a request nobody decided within `timeoutSeconds` (a minute to a week, default an hour): `EXPIRE` (the default) lapses it,
    `REJECT` has the platform reject it. Neither writes anything; there is no option that approves by itself.

    `requiredApprovals` (default 1, one approval, as before) may be 2: the request then needs two different people to approve it (the first approval keeps it waiting,
    the requester's own never counts, one rejection ends it). Stored and pushed only when it is 2, so a policy that does not use it is exactly what it was."""
    timeoutSeconds: int = Field(default=3600, ge=60, le=604_800)
    onTimeout: Literal["EXPIRE", "REJECT"] = "EXPIRE"
    requiredApprovals: Literal[1, 2] = 1


def _policy_to_store(policy: ApprovalPolicy | None) -> dict | None:
    """The policy as kept on the instance and pushed to RAN NF OAM. `requiredApprovals` is left out while it is 1, so an instance that did not opt into two-person
    approval has the policy it had before the field existed (stored, returned and pushed unchanged)."""
    if policy is None:
        return None
    stored = policy.model_dump()
    if stored["requiredApprovals"] == 1:
        del stored["requiredApprovals"]
    return stored


# The body of CreateInstance. `packageId` is the only required field. `autonomyMode` is fixed here for the life of the instance. `approvalPolicy` is accepted only with ASSIST
# (422 APPROVAL_POLICY_NEEDS_ASSIST). `authzScope` and `operatorApiBase` are checked in the route (422), not by this model, so the answer can use a fixed message that does not echo the input.
class CreateInstanceRequest(BaseModel):
    packageId: uuid.UUID
    config: dict = {}
    # HISTORY.md OI-6.3 — rApp Autonomy Modes: fixed at onboarding
    # (this call), not chosen per-inference-call. SHADOW (no enforcement)
    # is the safe default for every existing caller that doesn't declare
    # one. regionScope only matters for AUTONOMOUS — opaque JSON, the
    # same shape config already is.
    autonomyMode: Literal["AUTONOMOUS", "ASSIST", "SHADOW"] = "SHADOW"
    regionScope: dict | None = None
    # PR-GUI-8: where the new instance's operator API is reached; an operator may give it here, the instance can register it later (PUT .../operator-api)
    operatorApiBase: str | None = None
    # PR-AI-11.4: hold this instance's config jobs for a human to approve (RAN NF OAM's approval queue). Only for ASSIST, the mode in which a person
    # decides: AUTONOMOUS means no one is asked, and a SHADOW instance writes nothing. Without it an instance of any mode behaves as before.
    approvalPolicy: ApprovalPolicy | None = None
    # PR-SEC-10.3 (docs/adr/0005-tenant-region-authorization.md): which managed elements the instance may touch, `{"regions": [...], "tenants": [...]}` (either key
    # optional). Put on the instance's invoker at SME, so every module that owns a target enforces it for this rApp. Absent: unscoped, as before. Not `regionScope`
    # (where an AUTONOMOUS instance's intents go). Checked in the route (422 AUTHZ_SCOPE_INVALID, a fixed message), not by the model, which would repeat the input.
    authzScope: dict | None = None


# The body of UpgradeInstance: the package the replacement instance runs (it must be AVAILABLE or PRIMED, checked when the replacement is provisioned).
class UpgradeRequest(BaseModel):
    newPackageId: uuid.UUID


def _get_or_404(db: Session, instance_id: uuid.UUID) -> RAppInstance:
    inst = db.get(RAppInstance, instance_id)
    if inst is None:
        raise framework_error(FrameworkError.RAPP_INSTANCE_NOT_FOUND, detail=f"no such RAppInstance {instance_id}")
    return inst


def _upgrade_owner(db: Session, inst: RAppInstance) -> RAppInstance | None:
    """The old row of the upgrade `inst` takes part in: itself if it has a
    pending replacement, or the row whose pending replacement it is."""
    if inst.pending_upgrade_instance_id is not None:
        return inst
    return db.scalar(select(RAppInstance).where(RAppInstance.pending_upgrade_instance_id == inst.instance_id))


def _sweep_overdue_upgrade(db: Session, old: RAppInstance) -> bool | None:
    """The lazy upgradeTimeoutSeconds sweep (upgrade.py) for one upgrade, committed on its own.
    True: this request rolled it back. False: nothing was overdue. None: another replica
    ran the same sweep first (the write was stale, PR-ST-2), so it is done either way."""
    try:
        if not expire_overdue_upgrade(db, old):
            return False
        db.commit()
        return True
    except StaleDataError:
        db.rollback()
        return None


def _load_instance(db: Session, instance_id: uuid.UUID) -> RAppInstance:
    """404 for an unknown id; otherwise first enforces upgradeTimeoutSeconds
    on the upgrade this instance takes part in (upgrade.py's lazy timeout),
    committing the rollback on its own so a later refusal in the same
    request can't undo it. A replacement rolled back here is gone: 404."""
    inst = _get_or_404(db, instance_id)
    owner = _upgrade_owner(db, inst)
    swept = _sweep_overdue_upgrade(db, owner) if owner is not None else False
    if swept is None:  # another replica swept it first: reload, which is a 404 if the replacement is gone
        return _get_or_404(db, instance_id)
    if swept and owner is not None and owner.instance_id != instance_id:
        raise framework_error(FrameworkError.RAPP_INSTANCE_NOT_FOUND,
                              detail=f"RAppInstance {instance_id} was an upgrade replacement, rolled back after "
                                     f"upgradeTimeoutSeconds={owner.upgrade_timeout_seconds}")
    return inst


def _fire(inst: RAppInstance, event: InstanceEvent) -> InstanceState:
    try:
        return RAPP_INSTANCE_FSM.fire(InstanceState(inst.state), event, instance=inst)
    except IllegalTransition as exc:
        raise illegal_transition_error(exc, f"RAppInstance {inst.instance_id}") from exc


@app.post("/instances", status_code=202)
@idempotent("rapp-mgmt", status_code=202)
def create_instance(body: CreateInstanceRequest, request: Request, db: Session = Depends(get_session)):
    """CreateInstance — requires a validated package: AVAILABLE, or PRIMED
    (AVAILABLE plus pre-provisioned resources; D-SEC-RAPP-1); 404 for an
    unknown package, 409 for any other state. NFO handoff per Onboarding/rApp
    Mgmt LLD section 5: reads the package's TOSCA service template and issues
    NFO.Instantiate; the returned nfDeploymentId is kept as workloadRef, and
    TERMINATE hands it back to NFO. The same path provisions an upgrade's
    replacement instance (provisioning.py).
    """
    base = _checked_operator_api_base(body.operatorApiBase) if body.operatorApiBase is not None else None
    if body.approvalPolicy is not None and body.autonomyMode != "ASSIST":
        raise problem(422, "APPROVAL_POLICY_NEEDS_ASSIST", "approvalPolicy applies to an ASSIST instance: AUTONOMOUS asks no one and SHADOW writes nothing")
    try:
        claim = authz_scope.to_claim(authz_scope.from_claim(body.authzScope))
    except ValueError as exc:
        raise framework_error(FrameworkError.AUTHZ_SCOPE_INVALID, detail=str(exc)) from None
    inst = provision_instance(db, body.packageId, configuration=body.config, autonomy_mode=body.autonomyMode,
                              region_scope=body.regionScope, approval_policy=_policy_to_store(body.approvalPolicy),
                              authz_scope=claim)
    inst.operator_api_base = base
    db.commit()
    return {"instanceId": str(inst.instance_id), "oauthClientId": inst.oauth_client_id}


def _checked_operator_api_base(value: str) -> str:
    """The base URL as it will be stored, or 422: http(s) only, no credentials, query or fragment, and not a loopback, link-local or metadata address
    (smo_shared.webhook.normalise_base_url, the guard every caller-supplied destination passes)."""
    base = normalise_base_url(value)
    if base is None:
        raise problem(422, "OPERATOR_API_BASE_INVALID", "operatorApiBase must be an http or https URL without credentials, query or fragment, "
                                                         "and not a loopback, link-local or metadata address")
    return base


def _on_bootstrap(inst) -> None:
    """What happens when an instance's bootstrap is accepted: the limits its manifest declares are put in force (fail-closed: AI-10.2), then
    its approval policy, if it was created with one (AI-11.4, fail-closed as well), then its SME declarations are registered (best-effort)."""
    with on_own_account():
        # the platform's act about the rApp, not the rApp's: when the rApp itself reports that it is up (it may, for its own instance) a call made "for" it would be
        # refused by RAN NF OAM, which does not let a caller change its own limit or approval policy
        status = onboarding_status(inst)
        apply_rapp_limits(inst, status)
        apply_approval_policy(inst)
        register_sme_declarations(inst, status)


@app.post("/instances/{instance_id}/credentials")
def issue_instance_credentials(instance_id: uuid.UUID, response: Response, db: Session = Depends(get_session)):
    """PR-SEC-14: the OAuth client credentials the instance's workload authenticates with, issued once. SME keeps only a hash of the secret, so
    this registers a NEW invoker for the instance (it replaces the one made at create and deregisters it) and returns its id and secret in this
    answer and nowhere else; call it again to rotate. The id is the instance's `oauthClientId`, so call it before the workload bootstraps:
    409 once the instance is RUNNING (the SME and DME registrations made at bootstrap are under the old id). It is not an idempotent
    command: a replayed answer would be a stored secret. Give the pair to the workload as SMO_INVOKER_ID / SMO_INVOKER_SECRET with
    SMO_IDENTITY_KIND=rapp."""
    inst = _load_instance(db, instance_id)
    if InstanceState(inst.state) != InstanceState.DEPLOYING:
        raise illegal_transition_error(IllegalTransition(InstanceState(inst.state), InstanceEvent.BOOTSTRAP_OK),
                                       f"RAppInstance {instance_id}: credentials are issued while it is DEPLOYING")
    previous = inst.oauth_client_id
    secret = register_instance_invoker(inst)
    delivered = deliver_credentials(inst, secret)
    db.commit()
    if previous:
        with suppress(httpx.HTTPError):
            R1Client().delete(f"/sme/invoker-registrations/{previous}")
    response.headers["Cache-Control"] = "no-store"
    if delivered:       # with delivery on, the secret went to the workload's Secret and goes nowhere else, not even into this answer
        return {"instanceId": str(inst.instance_id), "oauthClientId": inst.oauth_client_id, "credentialSecret": delivered["kubernetesSecret"]}
    return {"instanceId": str(inst.instance_id), "oauthClientId": inst.oauth_client_id, "oauthClientSecret": secret}


# The body of the kill-switch route: `requestedBy` is recorded at RAN NF OAM as who stopped the instance (the GUI forces it to the signed-in user) and `reason` is optional free text.
class KillRequest(BaseModel):
    requestedBy: str
    reason: str | None = None


def _kill_call(inst, call):
    """The operator thinks in instances; RAN NF OAM keys the switch on the invoker id, which is the instance's `oauth_client_id`."""
    if inst.oauth_client_id is None:
        raise framework_error(FrameworkError.RAPP_INSTANCE_NOT_FOUND, detail=f"RAppInstance {inst.instance_id} has no credential (terminated)")
    try:
        resp = call()
    except httpx.HTTPError:
        resp = None
    if resp is None or resp.status_code not in (200, 204):
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail="RAN NF OAM did not accept the kill switch change; nothing was changed")
    return resp


@app.put("/instances/{instance_id}/kill")
def kill_instance(instance_id: uuid.UUID, body: KillRequest, db: Session = Depends(get_session)):
    """AI-10.4, the per-rApp kill switch, as an operator action: stop this instance's writes at RAN NF OAM (its config jobs are refused until
    `DELETE`). The instance itself keeps running; terminate it to remove it. 503 when RAN NF OAM cannot be told: a switch that may not have
    been thrown is reported as such, never as done."""
    inst = _load_instance(db, instance_id)
    resp = _kill_call(inst, lambda: R1Client().put(f"/ran-nf-oam/rapp-kill/{inst.oauth_client_id}", json={"requestedBy": body.requestedBy, "reason": body.reason}))
    return {"instanceId": str(inst.instance_id), "killed": True, **{k: v for k, v in resp.json().items() if k in ("killedBy", "reason", "killedAt")}}


@app.delete("/instances/{instance_id}/kill")
def lift_instance_kill(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """AI-10.4: let the instance write again. Idempotent: lifting a switch that was not thrown is fine."""
    inst = _load_instance(db, instance_id)
    _lift_kill_call(inst)
    return {"instanceId": str(inst.instance_id), "killed": False}


def _lift_kill_call(inst) -> None:
    """Lifts the kill switch of one instance at RAN NF OAM (`DELETE /ran-nf-oam/rapp-kill/{oauthClientId}`); nothing to do for an instance without a
    credential. A 404 (the switch was not thrown) counts as done. Raises 503 `ENDPOINT_UNREACHABLE` when RAN NF OAM cannot be told or refuses."""
    try:
        resp = R1Client().delete(f"/ran-nf-oam/rapp-kill/{inst.oauth_client_id}") if inst.oauth_client_id else None
    except httpx.HTTPError:
        resp = None
    if inst.oauth_client_id is not None and (resp is None or resp.status_code not in (204, 404)):
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail="RAN NF OAM did not accept the change; nothing was changed")


# GUI-9.6: the page size used to read RAN NF OAM's list of stopped invokers (its maximum, smo_shared.pagination.MAX_LIMIT).
_KILL_LIST_PAGE = 500


def _stopped_invoker_ids() -> set[str]:
    """Returns the invoker ids RAN NF OAM holds as stopped, read page by page from `GET /ran-nf-oam/rapp-kill`.

    Raises 503 `ENDPOINT_UNREACHABLE` when any page cannot be read: a global stop or resume must not act on a partial picture, and a count that could
    not be read is not reported as zero.
    """
    r1 = R1Client()
    stopped: set[str] = set()
    offset = 0
    while True:
        try:
            resp = r1.get("/ran-nf-oam/rapp-kill", params={"limit": _KILL_LIST_PAGE, "offset": offset})
        except httpx.HTTPError:
            resp = None
        if resp is None or resp.status_code != 200:
            raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail="RAN NF OAM did not answer; the stopped rApps could not be read")
        items = resp.json().get("items") or []
        stopped.update(i["invokerId"] for i in items if i.get("invokerId"))
        if len(items) < _KILL_LIST_PAGE:
            return stopped
        offset += _KILL_LIST_PAGE


def _live_instances(db: Session) -> list[RAppInstance]:
    """Returns every instance that can still write: not UNDEPLOYED and holding a credential (the switch keys on `oauth_client_id`), oldest first."""
    stmt = (select(RAppInstance).where(RAppInstance.state != InstanceState.UNDEPLOYED, RAppInstance.oauth_client_id.is_not(None))
            .order_by(RAppInstance.created_at, RAppInstance.instance_id))
    return list(db.scalars(stmt))


def _failure(inst: RAppInstance, exc: HTTPException) -> dict:
    """The entry of the `failed` list for an instance whose switch could not be changed: its id and the problem's detail text."""
    detail = exc.detail.get("detail") if isinstance(exc.detail, dict) else exc.detail
    return {"instanceId": str(inst.instance_id), "error": str(detail)}


@app.put("/kill-all")
def kill_all_instances(body: KillRequest, db: Session = Depends(get_session)):
    """GUI-9.6, the global stop: throw the kill switch (AI-10.4) of every instance that is not UNDEPLOYED, one instance at a time through the same call as
    `PUT /instances/{id}/kill`. An instance already stopped is left as it is (its first stop, reason and author are kept) and counted in `alreadyStopped`.
    Answers `{"stopped", "alreadyStopped", "failed": [{"instanceId", "error"}]}`; an instance RAN NF OAM did not accept is listed in `failed`, the others
    stay stopped. 503 when RAN NF OAM's list of stopped rApps cannot be read: then nothing was changed."""
    already = _stopped_invoker_ids()
    stopped, already_stopped, failed = 0, 0, []
    for inst in _live_instances(db):
        if inst.oauth_client_id in already:
            already_stopped += 1
            continue
        try:
            _kill_call(inst, lambda inst=inst: R1Client().put(f"/ran-nf-oam/rapp-kill/{inst.oauth_client_id}",
                                                              json={"requestedBy": body.requestedBy, "reason": body.reason}))
        except HTTPException as exc:
            failed.append(_failure(inst, exc))
            continue
        stopped += 1
    log.warning("global stop by %s: %d stopped, %d already stopped, %d failed", body.requestedBy, stopped, already_stopped, len(failed))
    return {"stopped": stopped, "alreadyStopped": already_stopped, "failed": failed}


@app.delete("/kill-all")
def resume_all_instances(db: Session = Depends(get_session)):
    """GUI-9.6: lift the kill switch of every stopped instance that is not UNDEPLOYED (the per-instance `DELETE /instances/{id}/kill`, for each).
    A stop of an invoker that is not one of this module's instances is not touched. Answers `{"resumed", "failed": [{"instanceId", "error"}]}`.
    503 when RAN NF OAM's list of stopped rApps cannot be read: then nothing was changed."""
    stopped_ids = _stopped_invoker_ids()
    resumed, failed = 0, []
    for inst in _live_instances(db):
        if inst.oauth_client_id not in stopped_ids:
            continue
        try:
            _lift_kill_call(inst)
        except HTTPException as exc:
            failed.append(_failure(inst, exc))
            continue
        resumed += 1
    log.warning("global resume: %d resumed, %d failed", resumed, len(failed))
    return {"resumed": resumed, "failed": failed}


@app.get("/kill-all")
def count_stopped_instances(db: Session = Depends(get_session)):
    """GUI-9.6: how many instances that are not UNDEPLOYED are stopped now (`stopped`), out of how many (`instances`). 503 when RAN NF OAM cannot
    answer: a count that could not be read is not reported as zero."""
    stopped_ids = _stopped_invoker_ids()
    live = _live_instances(db)
    return {"stopped": sum(1 for i in live if i.oauth_client_id in stopped_ids), "instances": len(live)}


@app.get("/instances/{instance_id}/safeguards")
def instance_safeguards(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """What holds this instance in check at RAN NF OAM, in one read (for the operator GUI, which thinks in instances while RAN NF OAM keys on the
    invoker id, the instance's `oauthClientId`): whether it is stopped (and by whom and why), and its limits with the number of config jobs it has
    started in the last hour. A terminated instance has no credential, so no invoker id and nothing to show. 503 when RAN NF OAM cannot answer: a
    stop that cannot be read is not reported as "not stopped"."""
    inst = _load_instance(db, instance_id)
    answer = {"instanceId": str(inst.instance_id), "invokerId": inst.oauth_client_id, "killed": False, "kill": None, "limits": None, "approvalPolicy": None}
    if inst.oauth_client_id is None:
        return answer
    r1 = R1Client()
    try:
        kill = r1.get(f"/ran-nf-oam/rapp-kill/{inst.oauth_client_id}")
        limits = r1.get(f"/ran-nf-oam/rapp-limits/{inst.oauth_client_id}")
        approval = r1.get(f"/ran-nf-oam/rapp-approval-policy/{inst.oauth_client_id}")
    except httpx.HTTPError:
        kill = limits = approval = None
    if (kill is None or limits is None or approval is None or kill.status_code not in (200, 404) or limits.status_code not in (200, 404)
            or approval.status_code not in (200, 404)):
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail="RAN NF OAM did not answer; the safeguards of this instance could not be read")
    if kill.status_code == 200:
        answer.update(killed=True, kill=kill.json())
    if limits.status_code == 200:
        answer["limits"] = limits.json()
    if approval.status_code == 200:
        answer["approvalPolicy"] = approval.json()
    return answer


@app.post("/instances/{instance_id}/bootstrap-complete")
def bootstrap_complete(instance_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """Called once the rApp container has bootstrapped via R1 Termination
    and registered with SME/DME — closes DEPLOYING -> RUNNING. 409 if the
    instance is not DEPLOYING. The rApp may call it for its own instance only
    (403 `NOT_THIS_INSTANCE` for another rApp's); an operator may call it for any.
    """
    inst = _load_instance(db, instance_id)
    _own_instance_or_operator(request, inst)
    if InstanceState(inst.state) != InstanceState.DEPLOYING:
        raise illegal_transition_error(IllegalTransition(InstanceState(inst.state), InstanceEvent.BOOTSTRAP_OK),
                                       f"RAppInstance {instance_id}")
    _on_bootstrap(inst)
    inst.state = _fire(inst, InstanceEvent.BOOTSTRAP_OK)
    db.commit()
    return {"instanceId": str(inst.instance_id), "state": inst.state}


@app.post("/instances/{instance_id}/recover")
def recover_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """RECOVER — v1.3's own FAULTED exit, concretized: the FSM transition
    (FAULTED -> DEPLOYING) existed but no route ever fired it, so a
    critically-faulted instance had no API path back to RUNNING at all.
    Re-enters at the same point CreateInstance does — the container must
    re-bootstrap and call bootstrap-complete again, matching the "no
    lightweight update path" principle this build applies everywhere
    else (e.g. AI/ML Workflow's retraining re-entry). 409 from any state
    but FAULTED.
    """
    inst = _load_instance(db, instance_id)
    inst.state = _fire(inst, InstanceEvent.RECOVER)
    db.commit()
    return {"instanceId": str(inst.instance_id), "state": inst.state}


@app.post("/instances/{instance_id}/upgrade")
def upgrade_instance(instance_id: uuid.UUID, body: UpgradeRequest, db: Session = Depends(get_session)):
    """UpgradeInstance — Annex A.1.2.2.1. Kicks off the two-row choreography
    in upgrade.py: the old instance (must be RUNNING, else 409) goes
    UPGRADING and a replacement is provisioned exactly like CreateInstance
    does (newPackageId must be AVAILABLE or PRIMED — 404 unknown, 409
    otherwise; its own oauthClientId, NFO deployment and usage
    registration), inheriting the old instance's configuration,
    autonomyMode and regionScope. The outcome is reported through
    upgrade/resolve, or the upgrade rolls back on its own once
    upgradeTimeoutSeconds have passed unresolved.
    """
    old = _load_instance(db, instance_id)
    try:
        new = start_upgrade(db, old, body.newPackageId)
    except IllegalTransition as exc:
        raise illegal_transition_error(exc, f"RAppInstance {instance_id}") from exc
    db.commit()
    return {"newInstanceId": str(new.instance_id), "oldInstanceState": old.state,
            "oauthClientId": new.oauth_client_id}


@app.post("/instances/{instance_id}/upgrade/resolve")
def resolve_upgrade_outcome(instance_id: uuid.UUID, succeeded: bool, db: Session = Depends(get_session)):
    """Resolves the upgrade pending on the OLD instance. succeeded=true
    commits it: the replacement becomes RUNNING (registering its SME
    declarations if it never called bootstrap-complete itself) and the old
    instance is retired like a TERMINATE — DME/SME deregistration,
    credential revocation, NFO Terminate, usage/stop — then deleted.
    succeeded=false rolls back: the replacement is torn down the same way
    and deleted, the old instance returns to RUNNING. Answers with the
    surviving instance.

    404 for an unknown instance or one with no pending upgrade; 409
    LIFECYCLE_ILLEGAL_TRANSITION if the replacement can no longer be
    committed (e.g. it crashed); 409 RAPP_UPGRADE_TIMED_OUT for
    succeeded=true after upgradeTimeoutSeconds — the upgrade has already
    been rolled back (succeeded=false after the deadline just returns the
    rolled-back survivor).
    """
    old = _get_or_404(db, instance_id)
    if old.pending_upgrade_instance_id is None:
        raise framework_error(FrameworkError.RAPP_INSTANCE_NOT_FOUND, detail=f"RAppInstance {instance_id} has no pending upgrade")
    if _sweep_overdue_upgrade(db, old) is not False:
        if succeeded:
            raise framework_error(FrameworkError.RAPP_UPGRADE_TIMED_OUT,
                                  detail=f"upgrade of RAppInstance {instance_id} exceeded upgradeTimeoutSeconds="
                                         f"{old.upgrade_timeout_seconds} and was rolled back")
        return {"instanceId": str(old.instance_id), "state": old.state, "packageId": str(old.package_id)}
    new = _get_or_404(db, old.pending_upgrade_instance_id)
    try:
        resolve_upgrade(db, old, new, new_bootstrap_succeeded=succeeded, register_identity=_on_bootstrap)
    except IllegalTransition as exc:
        raise illegal_transition_error(exc, f"upgrade of RAppInstance {instance_id}") from exc
    db.commit()
    survivor = new if succeeded else old
    return {"instanceId": str(survivor.instance_id), "state": survivor.state, "packageId": str(survivor.package_id)}


def _resolve_current(db: Session, instance_id: uuid.UUID) -> RAppInstance:
    """A live instance, or — for an id an upgrade has since superseded — the
    instance that replaced it last (the lineage in the version history).
    404 if the id is neither."""
    current_id = current_instance_id(db, instance_id)
    if current_id is None:
        raise framework_error(FrameworkError.RAPP_INSTANCE_NOT_FOUND, detail=f"no such RAppInstance {instance_id}")
    return _load_instance(db, current_id)


def _version_view(v) -> dict:
    """One `RAppInstanceVersion` row as the JSON the version routes return: ids as strings, the instance and package it replaced, the configuration it ran, the rollback that undid it (or null) and the commit time.
    """
    return {"versionId": str(v.version_id), "kind": v.kind, "instanceId": str(v.instance_id),
            "packageId": str(v.package_id), "previousInstanceId": str(v.previous_instance_id),
            "previousPackageId": str(v.previous_package_id), "previousConfiguration": v.previous_configuration,
            "rolledBackByVersionId": str(v.rolled_back_by_version_id) if v.rolled_back_by_version_id else None,
            "committedAt": v.committed_at.isoformat()}


@app.post("/instances/{instance_id}/rollback")
def rollback_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """RollbackInstance (OI-1-sa-rollback) — an upgrade back to the newest
    version in this instance's history that has not been rolled back
    already, restoring the package, configuration, autonomy mode and region
    scope that version's instance ran. Same two-row choreography as
    UpgradeInstance: the current instance goes UPGRADING, the replacement
    is resolved through upgrade/resolve (or rolls back on its own after
    upgradeTimeoutSeconds, leaving the current version running). Repeated
    rollbacks walk further back rather than flip-flopping.

    `instance_id` may be one an upgrade has since superseded (an SA SMOS
    monitor registered before the upgrade): the rollback applies to the
    instance that replaced it last, named in the answer as instanceId.

    404 unknown id; 409 ROLLBACK_HISTORY_UNAVAILABLE when there is nothing
    to roll back to; 409 LIFECYCLE_ILLEGAL_TRANSITION when the current
    instance is not RUNNING; 404/409 from provisioning when the earlier
    package is gone or no longer deployable.
    """
    current = _resolve_current(db, instance_id)
    try:
        started = start_rollback(db, current)
    except IllegalTransition as exc:
        raise illegal_transition_error(exc, f"RAppInstance {current.instance_id}") from exc
    if started is None:
        raise framework_error(FrameworkError.ROLLBACK_HISTORY_UNAVAILABLE,
                              detail=f"RAppInstance {current.instance_id} has no upgrade left to roll back")
    new, target = started
    db.commit()
    return {"instanceId": str(current.instance_id), "newInstanceId": str(new.instance_id),
            "oldInstanceState": current.state, "fromPackageId": str(current.package_id),
            "toPackageId": str(new.package_id), "rollbackOfVersionId": str(target.version_id),
            "oauthClientId": new.oauth_client_id}


@app.get("/instances/{instance_id}/versions")
def list_instance_versions(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """The version history behind an instance (OI-1-sa-rollback), newest
    first: one entry per committed upgrade or rollback, with what the
    retired instance ran. A superseded id resolves to the current instance,
    as for rollback. rollbackTarget is the version a rollback would undo
    now (null: nothing to roll back to)."""
    current = _resolve_current(db, instance_id)
    target = rollback_target(db, current.instance_id)
    return {"instanceId": str(current.instance_id), "packageId": str(current.package_id), "state": current.state,
            "workloadRef": current.workload_ref,
            "rollbackTarget": _version_view(target) if target else None,
            "versions": [_version_view(v) for v in version_history(db, current.instance_id)]}


@app.post("/instances/{instance_id}/terminate")
def terminate_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """TerminateInstance — Annex A.1.2.3. Legal from RUNNING, FAULTED (a
    crashed instance is retired without recovering it first) and DEPLOYING
    (one whose container never bootstrapped); 409 otherwise, and 409 for an
    upgrade's pending replacement (resolve the upgrade instead).

    Credential revocation is part of this transition itself
    (statemachine.py's _revoke_credential action), not a separate step
    (closes RT-3), after the DME/SME deregistration keyed on that
    credential. Then the workload itself: NFO Terminate for workloadRef
    (the nfDeploymentId CreateInstance received) and usage/stop for the
    PackageUsageRegistration that Onboarding's deprime and cascade-delete
    guards read. Both are best-effort — an unreachable NFO or Onboarding
    never blocks the teardown — but their outcome is recorded in
    lastTeardown.

    HISTORY.md §5: this used to delete the instance row
    outright, in the same call — undeploy and delete collapsed into one
    irreversible step, with no way to observe an instance post-teardown
    or to delete one that was already torn down some other way (e.g.
    CRASH). The reference's own split (`RappService.undeployRappInstance`/
    `deleteRappInstance`, DEPLOYED -> UNDEPLOYING -> UNDEPLOYED, delete
    only legal from UNDEPLOYED) is adopted here: TERMINATE now only tears
    the workload down (this action) and lands in the terminal UNDEPLOYED
    state with the row still present; removing the row itself is the
    separate `delete_instance` below.
    """
    inst = _load_instance(db, instance_id)
    owner = _upgrade_owner(db, inst)
    if owner is not None and owner.instance_id != inst.instance_id:
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                              detail=f"RAppInstance {instance_id} is the pending replacement of an upgrade of "
                                     f"{owner.instance_id}; resolve that upgrade instead")
    inst.state = _fire(inst, InstanceEvent.TERMINATE)
    inst.last_teardown = release_instance_resources(inst, "TERMINATE")
    db.commit()
    return {"instanceId": str(inst.instance_id), "state": inst.state, "lastTeardown": inst.last_teardown}


@app.delete("/instances/{instance_id}", status_code=204)
def delete_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """DeleteRappInstance — the reference's own standalone delete, distinct
    from undeploy (`RappService.deleteRappInstance`'s guard: "Unable to
    delete rApp instance %s as it is not in UNDEPLOYED state"). Only legal
    once TERMINATE has already landed the instance in UNDEPLOYED — a
    running or faulted instance can't be deleted out from under itself.

    Also closes the same FK-cascade bug class already found and fixed for
    DME's `deregister_producer`/AI-ML Workflow's `deregister_model`: the
    instance's `rapp_fault_report`/`rapp_performance_report` rows had no
    `ON DELETE CASCADE` (fixed alongside this), so this cleans them up
    explicitly as a second, directly-testable line of defense.
    """
    inst = _load_instance(db, instance_id)
    if inst.state != InstanceState.UNDEPLOYED:
        raise framework_error(FrameworkError.RAPP_INSTANCE_NOT_UNDEPLOYED,
                               detail=f"instance {instance_id} is not UNDEPLOYED (state={inst.state})")
    db.query(RAppFaultReport).filter(RAppFaultReport.instance_id == instance_id).delete()
    db.query(RAppPerformanceReport).filter(RAppPerformanceReport.instance_id == instance_id).delete()
    db.delete(inst)
    db.commit()


@app.get("/instances/{instance_id}/config")
def get_config(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    # The stored configuration of an instance, `{}` when none. 404 for an unknown instance. Unlike most routes here it reads the row directly, without the lazy upgrade-timeout sweep of `_load_instance`.
    inst = _get_or_404(db, instance_id)
    return inst.configuration or {}


@app.put("/instances/{instance_id}/config")
def set_config(instance_id: uuid.UUID, config: dict, request: Request, db: Session = Depends(get_session)):
    # Replaces the whole configuration of an instance with the body (no merge, no schema check) and answers 200 `{status: updated}`; 404 for an unknown instance. It does not restart the workload.
    # A caller with the rApp role may replace only its own instance's configuration (403 `NOT_THIS_INSTANCE` otherwise, `_own_instance_or_operator`); any other caller is trusted
    # (the operator GUI's rule table allows operators and admins). The gateway does not let an rApp change this route at all (`roles.RAPP_MAY_CHANGE`): this is the second line.
    inst = _get_or_404(db, instance_id)
    _own_instance_or_operator(request, inst)
    inst.configuration = config
    db.commit()
    return {"status": "updated"}


@app.post("/instances/{instance_id}/performance")
def report_performance(instance_id: uuid.UUID, metrics: dict, request: Request, db: Session = Depends(get_session)):
    """Record a metrics object for the instance. The rApp may report for its own instance only (403 `NOT_THIS_INSTANCE` for another rApp's); an operator may for any."""
    _own_instance_or_operator(request, _get_or_404(db, instance_id))
    db.add(RAppPerformanceReport(instance_id=instance_id, metrics=metrics))
    db.commit()
    return {"status": "recorded"}


@app.post("/instances/{instance_id}/fault")
def report_fault(instance_id: uuid.UUID, request: Request, severity: str, description: str = "", db: Session = Depends(get_session)):
    """Records every fault report; severity=critical additionally fires
    CRASH (RUNNING -> FAULTED). A critical fault on an instance that is not
    RUNNING is refused with 409 and not recorded. A rApp may report for its
    own instance only (403 `NOT_THIS_INSTANCE` for another rApp's); an operator may for any.
    """
    inst = _load_instance(db, instance_id)
    _own_instance_or_operator(request, inst)      # before the CRASH below: another instance's token must not be able to fault this one
    if severity == "critical":
        inst.state = _fire(inst, InstanceEvent.CRASH)
    db.add(RAppFaultReport(instance_id=instance_id, severity=severity, description=description))
    db.commit()
    return {"status": "recorded", "instanceState": inst.state}


# The body of the operator-API registration: the base URL where the instance's operator API is reached. The route validates it (422 OPERATOR_API_BASE_INVALID).
class OperatorApiRequest(BaseModel):
    operatorApiBase: str


def _own_instance_or_operator(request: Request, inst: RAppInstance) -> None:
    """An rApp may register only for itself: the invoker id the gateway stamped must be this instance's credential. Any other role (the
    operator's GUI, an SMO module, a call that did not come through the gateway) is trusted as elsewhere in this module."""
    if request.headers.get(roles.ROLE_HEADER) == roles.ROLE_RAPP and (not inst.oauth_client_id or request.headers.get(INVOKER_ID_HEADER) != inst.oauth_client_id):
        raise problem(403, "NOT_THIS_INSTANCE", "a rApp may register the operator API of its own instance only")


def _serves(inst: RAppInstance) -> bool:
    return inst.state != InstanceState.UNDEPLOYED


@app.put("/instances/{instance_id}/operator-api")
def register_operator_api(instance_id: uuid.UUID, body: OperatorApiRequest, request: Request, db: Session = Depends(get_session)):
    """GUI-8.3: register where this instance's operator API is reached (docs/adr/0004-operator-ui-declaration.md, 4): the base URL R1 Termination
    resolves `/rapps/{instanceId}/operator/...` to. Accepted from an operator, or from the instance itself (a caller with the rApp role whose invoker
    id is this instance's `oauthClientId`; another rApp is refused, 403). The URL must be http or https, carry no credentials, query or fragment and
    not be a loopback, link-local or metadata address (422); the same check runs again before every call the gateway makes. Replaces an earlier value.
    A terminated instance is refused with 409: it has nothing to serve."""
    inst = _load_instance(db, instance_id)
    _own_instance_or_operator(request, inst)
    if not _serves(inst):
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION, detail=f"RAppInstance {instance_id} is {inst.state}: it has no operator API to register")
    inst.operator_api_base = _checked_operator_api_base(body.operatorApiBase)
    db.commit()
    return {"instanceId": str(inst.instance_id), "operatorApiBase": inst.operator_api_base}


@app.delete("/instances/{instance_id}/operator-api", status_code=204)
def clear_operator_api(instance_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """GUI-8.3: forget the registered operator API (the instance's declared page then shows "not registered"). Same callers as the PUT; idempotent."""
    inst = _load_instance(db, instance_id)
    _own_instance_or_operator(request, inst)
    inst.operator_api_base = None
    db.commit()


@app.get("/instances/{instance_id}/operator-api")
def get_operator_api(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """GUI-8.3: the registered operator API base of an instance (null: none), with its state. What R1 Termination reads to resolve the
    `/rapps/{instanceId}/operator/...` prefix. A terminated instance answers null: it has nothing to serve."""
    inst = _load_instance(db, instance_id)
    return {"instanceId": str(inst.instance_id), "state": inst.state, "operatorApiBase": inst.operator_api_base if _serves(inst) else None}


def _instances_in_region(db: Session, region: str, include_unscoped: bool):
    """PR-GUI-9.3: the condition "this instance may act in `region`": its `authz_scope` (PR-SEC-10.3) lists `region` under `regions`, or, with
    `include_unscoped`, it restricts no region (no claim at all, or a claim of tenants only: ADR 0005 leaves an axis it does not name unrestricted).

    Computed in SQL so the page count stays exact. The JSON column is spelled per dialect: Postgres `json_array_elements_text(authz_scope -> 'regions')`
    (a missing key is NULL, which expands to no rows), SQLite `json_each(authz_scope, '$.regions')`; `json_each` and the derived column list are not
    portable to the other side, hence the branch."""
    if db.get_bind().dialect.name == "postgresql":
        regions_json = RAppInstance.authz_scope["regions"]                                # `->`: JSON, not the text of `->>`
        items = func.json_array_elements_text(regions_json).table_valued("value").render_derived(name="scope_region")
    else:
        regions_json = func.json_extract(RAppInstance.authz_scope, "$.regions")
        items = func.json_each(RAppInstance.authz_scope, "$.regions").table_valued("value")
    listed = exists(select(items.c.value).where(items.c.value == region))
    if not include_unscoped:
        return listed
    return or_(listed, RAppInstance.authz_scope.is_(None), regions_json.is_(None))


@app.get("/instances")
def list_instances(state: str | None = None,
                   region: str | None = Query(None, min_length=1, max_length=100,
                                              description="Keep the instances whose `authzScope.regions` lists this region (ADR 0005)."),
                   include_unscoped: bool = Query(True, description="With `region`: also keep the instances that restrict no region "
                                                                    "(no `authzScope`, or one without `regions`), which may act anywhere."),
                   limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """The rApp instances, paged. `state` narrows. PR-GUI-9.3: `region` keeps the instances whose `authzScope.regions` lists it and, unless
    `include_unscoped=false`, the instances that restrict no region (they may touch an element of any region); without `region`,
    `include_unscoped` does nothing."""
    # lazy upgradeTimeoutSeconds enforcement (upgrade.py) for every upgrade in flight
    for old in list(db.scalars(select(RAppInstance).where(RAppInstance.state == InstanceState.UPGRADING))):
        _sweep_overdue_upgrade(db, old)
    stmt = select(RAppInstance)
    if state:
        stmt = stmt.where(RAppInstance.state == state)
    if region:
        stmt = stmt.where(_instances_in_region(db, region, include_unscoped))
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"instanceId": str(i.instance_id), "packageId": str(i.package_id), "state": i.state,
             "autonomyMode": i.autonomy_mode, "operatorApiBase": i.operator_api_base, "authzScope": i.authz_scope} for i in page["items"]]}


@app.get("/instances/{instance_id}")
def get_instance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """HISTORY.md §5: no single-instance detail read existed at
    all — only the list route above and single-field sub-resources
    (config via get_config/set_config). The reference's own
    GET .../instance/{id} returns nested ACM/SME/DME resource records
    (composition IDs, provider-function IDs, producer/consumer type
    lists) — the ACM part stays out of scope, unchanged: CreateInstance
    never accepts that caller-supplied deploy descriptor in the first
    place (real ACM/Helm/K8s deployment is the declared elision), so
    echoing it back would mean inventing descriptor data, not exposing
    something this build already computes. What this genuinely does
    expose: workloadRef (the real NFO nfDeploymentId CreateInstance
    received back), smeServiceIds (the real SME serviceId(s)
    bootstrap-complete's own SME auto-registration received back —
    HISTORY.md §7's Onboarding/rApp Mgmt finding 3, closed), and the
    caller-supplied configuration, alongside the identity/state fields
    list_instances already returns. lastTeardown is the recorded outcome of
    the most recent NFO Terminate / usage/stop this row performed or
    inherited through an upgrade. Reading an instance enforces its
    upgrade's upgradeTimeoutSeconds (an overdue upgrade rolls back).
    """
    inst = _load_instance(db, instance_id)
    return {
        "instanceId": str(inst.instance_id), "packageId": str(inst.package_id), "state": inst.state,
        "workloadRef": inst.workload_ref, "configuration": inst.configuration,
        "pendingUpgradeInstanceId": str(inst.pending_upgrade_instance_id) if inst.pending_upgrade_instance_id else None,
        "smeServiceIds": inst.sme_service_ids,
        "autonomyMode": inst.autonomy_mode, "regionScope": inst.region_scope, "approvalPolicy": inst.approval_policy,
        "authzScope": inst.authz_scope, "lastTeardown": inst.last_teardown, "operatorApiBase": inst.operator_api_base,
    }


@app.get("/instances/{instance_id}/performance")
def list_performance_reports(instance_id: uuid.UUID, limit: int = PageLimit, offset: int = PageOffset,
                              db: Session = Depends(get_session)):
    """Read side of report_performance above — previously write-only, so
    an operator had no way to see what an rApp had reported at all.
    Newest first (the GUI's KPI sparkline only ever wants the recent tail).
    """
    _get_or_404(db, instance_id)
    stmt = select(RAppPerformanceReport).where(RAppPerformanceReport.instance_id == instance_id).order_by(RAppPerformanceReport.reported_at.desc())
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"reportId": str(r.id), "metrics": r.metrics, "reportedAt": r.reported_at.isoformat()} for r in page["items"]]}


# GUI-9.8: the most instances one batched latest-KPI read may name (a page of the GUI's rApps table).
LATEST_PERFORMANCE_MAX_IDS = 50


def _numeric_metrics(metrics: dict | None) -> dict:
    """The top-level entries of a performance report whose value is a number (a bool is not one); nested objects, strings and lists are left out."""
    return {k: v for k, v in (metrics or {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _latest_view(instance_id: uuid.UUID, report: RAppPerformanceReport | None) -> dict:
    """The latest-KPI answer for one instance: `at` (null) and `metrics` (`{}`) when it has reported nothing."""
    if report is None:
        return {"instanceId": str(instance_id), "at": None, "metrics": {}}
    return {"instanceId": str(instance_id), "at": as_utc(report.reported_at).isoformat(), "metrics": _numeric_metrics(report.metrics)}


def _latest_reports(db: Session, instance_ids: list[uuid.UUID]) -> dict[uuid.UUID, RAppPerformanceReport]:
    """The newest performance report of each of `instance_ids`, in one query (the newest `reported_at` per instance, joined back to its row; the
    index `ix_rapp_performance_report_instance_reported` serves both). Two reports with the same newest time: either one."""
    if not instance_ids:
        return {}
    newest = (select(RAppPerformanceReport.instance_id, func.max(RAppPerformanceReport.reported_at).label("at"))
              .where(RAppPerformanceReport.instance_id.in_(instance_ids)).group_by(RAppPerformanceReport.instance_id).subquery())
    stmt = select(RAppPerformanceReport).join(newest, (RAppPerformanceReport.instance_id == newest.c.instance_id)
                                              & (RAppPerformanceReport.reported_at == newest.c.at))
    return {r.instance_id: r for r in db.scalars(stmt)}


@app.get("/instances/performance/latest")
def latest_performance_batch(ids: str, db: Session = Depends(get_session)):
    """GUI-9.8, the headline KPI of many rApps in one read: `ids` is a comma-separated list of up to 50 instance ids. Answers `{"items": [{"instanceId",
    "at", "metrics": {name: number}}]}`, one item per id in the order given (duplicates once), each the numeric metrics of that instance's newest
    performance report; an id with no report (or no instance) has `at` null and `metrics` `{}`. 422 for more than 50 ids or one that is not a UUID."""
    try:
        wanted = list(dict.fromkeys(uuid.UUID(part.strip()) for part in ids.split(",") if part.strip()))
    except ValueError:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="ids must be a comma-separated list of instance ids (UUIDs)") from None
    if len(wanted) > LATEST_PERFORMANCE_MAX_IDS:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"at most {LATEST_PERFORMANCE_MAX_IDS} ids per call")
    reports = _latest_reports(db, wanted)
    return {"items": [_latest_view(i, reports.get(i)) for i in wanted]}


@app.get("/instances/{instance_id}/performance/latest")
def latest_performance(instance_id: uuid.UUID, db: Session = Depends(get_session)):
    """GUI-9.8, an rApp's headline KPI: `{"instanceId", "at", "metrics": {name: number}}`, the numeric metrics of the newest report it sent to
    `POST /instances/{id}/performance` (non-numeric values left out). Never 404: `at` is null and `metrics` `{}` when there is no report (or no
    such instance), so the GUI can ask for every row it shows."""
    return _latest_view(instance_id, _latest_reports(db, [instance_id]).get(instance_id))


@app.get("/instances/{instance_id}/faults")
def list_fault_reports(instance_id: uuid.UUID, limit: int = PageLimit, offset: int = PageOffset,
                        db: Session = Depends(get_session)):
    """Read side of report_fault above, same shape as list_performance_reports."""
    _get_or_404(db, instance_id)
    stmt = select(RAppFaultReport).where(RAppFaultReport.instance_id == instance_id).order_by(RAppFaultReport.reported_at.desc())
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"faultId": str(r.id), "severity": r.severity, "description": r.description,
             "reportedAt": r.reported_at.isoformat()} for r in page["items"]]}
