"""RAppInstance lifecycle.

SMO Design v1.3 section 3.5 state diagram, made precise by Onboarding/rApp
Mgmt LLD section 6: UpgradeInstance is TWO rows in choreography (old kept
running, new deployed alongside it), not one row transitioning through an
'upgrading' state in place. The FSM below governs each row's own
transitions; upgrade.py is the orchestrator that coordinates both rows
and applies the timeout/auto-rollback policy.
"""

from __future__ import annotations

from contextlib import suppress
from enum import StrEnum

import httpx

from smo_shared.r1_client import R1Client
from smo_shared.statemachine import StateMachine

from .models import RAppInstance


class InstanceState(StrEnum):
    """The lifecycle states of one instance row: DEPLOYING (created, waiting for the workload to call bootstrap-complete), RUNNING, UPGRADING (the old row of an upgrade
    in flight), FAULTED (a critical fault, or a failed bootstrap) and UNDEPLOYED (torn down; the row remains until it is deleted). The names are stored in `rapp_instance.state`, so they are never renamed.
    """
    DEPLOYING = "DEPLOYING"
    RUNNING = "RUNNING"
    UPGRADING = "UPGRADING"
    UNDEPLOYED = "UNDEPLOYED"
    FAULTED = "FAULTED"


class InstanceEvent(StrEnum):
    """What can happen to an instance row: bootstrap succeeded or failed, an upgrade started, committed or rolled back, terminate, crash and recover. Which event is legal in which
    state is the table in `build_rapp_instance_fsm`.
    """
    BOOTSTRAP_OK = "BOOTSTRAP_OK"
    BOOTSTRAP_FAILED = "BOOTSTRAP_FAILED"
    START_UPGRADE = "START_UPGRADE"
    UPGRADE_COMMIT = "UPGRADE_COMMIT"      # this row's replacement succeeded — this row is going away
    UPGRADE_ROLLBACK = "UPGRADE_ROLLBACK"  # replacement failed/timed out — this row stays, unaffected
    TERMINATE = "TERMINATE"
    CRASH = "CRASH"
    RECOVER = "RECOVER"                    # manual recovery / re-deploy, per v1.3's own FAULTED exit


def _revoke_credential(instance: RAppInstance, **_) -> None:
    """Credential revocation as an explicit, required sub-step of
    TerminateInstance — not a separate operation (closes v1.3's RT-3
    red-team finding).
    """
    if instance.oauth_client_id is not None:
        # PR-SEC-14: the invoker SME holds for this instance goes with it, and with it every token it was granted. Best-effort, like the
        # other deregistrations: an unreachable SME must not stop a terminate.
        with suppress(httpx.HTTPError):
            R1Client().delete(f"/sme/invoker-registrations/{instance.oauth_client_id}")
    instance.oauth_client_id = None


def _reconsider_dme_registration(instance: RAppInstance, **_) -> None:
    """rApp-as-producer reconsideration trigger (HISTORY.md §1):
    when this instance crashes or terminates it can no longer be trusted
    as a live DME producer, so its own DME registrations — keyed by
    producer_id, which is this instance's oauth_client_id (bootstrap
    registers with SME/DME using that same identity) — are deregistered
    here. Best-effort: a DME outage must never block CRASH/TERMINATE
    themselves, the same "unreachable callback never fails the primary
    operation" precedent Policy Mgmt's CreateIntent dispatch uses.
    """
    if instance.oauth_client_id is None:
        return
    try:
        R1Client().delete("/dme/production-capabilities", params={"producer_id": instance.oauth_client_id})
    except httpx.HTTPError:
        pass


