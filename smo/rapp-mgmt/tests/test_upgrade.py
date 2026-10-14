"""Tests for RAppInstance lifecycle + upgrade auto-rollback
(Onboarding/rApp Mgmt LLD sections 5-6). Run with: pytest smo/rapp-mgmt/tests -q

The upgrade orchestration provisions and tears down real resources through
R1 (OI-2-upgrade-completeness), so these tests fake R1Client and record
every call it makes.
"""

import datetime
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import Column, Table, Uuid, create_engine
from sqlalchemy.orm import Session

from smo_shared.db import Base
from smo_shared.r1_client import R1Client
from smo_shared.statemachine import IllegalTransition

from app.models import RAppFaultReport, RAppInstance, RAppInstanceVersion, RAppPerformanceReport
from app.statemachine import RAPP_INSTANCE_FSM, InstanceEvent, InstanceState
from app.upgrade import expire_overdue_upgrade, resolve_upgrade, start_upgrade, upgrade_deadline


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeR1:
    """Answers onboarding-status / NFO / usage / DME / SME calls; records them."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.package_states: dict[str, str] = {}
        self.nfo_delete_status = 204
        self.stopped: dict[str, dict] = {}     # call flow 07b: the kill switches RAN NF OAM holds, by invoker id
        self.kill_put_status = 200

    def get(self, _self, path, **kw):
        self.calls.append(("GET", path))
        if path.startswith("/ran-nf-oam/rapp-kill/"):
            stop = self.stopped.get(path.rsplit("/", 1)[1])
            return FakeResponse(200, stop) if stop is not None else FakeResponse(404, {})
        package_id = path.split("/")[3]
        state = self.package_states.get(package_id, "AVAILABLE")
        if state == "UNKNOWN":
            return FakeResponse(404, {})
        return FakeResponse(200, {"state": state, "nfDeploymentDescriptorId": str(uuid.uuid4())})

    def post(self, _self, path, json=None, **kw):
        self.calls.append(("POST", path))
        if path == "/nfo/deployments":
            return FakeResponse(202, {"nfDeploymentId": str(uuid.uuid4())})
        if path.endswith("/usage/start"):
            return FakeResponse(200, {"registrationId": str(uuid.uuid4())})
        if path == "/sme/invoker-registrations":
            return FakeResponse(201, {"apiInvokerId": f"api-invoker-{uuid.uuid4()}", "onboardingSecret": "s", "role": "rapp"})
        return FakeResponse(200, {})

    def put(self, _self, path, json=None, **kw):
        self.calls.append(("PUT", path))
        return FakeResponse(self.kill_put_status, {})

    def delete(self, _self, path, **kw):
        self.calls.append(("DELETE", path))
        if path.startswith("/nfo/deployments/"):
            return FakeResponse(self.nfo_delete_status)
        return FakeResponse(204)


@pytest.fixture
def r1(monkeypatch):
    """Replaces `R1Client`'s get, post and delete with a `FakeR1` that answers like Onboarding, NFO, SME and DME and records every call in `fake.calls`.
    """
    fake = FakeR1()
    monkeypatch.setattr(R1Client, "get", lambda self, path, **kw: fake.get(self, path, **kw))
    monkeypatch.setattr(R1Client, "post", lambda self, path, json=None, **kw: fake.post(self, path, json=json, **kw))
    monkeypatch.setattr(R1Client, "put", lambda self, path, json=None, **kw: fake.put(self, path, json=json, **kw))
    monkeypatch.setattr(R1Client, "delete", lambda self, path, **kw: fake.delete(self, path, **kw))
    return fake


@pytest.fixture
def db():
    """A SQLite session with the rApp Management tables (and a stand-in `application_package` table, which belongs to Onboarding) so the instance rows can be written and read.
    """
    engine = create_engine("sqlite://")
    # application_package lives in the onboarding module, out of scope for this
    # test package — stand in a minimal table so RAppInstance's FK resolves.
    # Production runs against the full consolidated migration (001_init.sql).
    if "application_package" not in Base.metadata.tables:
        Table("application_package", Base.metadata, Column("package_id", Uuid, primary_key=True))
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["application_package"], RAppInstance.__table__,
                                             RAppFaultReport.__table__, RAppPerformanceReport.__table__,
                                             RAppInstanceVersion.__table__])
    with Session(engine) as session:
        yield session


def running_instance(db, package_id=None, **kw) -> RAppInstance:
    """Adds a RUNNING instance with a client id, an NFO workload and a usage registration (`kw` overrides columns) and returns it flushed.
    """
    inst = RAppInstance(package_id=package_id or uuid.uuid4(), state=InstanceState.RUNNING, oauth_client_id="cred-123",
                        workload_ref="nf-old", package_usage_registration_id=uuid.uuid4(), **kw)
    db.add(inst)
    db.flush()
    return inst


def test_create_instance_bootstrap_success(db):
    """The transition table takes DEPLOYING to RUNNING on BOOTSTRAP_OK."""
    inst = RAppInstance(package_id=uuid.uuid4(), state=InstanceState.DEPLOYING)
    db.add(inst)
    db.flush()
    new_state = RAPP_INSTANCE_FSM.fire(InstanceState.DEPLOYING, InstanceEvent.BOOTSTRAP_OK, instance=inst)
    assert new_state == InstanceState.RUNNING


def test_terminate_revokes_credential(db, r1):
    """Closes v1.3's red-team finding RT-3: revocation is part of the
    TerminateInstance transition itself, not a separate step.
    """
    inst = running_instance(db)
    assert inst.oauth_client_id is not None
    new_state = RAPP_INSTANCE_FSM.fire(InstanceState.RUNNING, InstanceEvent.TERMINATE, instance=inst)
    assert new_state == InstanceState.UNDEPLOYED
    assert inst.oauth_client_id is None


@pytest.mark.parametrize("state", [InstanceState.FAULTED, InstanceState.DEPLOYING])
def test_terminate_is_legal_from_faulted_and_deploying(db, r1, state):
    """OI-2-lcm-error-mapping: a crashed instance (or one that never
    bootstrapped) can be retired without recovering it first."""
    inst = running_instance(db)
    assert RAPP_INSTANCE_FSM.fire(state, InstanceEvent.TERMINATE, instance=inst) == InstanceState.UNDEPLOYED
    assert inst.oauth_client_id is None


# Each row is a state in which terminate must be refused: UPGRADING (resolve the upgrade first) and UNDEPLOYED (already torn down).
@pytest.mark.parametrize("state", [InstanceState.UPGRADING, InstanceState.UNDEPLOYED])
def test_terminate_is_illegal_while_upgrading_or_once_undeployed(db, state):
    with pytest.raises(IllegalTransition):
        RAPP_INSTANCE_FSM.fire(state, InstanceEvent.TERMINATE, instance=running_instance(db))


def test_start_upgrade_provisions_a_complete_replacement(db, r1):
    """OI-2-upgrade-completeness / OI-1-upgrade-identity: the replacement
    goes through CreateInstance's own path — package check, its own fresh
    identity, NFO Instantiate, usage/start — and inherits configuration,
    autonomy mode and region scope."""
    new_package = uuid.uuid4()
    old = running_instance(db, configuration={"replicas": 2}, autonomy_mode="AUTONOMOUS",
                           region_scope={"nodeIds": ["ne-1"]}, upgrade_timeout_seconds=120)

    new = start_upgrade(db, old, new_package)

    assert old.state == InstanceState.UPGRADING
    assert old.pending_upgrade_instance_id == new.instance_id
    assert new.state == InstanceState.DEPLOYING
    assert new.package_id == new_package
    assert new.configuration == {"replicas": 2} and new.configuration is not old.configuration
    assert (new.autonomy_mode, new.region_scope) == ("AUTONOMOUS", {"nodeIds": ["ne-1"]})
    assert new.oauth_client_id and new.oauth_client_id != old.oauth_client_id
    assert new.workload_ref and new.package_usage_registration_id is not None
    assert new.upgrade_timeout_seconds == 120
    assert ("GET", f"/onboarding/packages/{new_package}/onboarding-status") in r1.calls
    assert ("POST", "/nfo/deployments") in r1.calls
    assert ("POST", f"/onboarding/packages/{new_package}/usage/start") in r1.calls


# Each row is a package state the replacement may not be made from (DEPRECATED, FAILED: 409; unknown: 404). The old instance stays RUNNING with no pending upgrade and nothing is instantiated.
@pytest.mark.parametrize("package_state,status", [("DEPRECATED", 409), ("FAILED", 409), ("UNKNOWN", 404)])
def test_start_upgrade_refuses_a_package_that_is_not_deployable(db, r1, package_state, status):
    new_package = uuid.uuid4()
    r1.package_states[str(new_package)] = package_state
    old = running_instance(db)

    with pytest.raises(HTTPException) as exc:
        start_upgrade(db, old, new_package)

    assert exc.value.status_code == status
    assert old.state == InstanceState.RUNNING and old.pending_upgrade_instance_id is None
    assert not any(path == "/nfo/deployments" for _, path in r1.calls)


def test_start_upgrade_refuses_a_non_running_instance_before_provisioning(db, r1):
    """Starting an upgrade of a non-RUNNING instance raises before any R1 call, so no replacement resources are created."""
    old = running_instance(db)
    old.state = InstanceState.FAULTED
    with pytest.raises(IllegalTransition):
        start_upgrade(db, old, uuid.uuid4())
    assert r1.calls == [] and old.state == InstanceState.FAULTED


def test_upgrade_success_commits_and_retires_old_row_like_terminate(db, r1):
    """Committing an upgrade makes the replacement RUNNING under its own identity and retires the old row like a terminate, in this order: DME and SME deregistration, the old invoker, NFO terminate, usage stop; the teardown is recorded on the replacement and the old row is deleted.
    """
    old_package = uuid.uuid4()
    new_package = uuid.uuid4()
    old = running_instance(db, package_id=old_package, sme_service_ids=["svc-1"])
    old_id, old_registration = old.instance_id, old.package_usage_registration_id

    new = start_upgrade(db, old, new_package)
    new_identity = new.oauth_client_id
    registered = []
    r1.calls.clear()

    resolve_upgrade(db, old, new, new_bootstrap_succeeded=True, register_identity=registered.append)
    db.flush()

    assert new.state == InstanceState.RUNNING
    assert new.package_id == new_package
    assert new.oauth_client_id == new_identity  # the replacement keeps its own identity
    assert registered == [new]  # SME declarations registered under the replacement's identity
    # the old row is retired like a TERMINATE: DME/SME deregistration, NFO Terminate, usage/stop
    assert r1.calls == [
        ("DELETE", "/dme/production-capabilities"),
        ("DELETE", "/sme/published-apis/v1/cred-123/service-apis/svc-1"),
        ("DELETE", "/sme/provider-registrations/cred-123"),
        ("DELETE", "/dme/data-jobs"),                                  # OI-7-rapp-data-jobs-teardown
        ("DELETE", "/sme/invoker-registrations/cred-123"),            # PR-SEC-14: the retired instance's own invoker
        ("DELETE", "/ran-nf-oam/rapp-kill/cred-123"),                 # call flow 07e: no stop left for the retired identity
        ("DELETE", "/nfo/deployments/nf-old"),
        ("POST", f"/onboarding/packages/{old_package}/usage/{old_registration}/stop"),
    ]
    assert new.last_teardown["instanceId"] == str(old_id)
    assert (new.last_teardown["reason"], new.last_teardown["nfoTerminate"], new.last_teardown["usageStop"]) == (
        "UPGRADE_COMMIT", "DONE", "DONE")
    assert db.get(RAppInstance, old_id) is None


def test_upgrade_commit_of_a_replacement_that_already_bootstrapped(db, r1):
    """The replacement's container may call bootstrap-complete itself
    before the outcome is resolved — then it is already RUNNING and already
    registered."""
    old = running_instance(db)
    new = start_upgrade(db, old, uuid.uuid4())
    new.state = InstanceState.RUNNING
    registered = []

    resolve_upgrade(db, old, new, new_bootstrap_succeeded=True, register_identity=registered.append)

    assert new.state == InstanceState.RUNNING and registered == []


def test_upgrade_commit_refused_when_the_replacement_crashed(db, r1):
    """If the replacement has FAULTED, committing is refused and nothing is released: the old instance stays UPGRADING with its credential.
    """
    old = running_instance(db)
    new = start_upgrade(db, old, uuid.uuid4())
    new.state = InstanceState.FAULTED
    r1.calls.clear()

    with pytest.raises(IllegalTransition):
        resolve_upgrade(db, old, new, new_bootstrap_succeeded=True)

    assert old.state == InstanceState.UPGRADING and old.oauth_client_id == "cred-123"
    assert r1.calls == []


def test_upgrade_failure_auto_rolls_back_and_tears_the_replacement_down(db, r1):
    """The auto-rollback decision v1.3 made explicit: failure never
    requires manual intervention, and the OLD instance is unaffected
    throughout — it never left RUNNING in the caller's observable sense
    except for the UPGRADING marker during the attempt. The replacement's
    NFO deployment and usage registration are released before its row goes.
    """
    old_package = uuid.uuid4()
    new_package = uuid.uuid4()
    old = running_instance(db, package_id=old_package)
    original_credential = old.oauth_client_id

    new = start_upgrade(db, old, new_package)
    new_id, new_workload, new_registration = new.instance_id, new.workload_ref, new.package_usage_registration_id
    r1.calls.clear()
    resolve_upgrade(db, old, new, new_bootstrap_succeeded=False)
    db.flush()

    assert old.state == InstanceState.RUNNING
    assert old.package_id == old_package  # never changed
    assert old.oauth_client_id == original_credential  # never revoked — this instance was never terminated
    assert old.pending_upgrade_instance_id is None
    assert ("DELETE", f"/nfo/deployments/{new_workload}") in r1.calls
    assert ("POST", f"/onboarding/packages/{new_package}/usage/{new_registration}/stop") in r1.calls
    assert ("DELETE", "/nfo/deployments/nf-old") not in r1.calls
    assert old.last_teardown["instanceId"] == str(new_id) and old.last_teardown["reason"] == "UPGRADE_ROLLBACK"
    assert db.get(RAppInstance, new_id) is None


def test_overdue_upgrade_rolls_back_lazily(db, r1):
    """upgradeTimeoutSeconds is enforced: an unresolved upgrade past its
    deadline rolls back the next time it is looked at."""
    old = running_instance(db, upgrade_timeout_seconds=60)
    new = start_upgrade(db, old, uuid.uuid4())
    new_id = new.instance_id
    deadline = upgrade_deadline(old, new)

    assert expire_overdue_upgrade(db, old, now=deadline - datetime.timedelta(seconds=1)) is False
    assert old.state == InstanceState.UPGRADING

    assert expire_overdue_upgrade(db, old, now=deadline) is True
    db.flush()
    assert old.state == InstanceState.RUNNING and old.pending_upgrade_instance_id is None
    assert old.last_teardown["reason"] == "UPGRADE_TIMEOUT"
    assert db.get(RAppInstance, new_id) is None
    assert expire_overdue_upgrade(db, old) is False  # nothing pending any more


def test_nfo_terminate_failure_is_recorded_not_raised(db, r1):
    """A failing NFO terminate during teardown does not stop it; the failure is recorded in `last_teardown`."""
    r1.nfo_delete_status = 503
    old = running_instance(db)
    new = start_upgrade(db, old, uuid.uuid4())
    resolve_upgrade(db, old, new, new_bootstrap_succeeded=False)
    assert old.state == InstanceState.RUNNING
    assert old.last_teardown["nfoTerminate"] == "FAILED: HTTP 503"


def test_crash_and_manual_recovery(db, r1):
    """CRASH takes RUNNING to FAULTED and RECOVER takes FAULTED back to DEPLOYING."""
    inst = running_instance(db)
    s = RAPP_INSTANCE_FSM.fire(InstanceState.RUNNING, InstanceEvent.CRASH, instance=inst)
    assert s == InstanceState.FAULTED
    s = RAPP_INSTANCE_FSM.fire(s, InstanceEvent.RECOVER, instance=inst)
    assert s == InstanceState.DEPLOYING


# ---------------------------------------------------------------- call flow 07: crash safeguards, the kill switch across an upgrade, the replacement's address

def test_a_crash_keeps_the_rate_limit_and_approval_policy_and_terminate_drops_them(db, r1):
    """Call flow 07a: a crashed instance keeps its credential (for a recover), so its rate limit and approval policy stay at RAN NF OAM; only its DME and
    SME registrations go. Terminating it then drops them, and its kill switch, after the credential is revoked."""
    inst = running_instance(db, approval_policy={"timeoutSeconds": 60, "onTimeout": "EXPIRE"})
    inst.rapp_limits_set = True

    RAPP_INSTANCE_FSM.fire(InstanceState.RUNNING, InstanceEvent.CRASH, instance=inst)

    paths = [path for _, path in r1.calls]
    assert "/ran-nf-oam/rapp-limits/cred-123" not in paths and "/ran-nf-oam/rapp-approval-policy/cred-123" not in paths
    assert "/dme/production-capabilities" in paths and "/sme/provider-registrations/cred-123" in paths
    r1.calls.clear()

    RAPP_INSTANCE_FSM.fire(InstanceState.FAULTED, InstanceEvent.TERMINATE, instance=inst)

    paths = [path for _, path in r1.calls]
    assert paths.index("/ran-nf-oam/rapp-limits/cred-123") < paths.index("/sme/invoker-registrations/cred-123") < paths.index("/ran-nf-oam/rapp-kill/cred-123")
    assert "/ran-nf-oam/rapp-approval-policy/cred-123" in paths


def test_a_failed_bootstrap_takes_a_deploying_instance_to_faulted(db):
    """Call flow 07a: BOOTSTRAP_FAILED takes DEPLOYING to FAULTED, from where the instance is recovered or terminated."""
    inst = running_instance(db)
    assert RAPP_INSTANCE_FSM.fire(InstanceState.DEPLOYING, InstanceEvent.BOOTSTRAP_FAILED, instance=inst) == InstanceState.FAULTED


def test_the_replacement_of_a_stopped_instance_is_stopped_too(db, r1):
    """Call flow 07b: an upgrade never releases a stopped rApp. The replacement gets the same stop (same author, the reason naming the old instance)."""
    r1.stopped["cred-123"] = {"invokerId": "cred-123", "killedBy": "alice", "reason": "storm"}
    old = running_instance(db)

    new = start_upgrade(db, old, uuid.uuid4())

    assert ("PUT", f"/ran-nf-oam/rapp-kill/{new.oauth_client_id}") in r1.calls
    assert old.state == InstanceState.UPGRADING


def test_an_unstopped_instance_upgrades_without_a_stop(db, r1):
    """Call flow 07b: when the old instance is not stopped, nothing is put on the replacement."""
    start_upgrade(db, running_instance(db), uuid.uuid4())
    assert not any(method == "PUT" for method, _ in r1.calls)


def test_an_upgrade_is_not_started_when_ran_nf_oam_cannot_say_whether_the_instance_is_stopped(db, r1, monkeypatch):
    """Call flow 07b: 503 before anything is provisioned when the stop cannot be read, so an upgrade never starts on a guess."""
    original = r1.get
    monkeypatch.setattr(r1, "get", lambda _self, path, **kw: FakeResponse(503) if "rapp-kill" in path else original(_self, path, **kw))
    old = running_instance(db)

    with pytest.raises(HTTPException) as exc:
        start_upgrade(db, old, uuid.uuid4())

    assert exc.value.status_code == 503
    assert old.state == InstanceState.RUNNING and old.pending_upgrade_instance_id is None
    assert not any(path == "/nfo/deployments" for _, path in r1.calls)


def test_a_replacement_ran_nf_oam_does_not_stop_is_torn_down_again(db, r1):
    """Call flow 07b: when the replacement's stop is refused, its identity, workload and usage registration are released and the upgrade is refused (503)."""
    r1.stopped["cred-123"] = {"invokerId": "cred-123", "killedBy": "alice"}
    r1.kill_put_status = 500
    old = running_instance(db)

    with pytest.raises(HTTPException) as exc:
        start_upgrade(db, old, uuid.uuid4())

    assert exc.value.status_code == 503
    assert old.state == InstanceState.RUNNING
    paths = [path for _, path in r1.calls]
    assert any(p.startswith("/sme/invoker-registrations/api-invoker-") for p in paths)
    assert any(p.startswith("/nfo/deployments/") for p in paths)
    assert any(p.endswith("/stop") for p in paths)


