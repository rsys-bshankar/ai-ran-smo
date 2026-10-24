"""Tests for rApp Management's routes (Onboarding/rApp Mgmt LLD section 5-6)
— CreateInstance's usage-registration wiring, TerminateInstance's
usage/stop call, and the RECOVER route, none of which had route-level
coverage before (only the FSM itself, in test_upgrade.py).
Run with: pytest smo/rapp-mgmt/tests -q
"""

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Table, Uuid as UuidType, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from smo_shared.db import Base, get_session
from smo_shared.idempotency import IdempotencyKey
from smo_shared.testing import concurrent_commit_on

from app.main import app
from app.models import RAppFaultReport, RAppInstance, RAppInstanceVersion, RAppPerformanceReport
from app.statemachine import InstanceState


class FakeR1Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def db_session_factory():
    """A session factory on one shared in-memory SQLite database holding the rApp Management tables and the idempotency table, with stand-in `application_package` and `package_usage_registration` tables (they belong to Onboarding). The routes and the tests see the same data.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    # application_package and package_usage_registration live in the onboarding
    # module, out of scope for this test package — stand in minimal tables so
    # RAppInstance's FKs resolve, same pattern as rapp-mgmt/tests/test_upgrade.py.
    if "application_package" not in Base.metadata.tables:
        Table("application_package", Base.metadata, Column("package_id", UuidType, primary_key=True))
    if "package_usage_registration" not in Base.metadata.tables:
        Table("package_usage_registration", Base.metadata, Column("id", UuidType, primary_key=True))
    Base.metadata.create_all(engine, tables=[
        Base.metadata.tables["application_package"], Base.metadata.tables["package_usage_registration"],
        RAppInstance.__table__, RAppFaultReport.__table__, RAppPerformanceReport.__table__,
        RAppInstanceVersion.__table__, IdempotencyKey.__table__,
    ])
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def fake_r1_delete(monkeypatch):
    """TERMINATE now calls NFO's DELETE /nfo/deployments/{workloadRef}
    (OI-2-terminate-workload) — every test gets a recording 204 fake for
    R1Client.delete unless it patches its own, so no test reaches for a
    real network."""
    calls = []

    def fake_delete(self, path, params=None, **kw):
        calls.append((path, params))
        return FakeR1Response(204, {})

    monkeypatch.setattr("app.main.R1Client.delete", fake_delete)
    return calls


@pytest.fixture
def client(db_session_factory):
    """A `TestClient` of the rApp Management app whose database session comes from `db_session_factory`; the override is removed after the test.
    """
    def override_get_session():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _route_r1_get_post(*, onboarding_status="AVAILABLE", registration_id=None, sme_declarations=None, sme_service_id=None, ai_capabilities=None, sme_status=201):
    """Builds a fake R1Client.get/post pair that answers each of
    CreateInstance's three downstream calls (onboarding-status, NFO
    deploy, usage/start) based on the path, since they all go through
    the same R1Client instance. onboarding-status includes a
    nfDeploymentDescriptorId — CreateInstance now requires it (the
    NFDeploymentDescriptor fix), so a fake response without it would
    incorrectly 409 before ever reaching the usage-registration wiring
    this suite actually tests. `sme_declarations` (HISTORY.md §7's
    Onboarding/rApp Mgmt finding 3) is None by default — bootstrap-
    complete's own SME auto-registration then finds nothing to do,
    covering every existing call site of this helper unchanged.
    """
    reg_id = registration_id or uuid.uuid4()
    service_id = sme_service_id or uuid.uuid4()

    def fake_get(self, path, **kw):
        if path.startswith("/nfo/deployments/"):              # call flow 15e: the detail reads the workload's state
            return FakeR1Response(200, {"state": "RUNNING", "abnormalReason": None})
        if path.startswith("/ran-nf-oam/rapp-kill/"):          # call flow 07b: an upgrade asks whether the old instance is stopped
            stop = fake_get.stopped.get(path.rsplit("/", 1)[1])
            return FakeR1Response(200, stop) if stop is not None else FakeR1Response(404, {})
        assert "/onboarding-status" in path
        return FakeR1Response(200, {
            "state": onboarding_status, "nfDeploymentDescriptorId": str(uuid.uuid4()),
            "smeDeclarations": sme_declarations, "aiCapabilities": ai_capabilities,
        })

    def fake_post(self, path, json=None, **kw):
        if "/nfo/deployments" in path:
            return FakeR1Response(200, {"nfDeploymentId": str(uuid.uuid4())})
        if "/usage/start" in path:
            return FakeR1Response(200, {"registrationId": str(reg_id)})
        if path == "/sme/invoker-registrations":          # PR-SEC-14: each instance gets its own invoker identity
            return FakeR1Response(201, {"apiInvokerId": f"api-invoker-{uuid.uuid4()}", "onboardingSecret": "s3cret", "role": "rapp"})
        if "/usage/" in path and path.endswith("/stop"):
            return FakeR1Response(200, {"status": "stopped"})
        if "/sme/provider-registrations" in path:
            return FakeR1Response(201, {"apfId": json["apfId"]})
        if "/sme/published-apis/" in path and "/service-apis" in path:
            return FakeR1Response(sme_status, {"serviceId": str(service_id)})
        if path.startswith("/rapps/") and path.endswith("/credentials"):   # call flow 01b: the push to the instance's rApp
            fake_post.pushed.append((path, json))
            return FakeR1Response(fake_post.push_status, {})
        if path.startswith("/sme/invoker-registrations/") and path.endswith("/secret"):   # call flow 01d: a new secret, same id
            return FakeR1Response(200, {"apiInvokerId": path.split("/")[3], "onboardingSecret": "renewed"})
        raise AssertionError(f"unexpected R1 POST to {path}")

    fake_post.pushed, fake_post.push_status = [], 204
    fake_get.stopped = {}
    return fake_get, fake_post


def test_create_instance_registers_package_usage(client, db_session_factory, monkeypatch):
    """The actual fix: CreateInstance now calls Onboarding's usage/start
    and stores the real registrationId — previously nothing called it at
    all, so the cascade-delete guard's active-usage condition could never
    fire from ordinary rApp deployment.
    """
    reg_id = uuid.uuid4()
    fake_get, fake_post = _route_r1_get_post(registration_id=reg_id)
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    resp = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}})
    assert resp.status_code == 202
    instance_id = uuid.UUID(resp.json()["instanceId"])

    with db_session_factory() as session:
        inst = session.get(RAppInstance, instance_id)
        assert inst.package_usage_registration_id == reg_id


def test_create_instance_defaults_autonomy_mode_to_shadow(client, monkeypatch):
    """HISTORY.md OI-6.3: SHADOW — the safest, no-enforcement
    mode — is the default for every existing caller that doesn't declare
    autonomyMode, the same permissive-by-default shape optional fields
    already use throughout this build.
    """
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    resp = client.post("/instances", json={"packageId": str(uuid.uuid4())})
    instance_id = resp.json()["instanceId"]

    fetched = client.get(f"/instances/{instance_id}").json()
    assert fetched["autonomyMode"] == "SHADOW"
    assert fetched["regionScope"] is None


def test_create_instance_stores_autonomous_mode_and_region_scope(client, monkeypatch):
    """An autonomy mode and region scope given at create are stored and shown in the detail and the list."""
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    resp = client.post("/instances", json={
        "packageId": str(uuid.uuid4()), "autonomyMode": "AUTONOMOUS", "regionScope": {"nodeIds": ["ne-1", "ne-2"]},
    })
    instance_id = resp.json()["instanceId"]

    fetched = client.get(f"/instances/{instance_id}").json()
    assert fetched["autonomyMode"] == "AUTONOMOUS"
    assert fetched["regionScope"] == {"nodeIds": ["ne-1", "ne-2"]}

    listed = client.get("/instances").json()["items"]
    assert [i["autonomyMode"] for i in listed] == ["AUTONOMOUS"]


# Each row is a package state and the answer to create: AVAILABLE and PRIMED give 202, every other state gives 409.
@pytest.mark.parametrize("state,status", [("AVAILABLE", 202), ("PRIMED", 202), ("ONBOARDING", 409),
                                          ("DEPRECATED", 409), ("FAILED", 409)])
def test_create_instance_accepts_available_and_primed_packages_only(client, monkeypatch, state, status):
    fake_get, fake_post = _route_r1_get_post(onboarding_status=state)
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    assert client.post("/instances", json={"packageId": str(uuid.uuid4())}).status_code == status


def test_create_instance_rejects_an_invalid_autonomy_mode(client):
    """An autonomy mode that is not AUTONOMOUS, ASSIST or SHADOW is 422."""
    resp = client.post("/instances", json={"packageId": str(uuid.uuid4()), "autonomyMode": "NOT_A_REAL_MODE"})
    assert resp.status_code == 422


def test_terminate_instance_calls_usage_stop(client, monkeypatch):
    """The other half of the fix: TerminateInstance must stop the usage
    registration CreateInstance started, or the guard sees permanently
    active usage even after the instance is long gone.
    """
    reg_id = uuid.uuid4()
    fake_get, fake_post = _route_r1_get_post(registration_id=reg_id)
    calls = []

    def recording_post(self, path, json=None, **kw):
        calls.append(path)
        return fake_post(self, path, json=json, **kw)

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", recording_post)

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")  # DEPLOYING -> RUNNING; TERMINATE needs RUNNING
    calls.clear()

    resp = client.post(f"/instances/{created['instanceId']}/terminate")
    assert resp.status_code == 200
    assert any(f"/usage/{reg_id}/stop" in c for c in calls)


def test_create_instance_is_refused_when_usage_start_fails(client, db_session_factory, monkeypatch):
    """Call flow 06: the usage registration is opened first and the instance is not created without it. Onboarding answering 503 makes CreateInstance a
    502 UPSTREAM_FAILED, and nothing else was made for the instance (no SME invoker, no NFO deployment, no row)."""
    calls = []

    def fake_get(self, path, **kw):
        return FakeR1Response(200, {"state": "AVAILABLE", "nfDeploymentDescriptorId": str(uuid.uuid4())})

    def fake_post(self, path, json=None, **kw):
        calls.append(path)
        if "/usage/start" in path:
            return FakeR1Response(503, {})  # onboarding unreachable
        raise AssertionError(f"unexpected R1 POST to {path}")

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    resp = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}})

    assert resp.status_code == 502 and resp.json()["detail"]["title"] == "UPSTREAM_FAILED"
    assert len(calls) == 1
    with db_session_factory() as session:
        assert session.query(RAppInstance).count() == 0


def test_create_instance_passes_on_onboardings_package_not_deployable(client, monkeypatch):
    """A package deprecated between rApp Management's read and usage/start: Onboarding's 409 is passed on as 409 PACKAGE_NOT_DEPLOYABLE."""
    monkeypatch.setattr("app.main.R1Client.get",
                        lambda self, path, **kw: FakeR1Response(200, {"state": "AVAILABLE", "nfDeploymentDescriptorId": str(uuid.uuid4())}))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(409, {"detail": {"title": "PACKAGE_NOT_DEPLOYABLE"}}))

    resp = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}})

    assert resp.status_code == 409 and resp.json()["detail"]["title"] == "PACKAGE_NOT_DEPLOYABLE"


def test_create_instance_stops_the_usage_registration_when_a_later_step_fails(client, monkeypatch):
    """When a step after usage/start raises (here SME cannot be reached), the registration just opened is stopped again, so it does not hold the package."""
    import httpx

    registration = str(uuid.uuid4())
    calls = []

    def fake_post(self, path, json=None, **kw):
        calls.append(path)
        if path.endswith("/usage/start"):
            return FakeR1Response(200, {"registrationId": registration})
        if path.endswith("/stop"):
            return FakeR1Response(200, {"status": "stopped"})
        raise httpx.ConnectError("sme down")

    monkeypatch.setattr("app.main.R1Client.get",
                        lambda self, path, **kw: FakeR1Response(200, {"state": "AVAILABLE", "nfDeploymentDescriptorId": str(uuid.uuid4())}))
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    package_id = uuid.uuid4()
    resp = client.post("/instances", json={"packageId": str(package_id), "config": {}})

    assert resp.status_code == 503
    assert calls[-1] == f"/onboarding/packages/{package_id}/usage/{registration}/stop"
def test_terminate_instance_deregisters_dme_producer(client, monkeypatch):
    """rApp-as-producer reconsideration trigger (HISTORY.md §1):
    TERMINATE must reach DME too, deregistering every DMEType this
    instance's own oauth_client_id (== its DME producerId) registered —
    not just revoke the local credential. Also reaches SME (HISTORY.md §7's
    Onboarding/rApp Mgmt finding 3): an unconditional, idempotent
    provider-registrations delete attempt using the same identity as its
    SME apfId — the package here declared no real SME data (fake_get's
    onboarding-status has no smeDeclarations), so no service-api deletes
    happen, only the always-attempted provider deregistration, mirroring
    the DME call's own unconditional-attempt shape.
    """
    fake_get, fake_post = _route_r1_get_post()
    calls = []

    def recording_delete(self, path, params=None, **kw):
        calls.append((path, params))
        return FakeR1Response(204, {})

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    monkeypatch.setattr("app.statemachine.R1Client.delete", recording_delete)

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")

    resp = client.post(f"/instances/{created['instanceId']}/terminate")
    assert resp.status_code == 200
    workload_ref = client.get(f"/instances/{created['instanceId']}").json()["workloadRef"]
    assert len(calls) == 6
    assert calls[0] == ("/dme/production-capabilities", {"producer_id": created["oauthClientId"]})
    assert calls[1] == (f"/sme/provider-registrations/{created['oauthClientId']}", None)
    assert calls[2] == ("/dme/data-jobs", {"created_by": created["oauthClientId"]})       # OI-7-rapp-data-jobs-teardown: the jobs it opened as a consumer
    assert calls[3] == (f"/sme/invoker-registrations/{created['oauthClientId']}", None)   # PR-SEC-14: the instance's invoker goes with its credential
    assert calls[4] == (f"/ran-nf-oam/rapp-kill/{created['oauthClientId']}", None)       # call flow 07e: no stop left for a revoked identity
    assert calls[5] == (f"/nfo/deployments/{workload_ref}", None)  # OI-2-terminate-workload, after DME/SME


def test_crash_via_critical_fault_deregisters_dme_producer(client, monkeypatch):
    """Also reaches SME (HISTORY.md §7's Onboarding/rApp Mgmt finding 3) —
    see test_terminate_instance_deregisters_dme_producer's own docstring
    for why the second call is an unconditional provider-registrations
    delete with no matching service-api deletes.
    """
    fake_get, fake_post = _route_r1_get_post()
    calls = []
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    monkeypatch.setattr("app.statemachine.R1Client.delete", lambda self, path, params=None, **kw: calls.append((path, params)))

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")

    resp = client.post(f"/instances/{created['instanceId']}/fault", params={"severity": "critical"})
    assert resp.status_code == 200
    assert resp.json()["instanceState"] == "FAULTED"
    assert calls == [
        ("/dme/production-capabilities", {"producer_id": created["oauthClientId"]}),
        (f"/sme/provider-registrations/{created['oauthClientId']}", None),
    ]


def test_terminate_instance_survives_unreachable_dme(client, monkeypatch):
    """Best-effort — a DME outage must never block TERMINATE itself, same
    "unreachable callback never fails the primary operation" precedent
    Policy Mgmt's CreateIntent dispatch uses.
    """
    import httpx as httpx_module

    fake_get, fake_post = _route_r1_get_post()

    def raise_error(self, path, params=None, **kw):
        raise httpx_module.ConnectError("dme unreachable")

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    monkeypatch.setattr("app.statemachine.R1Client.delete", raise_error)

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")

    resp = client.post(f"/instances/{created['instanceId']}/terminate")
    assert resp.status_code == 200


def test_bootstrap_complete_registers_package_sme_declarations(client, db_session_factory, monkeypatch):
    """HISTORY.md §7's Onboarding/rApp Mgmt finding 3, closed: a package
    whose CSAR declared real Files/Sme/providers + Files/Sme/serviceapis
    content gets that content registered with SME at bootstrap-complete,
    using this instance's own oauth_client_id as its apfId — the real
    O-RAN SC rApp Manager's own per-instance SME deploy timing
    (SmeDeployer.deployRappInstance), not at onboarding or CreateInstance.

    Uses the real external CAPIF shape (apiProvDomInfo/apiName+aefProfiles,
    grounded against nonrtric-plt-rappmanager's own real sample packages)
    — see the sibling test below for this build's own direct-shape CSARs
    (this build's own established convention).
    """
    sme_declarations = {
        "providers": [{"apiProvDomInfo": "Provider domain"}],
        "serviceApis": [{"apiName": "Energy Saving API Set 1", "aefProfiles": [{"aefId": "aef-1"}]}],
    }
    service_id = uuid.uuid4()
    calls = []

    fake_get, real_fake_post = _route_r1_get_post(sme_declarations=sme_declarations, sme_service_id=service_id)

    def fake_post(self, path, json=None, **kw):
        calls.append((path, json))
        return real_fake_post(self, path, json=json, **kw)

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    calls.clear()  # only care about bootstrap-complete's own SME calls, not CreateInstance's NFO/usage ones

    resp = client.post(f"/instances/{created['instanceId']}/bootstrap-complete")
    assert resp.status_code == 200

    apf_id = created["oauthClientId"]
    assert calls == [
        ("/sme/provider-registrations", {"apfId": apf_id, "providerDomainInfo": "Provider domain"}),
        (f"/sme/published-apis/v1/{apf_id}/service-apis", {
            # Suffixed with apf_id — SME's own serviceName is globally
            # unique across producers, so a CSAR's fixed name would
            # otherwise collide across separate instances/registrations.
            "serviceName": f"Energy Saving API Set 1-{apf_id}", "producerId": apf_id,
            "endpoint": "http://unknown", "version": "1.0", "moduleScope": "rapp",
            "aefProfiles": [{"aefId": "aef-1"}],
        }),
    ]

    with db_session_factory() as session:
        inst = session.get(RAppInstance, uuid.UUID(created["instanceId"]))
        assert inst.sme_service_ids == [str(service_id)]

    detail = client.get(f"/instances/{created['instanceId']}").json()
    assert detail["smeServiceIds"] == [str(service_id)]


def test_bootstrap_complete_passes_through_this_builds_own_sme_declaration_shape(client, monkeypatch):
    """A CSAR may bundle Files/Sme/ content in this build's own request-body shape directly
    (apfId/providerDomainInfo; serviceName/endpoint/version/moduleScope/
    aefProfiles/...) rather than the real external CAPIF shape — caught
    by reading that real sample file, not assumed. Both fields must pass
    through mostly as-is (serviceName's presence is the shape signal),
    with only apfId/producerId overridden to this instance's own real
    identity, never the CSAR's own hardcoded apfId (so two instances of
    the same package never collide on one shared apfId).
    """
    sme_declarations = {
        "providers": [{"apfId": "energy-saving-rapp", "providerDomainInfo": "Energy Saving rApp — demo provider domain"}],
        "serviceApis": [{
            "serviceName": "energy-saving-api", "producerId": "energy-saving-rapp",
            "endpoint": "http://energy-saving-rapp:8080/energy-saving/v1", "version": "v1",
            "fullApiVersions": ["v1"], "serviceCapabilities": {"resource": "energy-saving", "operations": ["GET"]},
            "selectionCriteria": {}, "moduleScope": "energy-saving-rapp", "allowedConsumers": ["energy-saving-rapp"],
            "aefProfiles": [{"aefId": "energy-saving-rapp-aef", "interfaceDescription": {"ipv4Addr": "energy-saving-rapp", "port": 8080}}],
        }],
    }
    service_id = uuid.uuid4()
    calls = []
    fake_get, real_fake_post = _route_r1_get_post(sme_declarations=sme_declarations, sme_service_id=service_id)

    def fake_post(self, path, json=None, **kw):
        calls.append((path, json))
        return real_fake_post(self, path, json=json, **kw)

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    calls.clear()

    resp = client.post(f"/instances/{created['instanceId']}/bootstrap-complete")
    assert resp.status_code == 200

    apf_id = created["oauthClientId"]
    assert calls[0] == ("/sme/provider-registrations", {"apfId": apf_id, "providerDomainInfo": "Energy Saving rApp — demo provider domain"})
    service_call_path, service_call_body = calls[1]
    assert service_call_path == f"/sme/published-apis/v1/{apf_id}/service-apis"
    assert service_call_body["producerId"] == apf_id  # overridden — never the CSAR's own hardcoded "energy-saving-rapp"
    assert service_call_body["serviceName"] == f"energy-saving-api-{apf_id}"  # suffixed — see this build's own global serviceName-uniqueness rule
    assert service_call_body["endpoint"] == "http://energy-saving-rapp:8080/energy-saving/v1"
    assert service_call_body["aefProfiles"] == sme_declarations["serviceApis"][0]["aefProfiles"]


def test_terminate_instance_deregisters_sme_service_apis_too(client, monkeypatch):
    """The registered-service-ids half of finding 3: TERMINATE deregisters
    each real serviceId bootstrap-complete received back, not just the
    provider domain.
    """
    sme_declarations = {"providers": [{"apiProvDomInfo": "Provider domain"}], "serviceApis": [{"apiName": "Energy Saving API"}]}
    service_id = uuid.uuid4()
    fake_get, fake_post = _route_r1_get_post(sme_declarations=sme_declarations, sme_service_id=service_id)
    calls = []
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    monkeypatch.setattr("app.statemachine.R1Client.delete", lambda self, path, params=None, **kw: (calls.append(path), FakeR1Response(204, {}))[1])

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")

    resp = client.post(f"/instances/{created['instanceId']}/terminate")
    assert resp.status_code == 200
    # DME reconsideration runs first (statemachine.py's _reconsider_registrations
    # ordering), then the SME service-api delete(s), then the provider delete,
    # then NFO Terminate for the workload (OI-2-terminate-workload).
    workload_ref = client.get(f"/instances/{created['instanceId']}").json()["workloadRef"]
    assert calls == [
        "/dme/production-capabilities",
        f"/sme/published-apis/v1/{created['oauthClientId']}/service-apis/{service_id}",
        f"/sme/provider-registrations/{created['oauthClientId']}",
        "/dme/data-jobs",                                               # OI-7-rapp-data-jobs-teardown
        f"/sme/invoker-registrations/{created['oauthClientId']}",      # PR-SEC-14
        f"/ran-nf-oam/rapp-kill/{created['oauthClientId']}",           # call flow 07e
        f"/nfo/deployments/{workload_ref}",
    ]


def test_recover_route_fires_recover_transition(client, db_session_factory):
    """RECOVER — previously unreachable via any route at all (the FSM
    transition existed, nothing called it).
    """
    inst_id = uuid.uuid4()
    with db_session_factory() as session:
        session.add(RAppInstance(instance_id=inst_id, package_id=uuid.uuid4(), state=InstanceState.FAULTED, oauth_client_id=None))
        session.commit()

    resp = client.post(f"/instances/{inst_id}/recover")
    assert resp.status_code == 200
    assert resp.json()["state"] == "DEPLOYING"



def test_an_instance_transition_is_published_on_the_event_bus(client, db_session_factory, monkeypatch):
    """MSG-3.5: the RECOVER transition is published as `org.oran.smo.rapp-instance.state-changed` about the instance, from the state machine's one hook."""
    from smo_shared import events
    monkeypatch.setenv("SMO_EVENT_BUS", "memory")
    monkeypatch.setattr(events.MEMORY, "events", [])
    inst_id = _make_instance(db_session_factory, InstanceState.FAULTED)
    assert client.post(f"/instances/{inst_id}/recover").status_code == 200
    [event] = [e for e in events.MEMORY.events if e["subject"] == str(inst_id)]
    assert event["type"] == "org.oran.smo.rapp-instance.state-changed"
    assert (event["data"]["from"], event["data"]["event"], event["data"]["to"]) == ("FAULTED", "RECOVER", "DEPLOYING")

def test_terminate_lands_in_undeployed_and_keeps_the_row(client, db_session_factory, monkeypatch):
    """HISTORY.md §5: TERMINATE used to delete the instance row
    outright, in the same call as the workload teardown. The reference's
    own split (RappService.undeployRappInstance/deleteRappInstance) keeps
    the row around, in a terminal UNDEPLOYED state, until a separate
    delete is called.
    """
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")

    resp = client.post(f"/instances/{created['instanceId']}/terminate")
    assert resp.status_code == 200
    assert (resp.json()["instanceId"], resp.json()["state"]) == (created["instanceId"], "UNDEPLOYED")

    with db_session_factory() as session:
        inst = session.get(RAppInstance, uuid.UUID(created["instanceId"]))
        assert inst is not None
        assert inst.state == "UNDEPLOYED"


def test_delete_instance_requires_undeployed_state(client, monkeypatch):
    """The reference's own DeleteRappInstance guard: "Unable to delete rApp
    instance %s as it is not in UNDEPLOYED state" — a running instance
    can't be deleted out from under itself.
    """
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")  # -> RUNNING, not UNDEPLOYED

    resp = client.delete(f"/instances/{created['instanceId']}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "RAPP_INSTANCE_NOT_UNDEPLOYED"


def test_delete_instance_removes_the_row_once_undeployed(client, db_session_factory, monkeypatch):
    """Deleting an UNDEPLOYED instance answers 204 and removes the row."""
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")
    client.post(f"/instances/{created['instanceId']}/terminate")

    resp = client.delete(f"/instances/{created['instanceId']}")
    assert resp.status_code == 204

    with db_session_factory() as session:
        assert session.get(RAppInstance, uuid.UUID(created["instanceId"])) is None


def test_delete_instance_cascades_fault_and_performance_reports(client, db_session_factory, monkeypatch):
    """Same FK-cascade bug class already found and fixed for DME's
    deregister_producer/AI-ML Workflow's deregister_model: neither
    dependent table had an ON DELETE CASCADE, so deleting an instance with
    fault/performance history would orphan those rows (SQLite) or crash
    with an unhandled IntegrityError (real Postgres).
    """
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    instance_id = created["instanceId"]
    client.post(f"/instances/{instance_id}/bootstrap-complete")
    client.post(f"/instances/{instance_id}/performance", json={"cpu": 0.5})
    client.post(f"/instances/{instance_id}/fault", params={"severity": "minor"})
    client.post(f"/instances/{instance_id}/terminate")

    resp = client.delete(f"/instances/{instance_id}")
    assert resp.status_code == 204

    with db_session_factory() as session:
        assert session.query(RAppFaultReport).filter(RAppFaultReport.instance_id == uuid.UUID(instance_id)).count() == 0
        assert session.query(RAppPerformanceReport).filter(RAppPerformanceReport.instance_id == uuid.UUID(instance_id)).count() == 0


def test_delete_unknown_instance_is_404(client):
    """Deleting an unknown instance is 404."""
    resp = client.delete(f"/instances/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_instance_returns_real_workload_ref_and_configuration(client, monkeypatch):
    """HISTORY.md §5: no single-instance detail read existed at
    all. Exposes what this build genuinely computes — the real NFO
    workloadRef and the caller-supplied configuration — not the
    reference's own nested ACM/SME/DME resource records, which stay out
    of scope since CreateInstance never accepts that descriptor.
    """
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {"replicas": 3}}).json()

    resp = client.get(f"/instances/{created['instanceId']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["instanceId"] == created["instanceId"]
    assert body["state"] == "DEPLOYING"
    assert body["configuration"] == {"replicas": 3}
    assert body["workloadRef"]  # the fake NFO deployment id from _route_r1_get_post
    assert body["pendingUpgradeInstanceId"] is None


def test_get_unknown_instance_is_404(client):
    """Reading an unknown instance is 404."""
    resp = client.get(f"/instances/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """GUI pass: the BFF's /modules/status probes /<module>/health on every module."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def _make_instance(db_session_factory, state=InstanceState.RUNNING) -> uuid.UUID:
    """Inserts an instance row directly in `state` (RUNNING by default) with no workload or credential, and returns its id."""
    instance_id = uuid.uuid4()
    with db_session_factory() as session:
        session.add(RAppInstance(instance_id=instance_id, package_id=uuid.uuid4(), state=state))
        session.commit()
    return instance_id


def test_list_performance_reports_returns_newest_first(client, db_session_factory):
    """GUI pass: POST /instances/{id}/performance was write-only."""
    instance_id = _make_instance(db_session_factory)
    client.post(f"/instances/{instance_id}/performance", json={"throughputMbps": 10})
    client.post(f"/instances/{instance_id}/performance", json={"throughputMbps": 20})

    resp = client.get(f"/instances/{instance_id}/performance")
    assert resp.status_code == 200
    reports = resp.json()["items"]
    assert [r["metrics"]["throughputMbps"] for r in reports] == [20, 10]
    assert all(r["reportedAt"] for r in reports)

    assert len(client.get(f"/instances/{instance_id}/performance", params={"limit": 1}).json()["items"]) == 1


def test_list_fault_reports_returns_recorded_faults(client, db_session_factory):
    """A fault recorded with its severity and description is listed."""
    instance_id = _make_instance(db_session_factory)
    client.post(f"/instances/{instance_id}/fault", params={"severity": "minor", "description": "slow"})

    resp = client.get(f"/instances/{instance_id}/faults")
    assert resp.status_code == 200
    assert [(f["severity"], f["description"]) for f in resp.json()["items"]] == [("minor", "slow")]


def test_list_reports_404_on_an_unknown_instance(client):
    """Both report lists are 404 for an unknown instance."""
    assert client.get(f"/instances/{uuid.uuid4()}/performance").status_code == 404
    assert client.get(f"/instances/{uuid.uuid4()}/faults").status_code == 404


def test_create_instance_records_workload_ref_from_nfos_202(client, db_session_factory, monkeypatch):
    """NFO's Instantiate answers 202 Accepted; the old 200-only check left
    workloadRef empty on every real deployment."""
    fake_get, fake_post = _route_r1_get_post()
    nf_deployment_id = str(uuid.uuid4())

    def post_202_from_nfo(self, path, json=None, **kw):
        if "/nfo/deployments" in path:
            return FakeR1Response(202, {"nfDeploymentId": nf_deployment_id, "state": "RUNNING"})
        return fake_post(self, path, json=json, **kw)

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", post_202_from_nfo)
    instance_id = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()["instanceId"]
    assert client.get(f"/instances/{instance_id}").json()["workloadRef"] == nf_deployment_id


# ---------------------------------------------------------------- OI-2-terminate-workload

def _running_instance(client, monkeypatch, **create_body):
    """Creates an instance with R1 stubbed (`create_body` adds request fields), completes its bootstrap so it is RUNNING, and returns the create answer.
    """
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}, **create_body}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")
    return created


def test_terminate_calls_nfo_terminate_for_the_workload_and_records_it(client, monkeypatch, fake_r1_delete):
    """OI-2-terminate-workload: TERMINATE hands workloadRef (the
    nfDeploymentId NFO Instantiate returned) back to NFO's
    DELETE /nfo/deployments/{id}, and records the outcome."""
    created = _running_instance(client, monkeypatch)
    workload_ref = client.get(f"/instances/{created['instanceId']}").json()["workloadRef"]
    assert workload_ref

    resp = client.post(f"/instances/{created['instanceId']}/terminate")

    assert resp.status_code == 200
    assert (f"/nfo/deployments/{workload_ref}", None) in fake_r1_delete
    teardown = client.get(f"/instances/{created['instanceId']}").json()["lastTeardown"]
    assert (teardown["reason"], teardown["nfoTerminate"], teardown["usageStop"]) == ("TERMINATE", "DONE", "DONE")
    assert resp.json()["lastTeardown"] == teardown


def test_terminate_records_an_nfo_failure_but_still_undeploys(client, monkeypatch):
    """If NFO refuses the terminate, the instance still reaches UNDEPLOYED and the failure is recorded in `lastTeardown`."""
    created = _running_instance(client, monkeypatch)
    monkeypatch.setattr("app.main.R1Client.delete", lambda self, path, params=None, **kw: FakeR1Response(503 if path.startswith("/nfo/") else 204, {}))

    resp = client.post(f"/instances/{created['instanceId']}/terminate")

    assert resp.status_code == 200 and resp.json()["state"] == "UNDEPLOYED"
    assert resp.json()["lastTeardown"]["nfoTerminate"] == "FAILED: HTTP 503"


# ---------------------------------------------------------------- OI-2-lcm-error-mapping

@pytest.mark.parametrize("state", [InstanceState.FAULTED, InstanceState.DEPLOYING])
def test_terminate_from_faulted_or_deploying_retires_the_instance(client, db_session_factory, fake_r1_delete, state):
    """A crashed instance no longer has to recover before it can be retired;
    neither does one whose container never bootstrapped."""
    instance_id = uuid.uuid4()
    with db_session_factory() as session:
        session.add(RAppInstance(instance_id=instance_id, package_id=uuid.uuid4(), state=state,
                                 oauth_client_id="cred", workload_ref="nf-1"))
        session.commit()

    resp = client.post(f"/instances/{instance_id}/terminate")

    assert resp.status_code == 200 and resp.json()["state"] == "UNDEPLOYED"
    assert ("/nfo/deployments/nf-1", None) in fake_r1_delete
    assert client.delete(f"/instances/{instance_id}").status_code == 204


# Each row is a route, a state it is illegal in, and the event the route fires; the answer is 409 LIFECYCLE_ILLEGAL_TRANSITION naming both the state and the event.
@pytest.mark.parametrize("route,state,event", [
    ("terminate", InstanceState.UNDEPLOYED, "TERMINATE"),
    ("terminate", InstanceState.UPGRADING, "TERMINATE"),
    ("recover", InstanceState.RUNNING, "RECOVER"),
    ("bootstrap-complete", InstanceState.RUNNING, "BOOTSTRAP_OK"),
])
def test_illegal_transitions_are_409_naming_state_and_event(client, db_session_factory, route, state, event):
    instance_id = _make_instance(db_session_factory, state=state)
    resp = client.post(f"/instances/{instance_id}/{route}")
    assert resp.status_code == 409
    problem = resp.json()["detail"]
    assert problem["title"] == "LIFECYCLE_ILLEGAL_TRANSITION"
    assert f"event {event}" in problem["detail"] and f"state {state}" in problem["detail"]


def test_critical_fault_on_a_non_running_instance_is_409(client, db_session_factory):
    """A critical fault on an instance that is not RUNNING is refused with 409 and is not recorded."""
    instance_id = _make_instance(db_session_factory, state=InstanceState.FAULTED)
    resp = client.post(f"/instances/{instance_id}/fault", params={"severity": "critical"})
    assert resp.status_code == 409
    assert "event CRASH" in resp.json()["detail"]["detail"] and "state FAULTED" in resp.json()["detail"]["detail"]
    assert client.get(f"/instances/{instance_id}/faults").json()["items"] == []  # refused, not recorded


def test_upgrade_of_a_non_running_instance_is_409(client, db_session_factory, monkeypatch):
    """Upgrading an instance that is not RUNNING (here FAULTED) is 409 naming the START_UPGRADE event."""
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    instance_id = _make_instance(db_session_factory, state=InstanceState.FAULTED)
    resp = client.post(f"/instances/{instance_id}/upgrade", json={"newPackageId": str(uuid.uuid4())})
    assert resp.status_code == 409
    assert "event START_UPGRADE" in resp.json()["detail"]["detail"]


# Each row is a lifecycle or configuration route called with an id that does not exist; every one answers 404 RAPP_INSTANCE_NOT_FOUND.
@pytest.mark.parametrize("method,path,kwargs", [
    ("post", "/instances/{id}/bootstrap-complete", {}),
    ("post", "/instances/{id}/recover", {}),
    ("post", "/instances/{id}/terminate", {}),
    ("post", "/instances/{id}/upgrade", {"json": {"newPackageId": str(uuid.uuid4())}}),
    ("post", "/instances/{id}/upgrade/resolve", {"params": {"succeeded": True}}),
    ("post", "/instances/{id}/fault", {"params": {"severity": "critical"}}),
    ("post", "/instances/{id}/performance", {"json": {"cpu": 1}}),
    ("get", "/instances/{id}/config", {}),
    ("put", "/instances/{id}/config", {"json": {}}),
    ("delete", "/instances/{id}", {}),
])
def test_unknown_instance_is_404_on_every_lifecycle_route(client, method, path, kwargs):
    resp = getattr(client, method)(path.format(id=uuid.uuid4()), **kwargs)
    assert resp.status_code == 404
    assert resp.json()["detail"]["title"] == "RAPP_INSTANCE_NOT_FOUND"


def test_resolve_with_no_pending_upgrade_is_404(client, db_session_factory):
    """Resolving an instance that has no pending upgrade is 404."""
    instance_id = _make_instance(db_session_factory)
    resp = client.post(f"/instances/{instance_id}/upgrade/resolve", params={"succeeded": True})
    assert resp.status_code == 404
    assert "no pending upgrade" in resp.json()["detail"]["detail"]


def test_create_instance_from_an_unknown_package_is_404(client, monkeypatch):
    """Creating from a package Onboarding does not know is 404 PACKAGE_NOT_FOUND."""
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeR1Response(404, {}))
    resp = client.post("/instances", json={"packageId": str(uuid.uuid4())})
    assert resp.status_code == 404
    assert resp.json()["detail"]["title"] == "PACKAGE_NOT_FOUND"


# ---------------------------------------------------------------- OI-2-upgrade-completeness / OI-1-upgrade-identity

def test_upgrade_route_provisions_a_complete_replacement(client, monkeypatch):
    """The upgrade route creates a replacement through the same path as create (package check, new identity, NFO deployment, usage registration), in DEPLOYING with the old instance's configuration, autonomy mode and region scope, and puts the old one in UPGRADING.
    """
    calls = []
    fake_get, fake_post = _route_r1_get_post()

    def recording_post(self, path, json=None, **kw):
        calls.append(path)
        return fake_post(self, path, json=json, **kw)

    created = _running_instance(client, monkeypatch, config={"replicas": 2}, autonomyMode="AUTONOMOUS",
                                regionScope={"nodeIds": ["ne-1"]})
    monkeypatch.setattr("app.main.R1Client.post", recording_post)
    new_package = str(uuid.uuid4())

    resp = client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": new_package})

    assert resp.status_code == 200
    body = resp.json()
    assert body["oldInstanceState"] == "UPGRADING"
    assert body["oauthClientId"] and body["oauthClientId"] != created["oauthClientId"]
    new = client.get(f"/instances/{body['newInstanceId']}").json()
    assert (new["packageId"], new["state"]) == (new_package, "DEPLOYING")
    assert new["configuration"] == {"replicas": 2}
    assert (new["autonomyMode"], new["regionScope"]) == ("AUTONOMOUS", {"nodeIds": ["ne-1"]})
    assert new["workloadRef"]
    assert "/nfo/deployments" in calls and f"/onboarding/packages/{new_package}/usage/start" in calls


def test_upgrade_route_refuses_a_package_that_is_not_deployable(client, monkeypatch):
    """Upgrading to a package that is not AVAILABLE or PRIMED is 409 PACKAGE_NOT_DEPLOYABLE, the old instance stays RUNNING and no replacement row is left behind.
    """
    created = _running_instance(client, monkeypatch)
    monkeypatch.setattr("app.main.R1Client.get", _route_r1_get_post(onboarding_status="DEPRECATED")[0])

    resp = client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())})

    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "PACKAGE_NOT_DEPLOYABLE"
    assert client.get(f"/instances/{created['instanceId']}").json()["state"] == "RUNNING"
    assert client.get("/instances").json()["total"] == 1  # no replacement row left behind