def _close_dme_data_jobs(instance: RAppInstance, **_) -> None:
    """OI-7-rapp-data-jobs-teardown: close the DME data jobs this instance opened as a consumer. The rApp acts as the instance (call flow 01c), so DME
    recorded the instance's own invoker id as each job's `created_by`, and every job of it is deleted in one call (`DELETE /dme/data-jobs?created_by=`;
    DME tells the producers). Left open, the jobs kept receiving every record of their type, and every PM fan-out of RAN NF OAM grew with each
    terminate-and-redeploy. Run on the moves into UNDEPLOYED only, not on a crash: a crashed instance keeps its identity for a recover, and its jobs with
    it. Best-effort, like the deregistrations: an unreachable DME must not stop a terminate. Jobs an rApp opened under its own identity (no instance
    credentials) are not the instance's to close."""
    if instance.oauth_client_id is None:
        return
    try:
        R1Client().delete("/dme/data-jobs", params={"created_by": instance.oauth_client_id})
    except httpx.HTTPError:
        pass


def _reconsider_sme_registration(instance: RAppInstance, **_) -> None:
    """HISTORY.md §7's Onboarding/rApp Mgmt finding 3 (SME auto-
    registration): the mirror image of _reconsider_dme_registration
    above, for the SME provider/service-API registrations bootstrap-
    complete made using this same oauth_client_id as its SME apfId. Real
    O-RAN SC rApp Manager behavior (SmeDeployer.undeployRappInstance) —
    deregister each published service API first, then the provider
    domain itself. Best-effort, same reasoning as the DME case: an
    unreachable SME must never block CRASH/TERMINATE.
    """
    if instance.oauth_client_id is None:
        return
    apf_id = instance.oauth_client_id
    r1 = R1Client()
    try:
        for service_id in (instance.sme_service_ids or []):
            r1.delete(f"/sme/published-apis/v1/{apf_id}/service-apis/{service_id}")
        r1.delete(f"/sme/provider-registrations/{apf_id}")
    except httpx.HTTPError:
        pass


def _reconsider_rapp_limits(instance: RAppInstance, **_) -> None:
    """AI-10.2: drop the limit RAN NF OAM holds for this instance's client id (set at bootstrap-complete). Best-effort: a limit left behind
    limits a client id that is revoked next. An instance whose package declared none has nothing to delete."""
    if instance.oauth_client_id is None or not instance.rapp_limits_set:
        return
    try:
        R1Client().delete(f"/ran-nf-oam/rapp-limits/{instance.oauth_client_id}")
    except httpx.HTTPError:
        pass


def _reconsider_approval_policy(instance: RAppInstance, **_) -> None:
    """AI-11.4: drop the approval policy RAN NF OAM holds for this instance's client id. Best-effort, like the limit: a policy left behind holds a client id
    that is revoked next. Requests already waiting stay in the queue."""
    if instance.oauth_client_id is None or not instance.approval_policy:
        return
    try:
        R1Client().delete(f"/ran-nf-oam/rapp-approval-policy/{instance.oauth_client_id}")
    except httpx.HTTPError:
        pass


def _drop_kill_switch(oauth_client_id: str | None) -> None:
    """Call flow 07e: remove the kill switch RAN NF OAM may hold for a revoked identity, so no stop is left behind for a client id that no longer exists.
    Best-effort, like the deregistrations: the credential is already revoked, so a record left behind stops nothing that could still write."""
    if oauth_client_id is None:
        return
    with suppress(httpx.HTTPError):
        R1Client().delete(f"/ran-nf-oam/rapp-kill/{oauth_client_id}")


def _reconsider_discovery(instance: RAppInstance, **_) -> None:
    """The action of CRASH (call flow 07a): a crashed instance is no longer offered to others, so its DME and SME registrations go. Its rate limit and
    approval policy stay at RAN NF OAM: the instance keeps its credential (for a recover), and a limit removed here would leave that credential free of
    them, since RAN NF OAM does not limit a caller it holds no limit for. They go when the instance is terminated."""
    _reconsider_dme_registration(instance)
    _reconsider_sme_registration(instance)


def _reconsider_registrations(instance: RAppInstance, **_) -> None:
    """Every registration made for the instance: DME, SME, then its rate limit and approval policy at RAN NF OAM (run as the instance goes UNDEPLOYED)."""
    _reconsider_discovery(instance)
    _reconsider_rapp_limits(instance)
    _reconsider_approval_policy(instance)