def test_the_replacement_is_reached_at_the_new_package_address_else_the_old_instances(db, r1, monkeypatch):
    """Call flow 07b: the replacement's operator API address is the one the new package declares; without one, the old instance's."""
    old = running_instance(db, operator_api_base="http://old-rapp:8000")
    assert start_upgrade(db, old, uuid.uuid4()).operator_api_base == "http://old-rapp:8000"

    original = r1.get
    def declaring(_self, path, **kw):
        resp = original(_self, path, **kw)
        if path.endswith("/onboarding-status"):
            resp._payload["aiCapabilities"] = {"operatorApi": {"baseUrl": "http://new-rapp:8000"}}
        return resp
    monkeypatch.setattr(r1, "get", declaring)
    other = running_instance(db, operator_api_base="http://old-rapp:8000")
    other.oauth_client_id = "cred-456"
    assert start_upgrade(db, other, uuid.uuid4()).operator_api_base == "http://new-rapp:8000"


def test_an_unreachable_ran_nf_oam_refuses_the_upgrade_and_tears_the_stopped_replacement_down(db, r1, monkeypatch):
    """Call flow 07b: no answer from RAN NF OAM is treated like a refusal, whether reading the old instance's stop (nothing provisioned) or putting it on
    the replacement (the replacement released again); both are 503."""
    import httpx

    def unreachable(*_a, **_kw):
        raise httpx.ConnectError("down")

    original_get = r1.get
    monkeypatch.setattr(r1, "get", lambda _self, path, **kw: unreachable() if "rapp-kill" in path else original_get(_self, path, **kw))
    old = running_instance(db)
    with pytest.raises(HTTPException) as exc:
        start_upgrade(db, old, uuid.uuid4())
    assert exc.value.status_code == 503 and not any(p == "/nfo/deployments" for _, p in r1.calls)

    monkeypatch.setattr(r1, "get", original_get)
    monkeypatch.setattr(r1, "put", unreachable)
    r1.stopped["cred-123"] = {"invokerId": "cred-123", "killedBy": "alice"}
    with pytest.raises(HTTPException) as exc:
        start_upgrade(db, old, uuid.uuid4())
    assert exc.value.status_code == 503 and old.state == InstanceState.RUNNING
    assert any(p.startswith("/nfo/deployments/") for m, p in r1.calls if m == "DELETE")


