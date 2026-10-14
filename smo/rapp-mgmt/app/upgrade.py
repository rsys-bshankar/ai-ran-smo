"""UpgradeInstance orchestration — Onboarding/rApp Mgmt LLD section 6.

Coordinates two RAppInstance rows through statemachine.py's per-row FSM.
Kept separate from the FSM itself because this is cross-entity choreography
(a saga), not a single state machine — same separation used for the
NFO -> FOCOM dependency in the NFO+FOCOM LLD.

The replacement row is a complete instance (OI-2-upgrade-completeness,
OI-1-upgrade-identity): it goes through CreateInstance's own
`provision_instance` (package-state check, its own fresh oauth_client_id,
NFO Instantiate, usage/start) and inherits the old row's configuration,
autonomy mode and region scope. Whichever row loses is torn down the way
TERMINATE tears an instance down — DME/SME deregistration and credential
revocation (the FSM action), then NFO Terminate and usage/stop
(`release_instance_resources`) — before its row is deleted.

Every commit is recorded as a RAppInstanceVersion (OI-1-sa-rollback): what
the retired row ran, and the lineage link from it to its successor. A
rollback (`start_rollback`) is an upgrade back to the newest version not
already rolled back — the same two-row choreography, timeout and
auto-rollback, so a failed rollback leaves the current version running.

Call flow 07b: the replacement is given an operator API address (the new
package's declared one, else the old row's), so the route can push it its
credentials and it can report itself up; and a stop the operator put on the
old row (the kill switch) is put on the replacement before the upgrade
starts, so an upgrade never releases a stopped rApp.

upgradeTimeoutSeconds is enforced lazily: no scheduler exists in this
build, so an unresolved upgrade whose replacement was created more than
`old.upgrade_timeout_seconds` ago is rolled back the next time either row
is touched (`expire_overdue_upgrade`, called from main.py's reads and
lifecycle routes).
"""

import copy
import datetime
import uuid
from typing import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.errors import FrameworkError, framework_error
from smo_shared.invoker import on_own_account
from smo_shared.r1_client import R1Client
from smo_shared.statemachine import IllegalTransition
from smo_shared.timeutil import as_utc

from .models import RAppFaultReport, RAppInstance, RAppInstanceVersion, RAppPerformanceReport
from .provisioning import provision_instance, release_instance_resources
from .statemachine import RAPP_INSTANCE_FSM, InstanceEvent, InstanceState, _terminate_side_effects


def start_upgrade(db: Session, old: RAppInstance, new_package_id: uuid.UUID,
                  restore: RAppInstanceVersion | None = None) -> RAppInstance:
    """An instance that is not RUNNING is refused (IllegalTransition) before
    anything is provisioned; a package that is not AVAILABLE/PRIMED is
    refused by provision_instance before any NFO call — either way the old
    row is left untouched.

    The replacement inherits the old row's configuration, autonomy mode,
    region scope and authorization scope claim (PR-SEC-10.3) — or, for a rollback (`restore`), the snapshot that version
    recorded for the instance it retired.

    Call flow 07b: the replacement's operator API address is the new package's declared one, else the old row's. When the old row is stopped (its
    kill switch), the replacement is stopped too before anything else happens to it; 503 `ENDPOINT_UNREACHABLE` when RAN NF OAM cannot say whether
    the old row is stopped, or does not take the replacement's stop (the replacement is then torn down again)."""
    old_state = InstanceState(old.state)
    if InstanceEvent.START_UPGRADE not in RAPP_INSTANCE_FSM.legal_events(old_state):
        raise IllegalTransition(old_state, InstanceEvent.START_UPGRADE)
    stop = _stop_of(old)
    if restore is None:
        configuration, autonomy_mode, region_scope = old.configuration, old.autonomy_mode, old.region_scope
        approval_policy, authz_scope = old.approval_policy, old.authz_scope
    else:
        configuration = restore.previous_configuration
        autonomy_mode, region_scope = restore.previous_autonomy_mode, restore.previous_region_scope
        approval_policy, authz_scope = restore.previous_approval_policy, restore.previous_authz_scope
    new = provision_instance(db, new_package_id, configuration=copy.deepcopy(configuration),
                             autonomy_mode=autonomy_mode, region_scope=copy.deepcopy(region_scope), approval_policy=copy.deepcopy(approval_policy),
                             authz_scope=copy.deepcopy(authz_scope))
    new.upgrade_timeout_seconds = old.upgrade_timeout_seconds
    new.operator_api_base = new.declared_operator_api_base or old.operator_api_base
    if stop is not None:
        _carry_stop(new, old, stop)
    new.rollback_of_version_id = restore.version_id if restore is not None else None
    old.state = RAPP_INSTANCE_FSM.fire(old_state, InstanceEvent.START_UPGRADE, instance=old)
    old.pending_upgrade_instance_id = new.instance_id
    return new