def test_upgrade_commit_via_route_releases_the_old_instance(client, monkeypatch, fake_r1_delete):
    """resolve(succeeded=true) runs the old row's TERMINATE side effects —
    DME/SME deregistration, NFO Terminate, usage/stop — before deleting it,
    so the old package's deprime/delete guards are released."""
    reg_id = uuid.uuid4()
    fake_get, fake_post = _route_r1_get_post(registration_id=reg_id)
    posts = []

    def recording_post(self, path, json=None, **kw):
        posts.append(path)
        return fake_post(self, path, json=json, **kw)

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", recording_post)
    package_id = str(uuid.uuid4())
    created = client.post("/instances", json={"packageId": package_id}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")
    old_workload = client.get(f"/instances/{created['instanceId']}").json()["workloadRef"]
    upgrade = client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())}).json()
    posts.clear()
    fake_r1_delete.clear()

    resp = client.post(f"/instances/{created['instanceId']}/upgrade/resolve", params={"succeeded": True})

    assert resp.status_code == 200
    assert resp.json()["instanceId"] == upgrade["newInstanceId"] and resp.json()["state"] == "RUNNING"
    assert [p for p, _ in fake_r1_delete] == [
        "/dme/production-capabilities", f"/sme/provider-registrations/{created['oauthClientId']}",
        "/dme/data-jobs",                                               # OI-7-rapp-data-jobs-teardown: the retired instance's consumer jobs
        f"/sme/invoker-registrations/{created['oauthClientId']}",
        f"/ran-nf-oam/rapp-kill/{created['oauthClientId']}",           # call flow 07e: no stop left for the retired identity
        f"/nfo/deployments/{old_workload}",
    ]
    assert f"/onboarding/packages/{package_id}/usage/{reg_id}/stop" in posts
    assert client.get(f"/instances/{created['instanceId']}").status_code == 404
    survivor = client.get(f"/instances/{upgrade['newInstanceId']}").json()
    assert survivor["lastTeardown"]["instanceId"] == created["instanceId"]
    assert survivor["lastTeardown"]["reason"] == "UPGRADE_COMMIT"