def _terminate_side_effects(instance: RAppInstance, **_) -> None:
    # Reconsideration must run BEFORE credential revocation — it reads
    # instance.oauth_client_id as the DME producer_id/SME apfId, which
    # _revoke_credential clears to None.
    """The transition action of every move into UNDEPLOYED: deregister the instance at DME, SME, RAN NF OAM (limit and approval policy), close its DME data jobs, revoke its
    credential, then drop its kill switch. The order matters: the deregistrations read `oauth_client_id` as the producer and provider id, and `_revoke_credential` sets it to
    None; the kill switch goes only once the credential is revoked, so the instance is never free to write in between.
    """
    oauth_client_id = instance.oauth_client_id
    _reconsider_registrations(instance)
    _close_dme_data_jobs(instance)
    _revoke_credential(instance)
    _drop_kill_switch(oauth_client_id)


def build_rapp_instance_fsm() -> StateMachine[InstanceState, InstanceEvent]:
    """Builds the transition table: which `InstanceEvent` is legal in which `InstanceState`, the state it leads to and the side-effect action it runs. Every move into UNDEPLOYED
    (terminate, an upgrade commit) runs `_terminate_side_effects`; a crash runs only the DME and SME deregistrations, so the credential stays for a recover and the rate
    limit and approval policy stay with it. A failed bootstrap (a critical fault while DEPLOYING) has nothing to undo: nothing is registered before bootstrap-complete. An event that is not in the table is
    refused by the caller as an illegal transition (409). The table is built once, as `RAPP_INSTANCE_FSM`.
    """
    fsm: StateMachine[InstanceState, InstanceEvent] = StateMachine()
    fsm.add(InstanceState.DEPLOYING, InstanceEvent.BOOTSTRAP_OK, InstanceState.RUNNING)
    fsm.add(InstanceState.DEPLOYING, InstanceEvent.BOOTSTRAP_FAILED, InstanceState.FAULTED)
    fsm.add(InstanceState.RUNNING, InstanceEvent.START_UPGRADE, InstanceState.UPGRADING)
    # UPGRADE_COMMIT retires the old row exactly like TERMINATE does (DME/SME
    # deregistration, then credential revocation); upgrade.py additionally
    # releases its NFO workload and usage registration (OI-1-upgrade-identity).
    fsm.add(InstanceState.UPGRADING, InstanceEvent.UPGRADE_COMMIT, InstanceState.UNDEPLOYED, action=_terminate_side_effects)
    fsm.add(InstanceState.UPGRADING, InstanceEvent.UPGRADE_ROLLBACK, InstanceState.RUNNING)
    fsm.add(InstanceState.RUNNING, InstanceEvent.TERMINATE, InstanceState.UNDEPLOYED, action=_terminate_side_effects)
    # OI-2-lcm-error-mapping: a crashed instance can be retired without
    # recovering it first, and so can one whose container never called
    # bootstrap-complete — DEPLOYING has no other exit except RECOVER's
    # re-entry into it, and its NFO workload and usage registration already
    # exist (CreateInstance made them), so they need the same teardown.
    # Deregistration is idempotent and best-effort, so repeating it after
    # CRASH already did it is harmless. UPGRADING stays excluded: an upgrade
    # in flight is resolved (commit/rollback) first.
    fsm.add(InstanceState.FAULTED, InstanceEvent.TERMINATE, InstanceState.UNDEPLOYED, action=_terminate_side_effects)
    fsm.add(InstanceState.DEPLOYING, InstanceEvent.TERMINATE, InstanceState.UNDEPLOYED, action=_terminate_side_effects)
    fsm.add(InstanceState.RUNNING, InstanceEvent.CRASH, InstanceState.FAULTED, action=_reconsider_discovery)
    fsm.add(InstanceState.FAULTED, InstanceEvent.RECOVER, InstanceState.DEPLOYING)
    return fsm


RAPP_INSTANCE_FSM = build_rapp_instance_fsm()