def _stop_of(old: RAppInstance) -> dict | None:
    """The kill switch RAN NF OAM holds for `old` (`GET /ran-nf-oam/rapp-kill/{oauthClientId}`), or None when it is not stopped. Raises 503
    `ENDPOINT_UNREACHABLE` when RAN NF OAM cannot answer: an upgrade must not start on a guess that the rApp was not stopped."""
    if old.oauth_client_id is None:
        return None
    try:
        with on_own_account():
            resp = R1Client().get(f"/ran-nf-oam/rapp-kill/{old.oauth_client_id}")
    except httpx.HTTPError:
        resp = None
    if resp is None or resp.status_code not in (200, 404):
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail="RAN NF OAM did not say whether the instance is stopped; the upgrade was not started")
    return resp.json() if resp.status_code == 200 else None


def _carry_stop(new: RAppInstance, old: RAppInstance, stop: dict) -> None:
    """Stops the replacement as the old row is stopped (`PUT /ran-nf-oam/rapp-kill/{new oauthClientId}`, same author, the reason naming the old row).
    When RAN NF OAM does not take it, the replacement is torn down again (its identity, workload and usage registration; the row goes with the
    request's rollback) and 503 `ENDPOINT_UNREACHABLE` is raised."""
    body = {"requestedBy": stop.get("killedBy") or "rapp-mgmt",
            "reason": f"carried over from instance {old.instance_id} by its upgrade" + (f": {stop['reason']}" if stop.get("reason") else "")}
    try:
        with on_own_account():
            resp = R1Client().put(f"/ran-nf-oam/rapp-kill/{new.oauth_client_id}", json=body)
    except httpx.HTTPError:
        resp = None
    if resp is None or resp.status_code not in (200, 204):
        _terminate_side_effects(new)
        release_instance_resources(new, "UPGRADE_NOT_STARTED")
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail="RAN NF OAM did not stop the replacement as the instance is stopped; the upgrade was not started")


def upgrade_deadline(old: RAppInstance, new: RAppInstance) -> datetime.datetime:
    return as_utc(new.created_at) + datetime.timedelta(seconds=old.upgrade_timeout_seconds)


def _delete_row(db: Session, inst: RAppInstance) -> None:
    db.query(RAppFaultReport).filter(RAppFaultReport.instance_id == inst.instance_id).delete()
    db.query(RAppPerformanceReport).filter(RAppPerformanceReport.instance_id == inst.instance_id).delete()
    db.delete(inst)


def _roll_back(db: Session, old: RAppInstance, new: RAppInstance | None, reason: str) -> None:
    """Tears the replacement down like a TERMINATE (DME/SME deregistration
    if it had bootstrapped, credential revocation, NFO Terminate,
    usage/stop), deletes it, and returns the old row to RUNNING."""
    old_state = RAPP_INSTANCE_FSM.fire(InstanceState(old.state), InstanceEvent.UPGRADE_ROLLBACK, instance=old)
    if new is not None:
        _terminate_side_effects(new)
        old.last_teardown = release_instance_resources(new, reason)
        old.pending_upgrade_instance_id = None
        db.flush()  # clear the old row's FK to the replacement before deleting it
        _delete_row(db, new)
    old.state = old_state
    old.pending_upgrade_instance_id = None


def resolve_upgrade(db: Session, old: RAppInstance, new: RAppInstance, new_bootstrap_succeeded: bool,
                    register_identity: Callable[[RAppInstance], None] | None = None) -> None:
    """Called once the new instance's bootstrap outcome is known — either
    it reached RUNNING within old.upgrade_timeout_seconds, or it didn't
    (FAULTED or the timeout fired first). Auto-rollback, no manual
    intervention, per v1.3's own decision — this function is where that
    decision is actually executed.

    Commit: the replacement must be DEPLOYING (fires BOOTSTRAP_OK and runs
    `register_identity` — SME registration under its own identity) or
    already RUNNING (its container called bootstrap-complete itself); any
    other state raises IllegalTransition before anything is changed. The old
    row is then retired like a TERMINATE and deleted; its teardown outcome
    is recorded on the replacement's `last_teardown`, and the commit as a
    RAppInstanceVersion (`_record_version`).
    """
    if new_bootstrap_succeeded:
        new_state = InstanceState(new.state)
        if new_state not in (InstanceState.DEPLOYING, InstanceState.RUNNING):
            raise IllegalTransition(new_state, InstanceEvent.BOOTSTRAP_OK)
        if InstanceState(old.state) != InstanceState.UPGRADING:
            raise IllegalTransition(InstanceState(old.state), InstanceEvent.UPGRADE_COMMIT)
        if new_state == InstanceState.DEPLOYING:
            new.state = RAPP_INSTANCE_FSM.fire(new_state, InstanceEvent.BOOTSTRAP_OK, instance=new)
            if register_identity is not None:
                register_identity(new)
        old.state = RAPP_INSTANCE_FSM.fire(InstanceState(old.state), InstanceEvent.UPGRADE_COMMIT, instance=old)
        _record_version(db, old, new)
        new.last_teardown = release_instance_resources(old, "UPGRADE_COMMIT")
        _delete_row(db, old)  # old row torn down once its replacement is confirmed RUNNING
    else:
        _roll_back(db, old, new, "UPGRADE_ROLLBACK")