def test_upgrade_rollback_via_route_tears_the_replacement_down(client, monkeypatch, fake_r1_delete):
    """Resolving an upgrade as failed returns the old instance to RUNNING, terminates the replacement's NFO workload and deletes the replacement row.
    """
    created = _running_instance(client, monkeypatch)
    upgrade = client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())}).json()
    new_workload = client.get(f"/instances/{upgrade['newInstanceId']}").json()["workloadRef"]

    resp = client.post(f"/instances/{created['instanceId']}/upgrade/resolve", params={"succeeded": False})

    assert resp.status_code == 200
    assert resp.json()["instanceId"] == created["instanceId"] and resp.json()["state"] == "RUNNING"
    assert (f"/nfo/deployments/{new_workload}", None) in fake_r1_delete
    assert client.get(f"/instances/{upgrade['newInstanceId']}").status_code == 404


def test_terminating_a_pending_upgrade_replacement_is_409(client, monkeypatch):
    """The replacement row of an upgrade in flight cannot be terminated (409): the upgrade must be resolved instead."""
    created = _running_instance(client, monkeypatch)
    upgrade = client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())}).json()
    resp = client.post(f"/instances/{upgrade['newInstanceId']}/terminate")
    assert resp.status_code == 409
    assert "resolve that upgrade" in resp.json()["detail"]["detail"]