def test_an_instance_without_a_credential_has_no_stop_to_read_or_drop(db, r1):
    """Call flow 07b/07e: with no credential there is no invoker id, so the upgrade reads no stop and a terminate drops none."""
    old = running_instance(db)
    old.oauth_client_id = None
    start_upgrade(db, old, uuid.uuid4())
    assert not any("rapp-kill" in p for _, p in r1.calls)

    other = running_instance(db)
    other.oauth_client_id = None
    r1.calls.clear()
    RAPP_INSTANCE_FSM.fire(InstanceState.RUNNING, InstanceEvent.TERMINATE, instance=other)
    assert not any("rapp-kill" in p for _, p in r1.calls)


def test_an_unreachable_ran_nf_oam_does_not_stop_a_terminate(db, r1, monkeypatch):
    """Call flow 07e: dropping the kill switch is best-effort, after the credential is revoked; a terminate goes through without RAN NF OAM."""
    import httpx
    original = r1.delete

    def delete(_self, path, **kw):
        if "rapp-kill" in path:
            raise httpx.ConnectError("down")
        return original(_self, path, **kw)
    monkeypatch.setattr(r1, "delete", delete)
    inst = running_instance(db)
    assert RAPP_INSTANCE_FSM.fire(InstanceState.RUNNING, InstanceEvent.TERMINATE, instance=inst) == InstanceState.UNDEPLOYED
    assert inst.oauth_client_id is None