def expire_overdue_upgrade(db: Session, old: RAppInstance, now: datetime.datetime | None = None) -> bool:
    """Rolls an unresolved upgrade back once its deadline has passed (reason
    UPGRADE_TIMEOUT). Returns True if it did. A pending pointer whose
    replacement row no longer exists is rolled back too."""
    if old.pending_upgrade_instance_id is None or InstanceState(old.state) != InstanceState.UPGRADING:
        return False
    new = db.get(RAppInstance, old.pending_upgrade_instance_id)
    now = now or datetime.datetime.now(datetime.UTC)
    if new is not None and upgrade_deadline(old, new) > now:
        return False
    _roll_back(db, old, new, "UPGRADE_TIMEOUT")
    return True


# ---------------------------------------------------------------- version history (OI-1-sa-rollback)

def _record_version(db: Session, old: RAppInstance, new: RAppInstance) -> RAppInstanceVersion:
    """Records the commit that retired `old` in favour of `new`. A rollback's
    replacement records kind ROLLBACK and marks the upgrade it undid."""
    version = RAppInstanceVersion(
        version_id=uuid.uuid4(), instance_id=new.instance_id, previous_instance_id=old.instance_id,
        package_id=new.package_id, previous_package_id=old.package_id,
        previous_configuration=copy.deepcopy(old.configuration), previous_autonomy_mode=old.autonomy_mode,
        previous_region_scope=copy.deepcopy(old.region_scope), previous_approval_policy=copy.deepcopy(old.approval_policy),
        previous_authz_scope=copy.deepcopy(old.authz_scope),
        kind="ROLLBACK" if new.rollback_of_version_id is not None else "UPGRADE",
    )
    db.add(version)
    # Inserted before the undone row's update references it: the ORM emits a
    # mapper's UPDATEs before its INSERTs, and only the migration declares
    # rolled_back_by_version_id's FK.
    db.flush()
    if new.rollback_of_version_id is not None:
        undone = db.get(RAppInstanceVersion, new.rollback_of_version_id)
        if undone is not None:
            undone.rolled_back_by_version_id = version.version_id
    return version


def _version_that_made(db: Session, instance_id: uuid.UUID) -> RAppInstanceVersion | None:
    return db.scalar(select(RAppInstanceVersion).where(RAppInstanceVersion.instance_id == instance_id))


def current_instance_id(db: Session, instance_id: uuid.UUID) -> uuid.UUID | None:
    """Follows the lineage forward from a possibly superseded instance id to
    the row that replaced it last. None if the id is neither a live row nor
    in the history."""
    seen = set()
    while db.get(RAppInstance, instance_id) is None:
        successor = db.scalar(select(RAppInstanceVersion.instance_id)
                              .where(RAppInstanceVersion.previous_instance_id == instance_id))
        if successor is None or successor in seen:
            return None
        seen.add(instance_id)
        instance_id = successor
    return instance_id


def version_history(db: Session, instance_id: uuid.UUID) -> list[RAppInstanceVersion]:
    """The versions behind `instance_id`, newest first."""
    history, seen = [], set()
    version = _version_that_made(db, instance_id)
    while version is not None and version.version_id not in seen:
        seen.add(version.version_id)
        history.append(version)
        version = _version_that_made(db, version.previous_instance_id)
    return history


def rollback_target(db: Session, instance_id: uuid.UUID) -> RAppInstanceVersion | None:
    """The newest UPGRADE in the instance's history not already rolled back:
    rolling back returns to what it retired. ROLLBACK rows and the upgrades
    they undid are stepped over, so repeated rollbacks walk further back
    (v3 -> v2 -> v1) instead of flip-flopping."""
    for version in version_history(db, instance_id):
        if version.kind == "UPGRADE" and version.rolled_back_by_version_id is None:
            return version
    return None


def start_rollback(db: Session, current: RAppInstance) -> tuple[RAppInstance, RAppInstanceVersion] | None:
    """Starts an upgrade of `current` back to the version `rollback_target`
    names, restoring that version's configuration snapshot. None if there is
    nothing to roll back to; IllegalTransition / the provisioning refusals
    as for start_upgrade otherwise."""
    target = rollback_target(db, current.instance_id)
    if target is None:
        return None
    return start_upgrade(db, current, target.previous_package_id, restore=target), target