def test_the_replacement_reporting_itself_up_commits_the_upgrade(client, monkeypatch, fake_r1_delete):
    """Call flow 07b: bootstrap-complete for the pending replacement commits the upgrade; the old instance is retired and gone."""
    created = _running_instance(client, monkeypatch)
    upgrade = client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())}).json()

    resp = client.post(f"/instances/{upgrade['newInstanceId']}/bootstrap-complete")

    assert resp.status_code == 200
    assert resp.json() == {"instanceId": upgrade["newInstanceId"], "state": "RUNNING", "upgradeCommitted": True}
    assert client.get(f"/instances/{created['instanceId']}").status_code == 404
    assert client.get(f"/instances/{upgrade['newInstanceId']}/versions").json()["versions"][0]["previousInstanceId"] == created["instanceId"]


def test_the_replacement_gets_its_credentials_pushed(client, monkeypatch):
    """Call flow 07b: the upgrade pushes the replacement's credentials to its rApp (here at the old instance's address), so it can report itself up."""
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "operatorApiBase": "http://rapp.example:8000"}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")
    fake_post.pushed.clear()

    upgrade = client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())}).json()

    assert upgrade["credentialsDelivered"] is True
    [(path, body)] = fake_post.pushed
    assert path == f"/rapps/{upgrade['newInstanceId']}/operator/instances/{upgrade['newInstanceId']}/credentials"
    assert body["oauthClientId"] == upgrade["oauthClientId"]


def test_the_replacement_failing_to_start_rolls_the_upgrade_back(client, monkeypatch, fake_r1_delete):
    """Call flow 07b: a critical fault from the pending replacement rolls the upgrade back; the report is kept on the old instance, which runs on."""
    created = _running_instance(client, monkeypatch)
    upgrade = client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())}).json()

    resp = client.post(f"/instances/{upgrade['newInstanceId']}/fault", params={"severity": "critical", "description": "no cells"})

    assert resp.json() == {"status": "recorded", "instanceState": None, "upgradeRolledBack": True}
    assert client.get(f"/instances/{upgrade['newInstanceId']}").status_code == 404
    assert client.get(f"/instances/{created['instanceId']}").json()["state"] == "RUNNING"
    [fault] = client.get(f"/instances/{created['instanceId']}/faults").json()["items"]
    assert upgrade["newInstanceId"] in fault["description"] and "no cells" in fault["description"]


def test_a_critical_fault_before_bootstrap_faults_the_instance(client, db_session_factory):
    """Call flow 07a: an instance that cannot start reports a critical fault while DEPLOYING; it goes FAULTED (BOOTSTRAP_FAILED), from where it is recovered or terminated."""
    instance_id = _make_instance(db_session_factory, state=InstanceState.DEPLOYING)
    resp = client.post(f"/instances/{instance_id}/fault", params={"severity": "critical"})
    assert resp.json() == {"status": "recorded", "instanceState": "FAULTED"}
    assert len(client.get(f"/instances/{instance_id}/faults").json()["items"]) == 1


def _expire_upgrade(db_session_factory, instance_id):
    with db_session_factory() as session:
        session.get(RAppInstance, uuid.UUID(instance_id)).upgrade_timeout_seconds = 0
        session.commit()


def test_overdue_upgrade_rolls_back_when_the_instance_is_read(client, db_session_factory, monkeypatch):
    """upgradeTimeoutSeconds is enforced lazily: reading either row after
    the deadline rolls the unresolved upgrade back."""
    created = _running_instance(client, monkeypatch)
    upgrade = client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())}).json()
    _expire_upgrade(db_session_factory, created["instanceId"])

    old = client.get(f"/instances/{created['instanceId']}").json()
    assert old["state"] == "RUNNING" and old["pendingUpgradeInstanceId"] is None
    assert old["lastTeardown"]["reason"] == "UPGRADE_TIMEOUT"
    assert client.get(f"/instances/{upgrade['newInstanceId']}").status_code == 404


def test_overdue_upgrade_rolls_back_when_the_list_is_read(client, db_session_factory, monkeypatch):
    """Listing instances after an upgrade's deadline rolls the unresolved upgrade back, so only the old instance remains, RUNNING.
    """
    created = _running_instance(client, monkeypatch)
    client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())})
    _expire_upgrade(db_session_factory, created["instanceId"])

    items = client.get("/instances").json()["items"]
    assert [(i["instanceId"], i["state"]) for i in items] == [(created["instanceId"], "RUNNING")]


def test_resolving_an_overdue_upgrade_as_succeeded_is_409_timed_out(client, db_session_factory, monkeypatch):
    """Resolving as succeeded after the deadline is 409 RAPP_UPGRADE_TIMED_OUT, and the rollback it triggered is committed."""
    created = _running_instance(client, monkeypatch)
    client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())})
    _expire_upgrade(db_session_factory, created["instanceId"])

    resp = client.post(f"/instances/{created['instanceId']}/upgrade/resolve", params={"succeeded": True})

    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "RAPP_UPGRADE_TIMED_OUT"
    assert client.get(f"/instances/{created['instanceId']}").json()["state"] == "RUNNING"  # the rollback was committed


def test_losing_the_lazy_upgrade_sweep_to_another_replica_is_not_an_error_on_a_read(client, db_session_factory, monkeypatch):
    """PR-ST-2: a read that sweeps an overdue upgrade races other replicas doing the same.
    Losing the race means the sweep is done; the read still answers, and is not a 409."""
    created = _running_instance(client, monkeypatch)
    client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())})
    _expire_upgrade(db_session_factory, created["instanceId"])

    with concurrent_commit_on("rapp_instance") as fired:
        listed = client.get("/instances")
    assert fired and listed.status_code == 200

    with concurrent_commit_on("rapp_instance") as fired:
        single = client.get(f"/instances/{created['instanceId']}")
    assert fired and single.status_code == 200

    # with no competing writer the sweep still happens
    assert client.get(f"/instances/{created['instanceId']}").json()["state"] == "RUNNING"


def test_losing_the_lazy_sweep_while_resolving_an_overdue_upgrade_is_still_409_timed_out(client, db_session_factory, monkeypatch):
    """If another replica rolls the overdue upgrade back at the same moment, resolving as succeeded still answers 409 RAPP_UPGRADE_TIMED_OUT.
    """
    created = _running_instance(client, monkeypatch)
    client.post(f"/instances/{created['instanceId']}/upgrade", json={"newPackageId": str(uuid.uuid4())})
    _expire_upgrade(db_session_factory, created["instanceId"])

    with concurrent_commit_on("rapp_instance") as fired:
        resp = client.post(f"/instances/{created['instanceId']}/upgrade/resolve", params={"succeeded": True})
    assert fired and resp.status_code == 409
    assert resp.json()["detail"]["title"] == "RAPP_UPGRADE_TIMED_OUT"


# ---------------------------------------------------------------- OI-1-sa-rollback: version history and RollbackInstance

def _commit_upgrade(client, instance_id, new_package):
    """Upgrade `instance_id` to `new_package` and commit it; returns the replacement's id."""
    new_id = client.post(f"/instances/{instance_id}/upgrade", json={"newPackageId": new_package}).json()["newInstanceId"]
    assert client.post(f"/instances/{new_id}/bootstrap-complete").json()["upgradeCommitted"] is True   # call flow 07b: the replacement up commits the upgrade
    return new_id


def _commit_rollback(client, instance_id):
    """Starts a rollback of `instance_id` and bootstraps the replacement, which commits it; returns the rollback answer.
    """
    started = client.post(f"/instances/{instance_id}/rollback")
    assert started.status_code == 200, started.json()
    body = started.json()
    assert client.post(f"/instances/{body['newInstanceId']}/bootstrap-complete").json()["upgradeCommitted"] is True
    return body


def test_upgrade_commit_records_a_version_with_what_the_old_instance_ran(client, monkeypatch):
    """Committing an upgrade records a version holding the retired instance's package and configuration, and the history names it as the rollback target.
    """
    v1 = str(uuid.uuid4())
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    created = client.post("/instances", json={"packageId": v1, "config": {"threshold": 1}}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")
    v2 = str(uuid.uuid4())

    new_id = _commit_upgrade(client, created["instanceId"], v2)

    history = client.get(f"/instances/{new_id}/versions").json()
    assert history["instanceId"] == new_id and history["packageId"] == v2
    [version] = history["versions"]
    assert (version["kind"], version["previousInstanceId"], version["previousPackageId"]) == ("UPGRADE", created["instanceId"], v1)
    assert version["previousConfiguration"] == {"threshold": 1}
    assert history["rollbackTarget"]["versionId"] == version["versionId"]


def test_rollback_restores_the_previous_package_and_configuration(client, monkeypatch):
    """A rollback re-provisions the previous version's package, configuration, autonomy mode and region scope, not the configuration the current version was retuned to.
    """
    created = _running_instance(client, monkeypatch, config={"threshold": 1}, autonomyMode="AUTONOMOUS",
                                regionScope={"nodeIds": ["ne-1"]})
    v1 = client.get(f"/instances/{created['instanceId']}").json()["packageId"]
    v2_id = _commit_upgrade(client, created["instanceId"], str(uuid.uuid4()))
    client.put(f"/instances/{v2_id}/config", json={"threshold": 9})  # retuned after the upgrade

    started = client.post(f"/instances/{v2_id}/rollback")

    assert started.status_code == 200
    body = started.json()
    assert (body["instanceId"], body["oldInstanceState"], body["toPackageId"]) == (v2_id, "UPGRADING", v1)
    replacement = client.get(f"/instances/{body['newInstanceId']}").json()
    assert replacement["packageId"] == v1 and replacement["configuration"] == {"threshold": 1}
    assert (replacement["autonomyMode"], replacement["regionScope"]) == ("AUTONOMOUS", {"nodeIds": ["ne-1"]})


def test_rollback_resolves_a_superseded_instance_id_to_the_current_one(client, monkeypatch):
    """An SA SMOS monitor registered before the upgrade holds the old id."""
    created = _running_instance(client, monkeypatch)
    v2_id = _commit_upgrade(client, created["instanceId"], str(uuid.uuid4()))
    assert client.get(f"/instances/{created['instanceId']}").status_code == 404

    started = client.post(f"/instances/{created['instanceId']}/rollback")

    assert started.status_code == 200 and started.json()["instanceId"] == v2_id
    assert client.get(f"/instances/{created['instanceId']}/versions").json()["instanceId"] == v2_id


def test_repeated_rollbacks_walk_back_instead_of_flip_flopping(client, monkeypatch):
    """Each rollback goes one version further back, an upgrade that was rolled back is skipped, and when none is left the answer is 409 ROLLBACK_HISTORY_UNAVAILABLE.
    """
    created = _running_instance(client, monkeypatch)
    v1 = client.get(f"/instances/{created['instanceId']}").json()["packageId"]
    v2, v3 = str(uuid.uuid4()), str(uuid.uuid4())
    v2_id = _commit_upgrade(client, created["instanceId"], v2)
    v3_id = _commit_upgrade(client, v2_id, v3)

    first = _commit_rollback(client, v3_id)
    assert first["toPackageId"] == v2
    second = _commit_rollback(client, first["newInstanceId"])
    assert second["toPackageId"] == v1

    third = client.post(f"/instances/{second['newInstanceId']}/rollback")
    assert third.status_code == 409 and third.json()["detail"]["title"] == "ROLLBACK_HISTORY_UNAVAILABLE"
    history = client.get(f"/instances/{created['instanceId']}/versions").json()  # the original id still resolves
    assert history["packageId"] == v1 and history["rollbackTarget"] is None
    assert [v["kind"] for v in history["versions"]] == ["ROLLBACK", "ROLLBACK", "UPGRADE", "UPGRADE"]
    assert all(v["rolledBackByVersionId"] for v in history["versions"] if v["kind"] == "UPGRADE")


def test_an_upgrade_after_a_rollback_can_itself_be_rolled_back(client, monkeypatch):
    """An upgrade made after a rollback is a version like any other and rolls back to the package before it."""
    created = _running_instance(client, monkeypatch)
    v1 = client.get(f"/instances/{created['instanceId']}").json()["packageId"]
    v2_id = _commit_upgrade(client, created["instanceId"], str(uuid.uuid4()))
    back = _commit_rollback(client, v2_id)
    v3 = str(uuid.uuid4())
    v3_id = _commit_upgrade(client, back["newInstanceId"], v3)

    assert client.post(f"/instances/{v3_id}/rollback").json()["toPackageId"] == v1


def test_rollback_with_no_history_is_409(client, monkeypatch):
    """An instance that was never upgraded cannot be rolled back (409 ROLLBACK_HISTORY_UNAVAILABLE) and stays RUNNING."""
    created = _running_instance(client, monkeypatch)
    resp = client.post(f"/instances/{created['instanceId']}/rollback")
    assert resp.status_code == 409 and resp.json()["detail"]["title"] == "ROLLBACK_HISTORY_UNAVAILABLE"
    assert client.get(f"/instances/{created['instanceId']}").json()["state"] == "RUNNING"


def test_rollback_of_an_unknown_instance_is_404(client):
    """Rolling back an unknown instance is 404."""
    resp = client.post(f"/instances/{uuid.uuid4()}/rollback")
    assert resp.status_code == 404 and resp.json()["detail"]["title"] == "RAPP_INSTANCE_NOT_FOUND"


def test_failed_rollback_leaves_the_current_version_running_and_rollable(client, monkeypatch, fake_r1_delete):
    """When a rollback's replacement fails, the current version stays RUNNING, the replacement is gone and the version it could roll back to is still available.
    """
    created = _running_instance(client, monkeypatch)
    v2 = str(uuid.uuid4())
    v2_id = _commit_upgrade(client, created["instanceId"], v2)
    started = client.post(f"/instances/{v2_id}/rollback").json()

    resp = client.post(f"/instances/{v2_id}/upgrade/resolve", params={"succeeded": False})

    assert resp.json()["instanceId"] == v2_id and resp.json()["state"] == "RUNNING"
    assert client.get(f"/instances/{started['newInstanceId']}").status_code == 404
    history = client.get(f"/instances/{v2_id}/versions").json()
    assert history["packageId"] == v2 and history["rollbackTarget"] is not None
    assert [v["kind"] for v in history["versions"]] == ["UPGRADE"]


def test_rollback_of_an_instance_that_is_not_running_is_409(client, monkeypatch):
    """Only a RUNNING instance can be rolled back; a FAULTED one is 409 LIFECYCLE_ILLEGAL_TRANSITION."""
    created = _running_instance(client, monkeypatch)
    v2_id = _commit_upgrade(client, created["instanceId"], str(uuid.uuid4()))
    client.post(f"/instances/{v2_id}/fault", params={"severity": "critical"})

    resp = client.post(f"/instances/{v2_id}/rollback")

    assert resp.status_code == 409 and resp.json()["detail"]["title"] == "LIFECYCLE_ILLEGAL_TRANSITION"


def test_rollback_to_a_package_no_longer_deployable_is_refused(client, monkeypatch):
    """If the earlier package is no longer deployable the rollback is 409 PACKAGE_NOT_DEPLOYABLE and the current instance stays RUNNING.
    """
    created = _running_instance(client, monkeypatch)
    v2_id = _commit_upgrade(client, created["instanceId"], str(uuid.uuid4()))
    monkeypatch.setattr("app.main.R1Client.get", _route_r1_get_post(onboarding_status="DELETING")[0])

    resp = client.post(f"/instances/{v2_id}/rollback")

    assert resp.status_code == 409 and resp.json()["detail"]["title"] == "PACKAGE_NOT_DEPLOYABLE"
    assert client.get(f"/instances/{v2_id}").json()["state"] == "RUNNING"


def test_a_concurrent_writer_turns_a_transition_into_a_409_and_the_repeat_succeeds(client, db_session_factory):
    """PR-ST-2: RAppInstance is versioned. Another replica committing between this request's
    load and its write makes the write 409 CONCURRENT_MODIFICATION, never a silent overwrite."""
    inst_id = uuid.uuid4()
    with db_session_factory() as session:
        session.add(RAppInstance(instance_id=inst_id, package_id=uuid.uuid4(), state=InstanceState.FAULTED, oauth_client_id=None))
        session.commit()

    with concurrent_commit_on("rapp_instance") as fired:
        stale = client.post(f"/instances/{inst_id}/recover")
    assert fired and stale.status_code == 409
    assert stale.json()["detail"]["title"] == "CONCURRENT_MODIFICATION"

    repeat = client.post(f"/instances/{inst_id}/recover")
    assert repeat.status_code == 200 and repeat.json()["state"] == "DEPLOYING"


def test_create_instance_with_an_idempotency_key_creates_one_instance(client, db_session_factory, monkeypatch):
    """PR-ST-3: a repeat of CreateInstance with the same Idempotency-Key is answered from the first
    answer, not run again; the same key with another payload is refused."""
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    body = {"packageId": str(uuid.uuid4()), "config": {}}

    first = client.post("/instances", json=body, headers={"Idempotency-Key": "create-1"})
    again = client.post("/instances", json=body, headers={"Idempotency-Key": "create-1"})
    assert first.status_code == again.status_code == 202
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    with db_session_factory() as session:
        assert session.query(RAppInstance).count() == 1

    other = client.post("/instances", json={**body, "packageId": str(uuid.uuid4())}, headers={"Idempotency-Key": "create-1"})
    assert other.status_code == 422 and other.json()["detail"]["title"] == "IDEMPOTENCY_KEY_REUSED"


def _rows(db_session_factory, model) -> int:
    """The number of `model` rows in the test database."""
    with db_session_factory() as session:
        return session.query(model).count()


def test_a_repeated_performance_report_with_the_same_key_is_stored_once(client, db_session_factory):
    """SDK-RETRY-IDEMPOTENCY: a Go or Java SDK retry of POST /instances/{id}/performance gets the first answer and stores no second report; the same
    key with other metrics, or on another instance, is 422 IDEMPOTENCY_KEY_REUSED and stores nothing."""
    instance_a, instance_b = _make_instance(db_session_factory), _make_instance(db_session_factory)
    first = client.post(f"/instances/{instance_a}/performance", json={"cpu": 0.5}, headers={"Idempotency-Key": "perf-1"})
    again = client.post(f"/instances/{instance_a}/performance", json={"cpu": 0.5}, headers={"Idempotency-Key": "perf-1"})
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    for path, body in ((f"/instances/{instance_a}/performance", {"cpu": 0.9}), (f"/instances/{instance_b}/performance", {"cpu": 0.5})):
        reused = client.post(path, json=body, headers={"Idempotency-Key": "perf-1"})
        assert reused.status_code == 422 and reused.json()["detail"]["title"] == "IDEMPOTENCY_KEY_REUSED"
    assert _rows(db_session_factory, RAppPerformanceReport) == 1


def test_a_repeated_fault_with_the_same_key_is_recorded_once_and_a_critical_one_is_not_refused(client, db_session_factory):
    """SDK-RETRY-IDEMPOTENCY: a retry of POST /instances/{id}/fault gets the first answer. A minor fault is stored once (it was stored twice); a critical
    one is answered with the first answer (FAULTED) instead of the state machine's 409 for an instance that is FAULTED already. The same key with
    another severity is 422."""
    instance_id = _make_instance(db_session_factory)
    for severity in ("minor", "critical"):
        params = {"severity": severity, "description": "slow"}
        first = client.post(f"/instances/{instance_id}/fault", params=params, headers={"Idempotency-Key": f"fault-{severity}"})
        again = client.post(f"/instances/{instance_id}/fault", params=params, headers={"Idempotency-Key": f"fault-{severity}"})
        assert first.status_code == again.status_code == 200, again.text
        assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    assert first.json()["instanceState"] == "FAULTED"
    reused = client.post(f"/instances/{instance_id}/fault", params={"severity": "major"}, headers={"Idempotency-Key": "fault-minor"})
    assert reused.status_code == 422 and reused.json()["detail"]["title"] == "IDEMPOTENCY_KEY_REUSED"
    assert _rows(db_session_factory, RAppFaultReport) == 2


# ---- call flow 01d (DOC-CF-1.1 review): RAPP_MGMT_REQUIRE_SME_REGISTRATION makes the SME declarations a condition of the bootstrap

_DECLARATIONS = {"providers": [{"apiProvDomInfo": "Provider domain"}], "serviceApis": [{"apiName": "API", "aefProfiles": [{"aefId": "aef-1"}]}]}


def _created_with_declarations(client, monkeypatch, **route):
    """An instance of a package that declares one SME provider and one service API, with R1 stubbed by `_route_r1_get_post(**route)`."""
    fake_get, fake_post = _route_r1_get_post(sme_declarations=_DECLARATIONS, **route)
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    return client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()


def test_a_refused_sme_registration_is_ignored_by_default(client, monkeypatch):
    """Best-effort by default: SME refusing the service API leaves the bootstrap accepted, the instance RUNNING."""
    created = _created_with_declarations(client, monkeypatch, sme_status=409)
    resp = client.post(f"/instances/{created['instanceId']}/bootstrap-complete")
    assert resp.status_code == 200 and resp.json()["state"] == "RUNNING"


def test_with_registration_required_a_refused_sme_registration_refuses_the_bootstrap(client, monkeypatch):
    """With RAPP_MGMT_REQUIRE_SME_REGISTRATION on, SME refusing a service API is 502 UPSTREAM_FAILED and the instance stays DEPLOYING."""
    monkeypatch.setenv("RAPP_MGMT_REQUIRE_SME_REGISTRATION", "true")
    created = _created_with_declarations(client, monkeypatch, sme_status=409)

    resp = client.post(f"/instances/{created['instanceId']}/bootstrap-complete")

    assert resp.status_code == 502 and resp.json()["detail"]["title"] == "UPSTREAM_FAILED"
    assert client.get(f"/instances/{created['instanceId']}").json()["state"] == "DEPLOYING"


def test_with_registration_required_an_unreachable_sme_refuses_the_bootstrap(client, monkeypatch):
    """With RAPP_MGMT_REQUIRE_SME_REGISTRATION on, SME not answering is 503 ENDPOINT_UNREACHABLE and the instance stays DEPLOYING; registered, it runs."""
    import httpx

    monkeypatch.setenv("RAPP_MGMT_REQUIRE_SME_REGISTRATION", "on")
    created = _created_with_declarations(client, monkeypatch)
    real_post = __import__("app.main", fromlist=["R1Client"]).R1Client.post

    def sme_down(self, path, json=None, **kw):
        if path == "/sme/provider-registrations":
            raise httpx.ConnectError("sme down")
        return real_post(self, path, json=json, **kw)

    monkeypatch.setattr("app.main.R1Client.post", sme_down)
    resp = client.post(f"/instances/{created['instanceId']}/bootstrap-complete")
    assert resp.status_code == 503 and resp.json()["detail"]["title"] == "ENDPOINT_UNREACHABLE"
    assert client.get(f"/instances/{created['instanceId']}").json()["state"] == "DEPLOYING"

    monkeypatch.setattr("app.main.R1Client.post", real_post)
    assert client.post(f"/instances/{created['instanceId']}/bootstrap-complete").json()["state"] == "RUNNING"


def test_terminate_closes_the_instances_dme_data_jobs_and_a_crash_does_not(client, monkeypatch):
    """OI-7-rapp-data-jobs-teardown: TERMINATE deletes the DME data jobs the instance created (by its invoker id, before the id is revoked); a crash keeps
    them, since a crashed instance keeps its identity for a recover; an unreachable DME does not stop the terminate."""
    fake_get, fake_post = _route_r1_get_post()
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    calls = []

    def delete(self, path, params=None, **kw):
        calls.append((path, params))
        if path == "/dme/data-jobs":
            raise httpx.ConnectError("DME down")
        return FakeR1Response(204, {})

    monkeypatch.setattr("app.statemachine.R1Client.delete", delete)
    crashed = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    client.post(f"/instances/{crashed['instanceId']}/bootstrap-complete")
    assert client.post(f"/instances/{crashed['instanceId']}/fault", params={"severity": "critical"}).status_code == 200
    assert ("/dme/data-jobs", {"created_by": crashed["oauthClientId"]}) not in calls

    created = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}}).json()
    client.post(f"/instances/{created['instanceId']}/bootstrap-complete")
    calls.clear()
    resp = client.post(f"/instances/{created['instanceId']}/terminate")
    assert resp.status_code == 200 and resp.json()["state"] == "UNDEPLOYED"
    assert ("/dme/data-jobs", {"created_by": created["oauthClientId"]}) in calls


# ---------------------------------------------------------------- call flow 15: the instance's workload at NFO

# Each row is an NFO answer to Instantiate that leaves no workload: a refusal (502 UPSTREAM_FAILED) or no answer (503 ENDPOINT_UNREACHABLE).
class _NotJson(FakeR1Response):
    """An answer whose body is not JSON (a proxy's HTML error page)."""
    def json(self):
        raise ValueError("not JSON")


@pytest.mark.parametrize("outcome,status,title", [(FakeR1Response(409, {"detail": {"title": "NFDEPLOYMENT_NAME_CONFLICT"}}), 502, "UPSTREAM_FAILED"),
                                                  (_NotJson(500, None), 502, "UPSTREAM_FAILED"),
                                                  (None, 503, "ENDPOINT_UNREACHABLE")])
def test_create_fails_without_a_workload_and_leaves_nothing_behind(client, db_session_factory, monkeypatch, fake_r1_delete, outcome, status, title):
    """Call flow 15b: an instance is not created without its NFO deployment. The usage registration is stopped and the identity made at SME removed."""
    import httpx
    fake_get, fake_post = _route_r1_get_post()
    posts = []

    def post(self, path, json=None, **kw):
        posts.append(path)
        if path == "/nfo/deployments":
            if outcome is None:
                raise httpx.ConnectError("down")
            return outcome
        return fake_post(self, path, json=json, **kw)
    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", post)

    resp = client.post("/instances", json={"packageId": str(uuid.uuid4())})

    assert resp.status_code == status and resp.json()["detail"]["title"] == title
    assert any(p.endswith("/stop") for p in posts)
    assert any(p.startswith("/sme/invoker-registrations/api-invoker-") for p, _ in fake_r1_delete)
    with db_session_factory() as session:
        assert session.query(RAppInstance).count() == 0


# Each row is what NFO says about the workload and what the instance detail shows: its state and reason, GONE for a 404, UNKNOWN for no answer.
@pytest.mark.parametrize("answer,expected", [
    (FakeR1Response(200, {"state": "ABNORMAL", "abnormalReason": "RUNTIME_FAILURE: oom"}), {"state": "ABNORMAL", "abnormalReason": "RUNTIME_FAILURE: oom"}),
    (FakeR1Response(404, {}), {"state": "GONE", "abnormalReason": None}),
    (FakeR1Response(200, {}), {"state": "UNKNOWN", "abnormalReason": None}),
    (None, {"state": "UNKNOWN", "abnormalReason": None}),
])
def test_the_instance_detail_shows_its_workload_as_nfo_sees_it(client, monkeypatch, answer, expected):
    """Call flow 15e: a broken workload is visible on the instance (its own state is unchanged)."""
    import httpx
    created = _running_instance(client, monkeypatch)
    original = __import__("app.main", fromlist=["R1Client"]).R1Client.get

    def get(self, path, **kw):
        if path.startswith("/nfo/deployments/"):
            if answer is None:
                raise httpx.ConnectError("down")
            return answer
        return original(self, path, **kw)
    monkeypatch.setattr("app.main.R1Client.get", get)

    body = client.get(f"/instances/{created['instanceId']}").json()
    assert body["workload"] == expected and body["state"] == "RUNNING"


def test_an_instance_without_a_workload_shows_none(client, db_session_factory):
    """An instance with no workloadRef (made before DOC-CF-1.4) shows no workload and NFO is not asked."""
    instance_id = _make_instance(db_session_factory)
    assert client.get(f"/instances/{instance_id}").json()["workload"] is None


def test_create_instance_is_503_when_onboarding_does_not_answer(client, db_session_factory, monkeypatch):
    """DOC-CF-2.2: Onboarding not answering the package read is 503 ENDPOINT_UNREACHABLE (it was a 500), and no instance is made."""
    def down(self, path, **kw):
        raise httpx.ConnectError("onboarding down")
    monkeypatch.setattr("app.main.R1Client.get", down)
    resp = client.post("/instances", json={"packageId": str(uuid.uuid4()), "config": {}})
    assert resp.status_code == 503 and resp.json()["detail"]["title"] == "ENDPOINT_UNREACHABLE"
    with db_session_factory() as session:
        assert session.query(RAppInstance).count() == 0
