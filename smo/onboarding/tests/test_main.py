"""The Onboarding routes end to end: onboarding a package, the lifecycle routes, usage registrations, validation of the manifest, and the NFO descriptor call.

Covers `app/main.py` (with `app/package_validation.py` and `app/statemachine.py` behind it) through FastAPI's TestClient. Fixtures: `db_session_factory` (an in-memory
SQLite with the three Onboarding tables and a stub `nf_deployment_descriptor` table) and `client` (the app with `get_session` overridden to that database). The package
fetch (`app.main.httpx.get`) and the NFO call (`app.main.R1Client.post`) are replaced with fakes; the CSAR bytes are real zips built by `_real_package_bytes`. The
signature policy has its own file (`test_signing.py`).

Run: `cd smo/onboarding && PYTHONPATH=.:../shared python -m pytest tests/test_main.py -q`. No Postgres or network is needed.
"""

import uuid
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import Column, Table, Uuid, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from smo_shared.db import Base, get_session
from smo_shared.testing import concurrent_commit_on

from app.main import app
from app.models import ApplicationPackage, Artifact, PackageUsageRegistration


class FakeR1Response:
    """Stands in for the response of an R1 call: a status code and a JSON payload."""
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def db_session_factory():
    """A session factory over a fresh in-memory SQLite (one shared connection) holding the three Onboarding tables.

    The NFO table `nf_deployment_descriptor` lives in another module, so a one-column stub is registered to let `ApplicationPackage`'s foreign key resolve.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    # nf_deployment_descriptor lives in the nfo module — stand in a minimal
    # table so ApplicationPackage's FK resolves, same pattern as nfo/tests'
    # application_package stub.
    if "nf_deployment_descriptor" not in Base.metadata.tables:
        Table("nf_deployment_descriptor", Base.metadata, Column("nf_deployment_descriptor_id", Uuid, primary_key=True))
    Base.metadata.create_all(engine, tables=[ApplicationPackage.__table__, Artifact.__table__, PackageUsageRegistration.__table__])
    return sessionmaker(bind=engine)


@pytest.fixture
def client(db_session_factory):
    """A TestClient of the app whose database sessions come from `db_session_factory`; the override is removed after the test."""
    def override_get_session():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_onboard_success_creates_nf_deployment_descriptor_via_nfo(client, monkeypatch):
    """A validated package gets the descriptor id NFO returns, stored on the package, and ends AVAILABLE (rApp Management must not have to pass packageId where NFO expects a descriptor).
    """
    monkeypatch.setattr("app.main._validate_package", lambda location: ("Definitions/main.yaml", [], "deadbeef", {}))
    descriptor_id = uuid.uuid4()
    monkeypatch.setattr(
        "app.main.R1Client.post",
        lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(descriptor_id)}),
    )

    resp = client.post("/packages", json={"location": "http://example/pkg.csar"})
    package_id = resp.json()["packageId"]

    status = client.get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "AVAILABLE"
    assert status.json()["nfDeploymentDescriptorId"] == str(descriptor_id)


def test_onboard_routes_to_failed_when_nfo_descriptor_creation_fails(client, monkeypatch):
    """DescriptorCreationFailed folds into the same VALIDATE_FAILED path as
    a malformed zip or an unreachable location — NFO being unavailable at
    onboarding time is a real, expected failure mode, not a crash.
    """
    monkeypatch.setattr("app.main._validate_package", lambda location: ("Definitions/main.yaml", [], "deadbeef", {}))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(503, {}))

    resp = client.post("/packages", json={"location": "http://example/pkg.csar"})
    package_id = resp.json()["packageId"]

    status = client.get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "FAILED"
    assert status.json()["nfDeploymentDescriptorId"] is None


def test_onboard_routes_to_failed_on_a_real_malformed_zip(client, monkeypatch):
    """Real validation code, with only the network fetch faked, sends bytes that are not a zip to FAILED instead of failing the request."""
    class FakeHttpResponse:
        content = b"not a real zip file"
        def raise_for_status(self):
            pass

    monkeypatch.setattr("app.main.httpx.get", lambda location, timeout=None: FakeHttpResponse())

    resp = client.post("/packages", json={"location": "http://example/pkg.csar"})
    package_id = resp.json()["packageId"]

    status = client.get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "FAILED"


def _real_package_bytes(include_acm_composition=True, definitions="tosca_definitions_version: tosca_simple_yaml_1_3\n",
                         manifest_yaml=None, capabilities_yaml=None, sme_provider_json=None, sme_service_api_json=None) -> bytes:
    """Builds a small but well-formed CSAR: TOSCA.meta pointing at `Definitions/main.yaml`, with optional ACM composition file, `manifest.yaml`, `capabilities.yaml`
    and SME provider and service-API files.

    The ACM file is at its real path in the reference (`Files/Acm/definition/compositions.json`, `FileExistenceValidator`). Every optional part is off by default, so a
    call without arguments is a package with none of the AI-platform or SME declarations.
    """
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("TOSCA-Metadata/TOSCA.meta", "Entry-Definitions: Definitions/main.yaml\n")
        z.writestr("Definitions/main.yaml", definitions)
        if include_acm_composition:
            z.writestr("Files/Acm/definition/compositions.json", "{}")
        if manifest_yaml is not None:
            z.writestr("manifest.yaml", manifest_yaml)
        if capabilities_yaml is not None:
            z.writestr("capabilities.yaml", capabilities_yaml)
        if sme_provider_json is not None:
            z.writestr("Files/Sme/providers/provider-function-1.json", sme_provider_json)
        if sme_service_api_json is not None:
            z.writestr("Files/Sme/serviceapis/api-set-1.json", sme_service_api_json)
    return buf.getvalue()


def _mock_fetch(monkeypatch, content: bytes) -> None:
    """Makes `httpx.get` in `app.main` return `content` as the fetched package."""
    class FakeHttpResponse:
        def raise_for_status(self):
            pass
    FakeHttpResponse.content = content
    monkeypatch.setattr("app.main.httpx.get", lambda location, timeout=None: FakeHttpResponse())


def test_onboard_routes_to_failed_when_location_does_not_end_with_csar(client, monkeypatch):
    """A location not named `*.csar` fails the package before anything is fetched (the reference's NamingValidator), so no network fake is needed."""
    resp = client.post("/packages", json={"location": "http://example/pkg.zip"})
    package_id = resp.json()["packageId"]

    status = client.get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "FAILED"


def test_onboard_succeeds_without_the_onap_acm_composition_file(client, monkeypatch):
    """The reference's own FileExistenceValidator requires
    Files/Acm/definition/compositions.json (an ONAP ACM composition
    file) alongside TOSCA-Metadata/TOSCA.meta. Deliberately NOT adopted
    here (formal-spec audit, Onboarding/rApp Mgmt vs. the real ASD/TOSCA
    CSAR format — see HISTORY.md §7): this build never calls ONAP ACM at
    all, so requiring every CSAR to bundle an ONAP-specific file just to
    pass validation isn't real spec fidelity, it's an unwanted
    dependency. A package that omits the file onboards the same as one
    that includes it.
    """
    _mock_fetch(monkeypatch, _real_package_bytes(include_acm_composition=False))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))

    resp = client.post("/packages", json={"location": "http://example/pkg.csar"})
    package_id = resp.json()["packageId"]

    status = client.get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "AVAILABLE"


def test_onboard_succeeds_with_a_real_well_formed_package(client, monkeypatch):
    """The positive case for the two checks above — a genuinely
    well-formed package (real zip bytes, not the usual fully-mocked
    _validate_package) still reaches AVAILABLE.
    """
    _mock_fetch(monkeypatch, _real_package_bytes())
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))

    resp = client.post("/packages", json={"location": "http://example/pkg.csar"})
    package_id = resp.json()["packageId"]

    status = client.get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "AVAILABLE"


def test_onboard_resolves_name_version_vendor_from_the_asd(client, monkeypatch):
    """A validated package takes its identity from the ASD's own
    application_name / application_version / provider properties instead
    of keeping the `unresolved-until-validated 0.0.0` placeholder.
    """
    asd = (
        "tosca_definitions_version: tosca_simple_yaml_1_3\n"
        "topology_template:\n  node_templates:\n    applicationServiceDescriptor:\n      properties:\n"
        '        provider: "ai-ran-ref"\n        application_name: energy-saving-rapp\n        application_version: "1.1"  # bumped\n'
    )
    _mock_fetch(monkeypatch, _real_package_bytes(definitions=asd))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))

    package_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]

    pkg = next(p for p in client.get("/packages").json()["items"] if p["packageId"] == package_id)
    assert (pkg["state"], pkg["name"], pkg["version"], pkg["vendor"]) == ("AVAILABLE", "energy-saving-rapp", "1.1", "ai-ran-ref")


def test_onboard_captures_the_real_asd_descriptor_identity_fields(client, monkeypatch):
    """descriptor_id, descriptor_invariant_id, descriptor_version and schema_version of the ASD are stored and returned; identity and uniqueness stay on the content hash.
    """
    asd = (
        "tosca_definitions_version: tosca_simple_yaml_1_2\n"
        "topology_template:\n  node_templates:\n    applicationServiceDescriptor:\n      properties:\n"
        "        descriptor_id: 2cd6a567-2e33-4960-8ef7-1cc519c998c4\n"
        "        descriptor_invariant_id: 3f8a5e1b-68f1-42e5-89d0-47090dd0ef5a\n"
        '        descriptor_version: "1.0"\n        schema_version: "2.0"\n'
        '        provider: "ai-ran-ref"\n        application_name: energy-saving-rapp\n        application_version: "1.0"\n'
    )
    _mock_fetch(monkeypatch, _real_package_bytes(definitions=asd))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))

    package_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]

    pkg = next(p for p in client.get("/packages").json()["items"] if p["packageId"] == package_id)
    assert pkg["descriptorId"] == "2cd6a567-2e33-4960-8ef7-1cc519c998c4"
    assert pkg["descriptorInvariantId"] == "3f8a5e1b-68f1-42e5-89d0-47090dd0ef5a"
    assert pkg["descriptorVersion"] == "1.0"
    assert pkg["schemaVersion"] == "2.0"


def test_onboard_keeps_placeholder_identity_when_the_asd_has_none(client, monkeypatch):
    """A descriptor without the identity properties still onboards and keeps the placeholder name, version 0.0.0 and no vendor."""
    _mock_fetch(monkeypatch, _real_package_bytes())
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))

    package_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]

    pkg = next(p for p in client.get("/packages").json()["items"] if p["packageId"] == package_id)
    assert (pkg["state"], pkg["name"], pkg["version"], pkg["vendor"]) == ("AVAILABLE", "unresolved-until-validated", "0.0.0", None)


def test_onboard_leaves_ai_capabilities_null_when_neither_file_is_present(client, monkeypatch):
    """A package without manifest.yaml and capabilities.yaml has aiCapabilities null, not an empty dict and not a failure."""
    _mock_fetch(monkeypatch, _real_package_bytes())
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))

    package_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]

    pkg = next(p for p in client.get("/packages").json()["items"] if p["packageId"] == package_id)
    assert pkg["state"] == "AVAILABLE"
    assert pkg["aiCapabilities"] is None


def test_onboard_parses_manifest_and_capabilities_yaml_when_present(client, monkeypatch):
    """The positive case: a package declaring both root-level files (like
    samples/energy-saving-rapp/) has its AI Platform
    capability declaration parsed and stored.
    """
    manifest_yaml = "rappManifest:\n  manifestVersion: \"1.0\"\n  aiRuntimeSdkVersion: \"1.0\"\n"
    capabilities_yaml = (
        "capabilities:\n"
        "  provides:\n"
        "    - namespace: data\n"
        "      description: produces energy-saving-metrics\n"
        "  consumes:\n"
        "    - namespace: platform\n"
        "      description: registers as an SME provider\n"
    )
    _mock_fetch(monkeypatch, _real_package_bytes(manifest_yaml=manifest_yaml, capabilities_yaml=capabilities_yaml))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))

    package_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]

    pkg = next(p for p in client.get("/packages").json()["items"] if p["packageId"] == package_id)
    assert pkg["state"] == "AVAILABLE"
    assert pkg["aiCapabilities"] == {
        "manifestVersion": "1.0", "aiRuntimeSdkVersion": "1.0",
        "provides": [{"namespace": "data", "description": "produces energy-saving-metrics"}],
        "consumes": [{"namespace": "platform", "description": "registers as an SME provider"}],
    }


def test_onboard_parses_capabilities_yaml_alone_without_a_manifest(client, monkeypatch):
    """The two files are independently optional — capabilities.yaml
    without manifest.yaml still produces a partial aiCapabilities dict,
    not a validation failure.
    """
    capabilities_yaml = "capabilities:\n  provides:\n    - namespace: models\n      description: registers a model\n"
    _mock_fetch(monkeypatch, _real_package_bytes(capabilities_yaml=capabilities_yaml))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))

    package_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]

    pkg = next(p for p in client.get("/packages").json()["items"] if p["packageId"] == package_id)
    assert pkg["aiCapabilities"] == {
        "consumes": [], "provides": [{"namespace": "models", "description": "registers a model"}],
    }


def test_onboard_routes_to_failed_on_malformed_capabilities_yaml(client, monkeypatch):
    """A malformed manifest.yaml/capabilities.yaml is a package validation
    failure like any other malformed package file (ONBOARD_VALIDATION_
    FAILURES), not an unhandled 500 — the same discipline as the
    malformed-zip and missing-composition-file cases above.
    """
    _mock_fetch(monkeypatch, _real_package_bytes(capabilities_yaml="capabilities: [unterminated"))

    resp = client.post("/packages", json={"location": "http://example/pkg.csar"})
    package_id = resp.json()["packageId"]

    status = client.get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "FAILED"


def test_onboard_leaves_sme_declarations_null_when_neither_directory_is_present(client, monkeypatch):
    """A package without `Files/Sme/` has smeDeclarations null, not an empty dict and not a failure."""
    _mock_fetch(monkeypatch, _real_package_bytes())
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))

    package_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]

    pkg = next(p for p in client.get("/packages").json()["items"] if p["packageId"] == package_id)
    assert pkg["state"] == "AVAILABLE"
    assert pkg["smeDeclarations"] is None


def test_onboard_parses_sme_provider_and_service_api_declarations_when_present(client, monkeypatch):
    """The CAPIF provider and service-API JSON files under `Files/Sme/` are stored raw, for rApp Management's bootstrap-complete to register per instance.
    """
    provider_json = '{"apiProvDomInfo": "Provider domain", "apiProvFuncs": [{"apiProvFuncRole": "APF"}]}'
    service_api_json = '{"apiName": "Energy Saving API Set 1", "aefProfiles": [{"aefId": "aef-1"}]}'
    _mock_fetch(monkeypatch, _real_package_bytes(sme_provider_json=provider_json, sme_service_api_json=service_api_json))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))

    package_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]

    pkg = next(p for p in client.get("/packages").json()["items"] if p["packageId"] == package_id)
    assert pkg["state"] == "AVAILABLE"
    assert pkg["smeDeclarations"] == {
        "providers": [{"apiProvDomInfo": "Provider domain", "apiProvFuncs": [{"apiProvFuncRole": "APF"}]}],
        "serviceApis": [{"apiName": "Energy Saving API Set 1", "aefProfiles": [{"aefId": "aef-1"}]}],
    }


def test_onboard_routes_to_failed_on_malformed_sme_provider_json(client, monkeypatch):
    """A malformed Files/Sme/providers/*.json is a package validation
    failure like any other malformed package file, not an unhandled 500 —
    the same discipline as malformed manifest.yaml/capabilities.yaml.
    """
    _mock_fetch(monkeypatch, _real_package_bytes(sme_provider_json="{not valid json"))

    resp = client.post("/packages", json={"location": "http://example/pkg.csar"})
    package_id = resp.json()["packageId"]

    status = client.get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "FAILED"


def test_onboard_routes_to_failed_for_a_byte_identical_duplicate_package(client, monkeypatch):
    """A second onboarding of byte-identical content (even from another location) is refused as a duplicate and ends FAILED."""
    package_bytes = _real_package_bytes()
    _mock_fetch(monkeypatch, package_bytes)
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))

    first = client.post("/packages", json={"location": "http://example/pkg.csar"})
    first_status = client.get(f"/packages/{first.json()['packageId']}/onboarding-status")
    assert first_status.json()["state"] == "AVAILABLE"

    second = client.post("/packages", json={"location": "http://example/pkg-again.csar"})
    second_status = client.get(f"/packages/{second.json()['packageId']}/onboarding-status")
    assert second_status.json()["state"] == "FAILED"


def test_onboarding_status_for_unknown_package_is_404(client):
    """An unknown package id is 404 PACKAGE_NOT_FOUND."""
    resp = client.get(f"/packages/{uuid.uuid4()}/onboarding-status")
    assert resp.status_code == 404
    assert resp.json()["detail"]["title"] == "PACKAGE_NOT_FOUND"


def _make_available_package(client, monkeypatch, integrity_hash="deadbeef") -> str:
    monkeypatch.setattr("app.main._validate_package", lambda location: ("Definitions/main.yaml", [], integrity_hash, {}))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))
    return client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]


def test_query_packages_lists_and_filters_by_state(client, monkeypatch):
    """The list returns every package and the `state` filter keeps only the matching ones."""
    available_id = _make_available_package(client, monkeypatch)
    monkeypatch.setattr("app.main._validate_package", lambda location: (_ for _ in ()).throw(KeyError("Definitions/missing.yaml")))
    client.post("/packages", json={"location": "http://example/other.csar"})  # routes to FAILED

    all_packages = client.get("/packages").json()["items"]
    assert len(all_packages) == 2

    available_only = client.get("/packages", params={"state": "AVAILABLE"}).json()["items"]
    assert [p["packageId"] for p in available_only] == [available_id]


def test_deprecate_then_cancel_delete_round_trip(client, monkeypatch):
    """DEPRECATE moves AVAILABLE to DEPRECATED and CANCEL_DELETE moves it back."""
    package_id = _make_available_package(client, monkeypatch)

    deprecated = client.post(f"/packages/{package_id}/deprecate")
    assert deprecated.status_code == 200
    assert deprecated.json()["state"] == "DEPRECATED"

    restored = client.post(f"/packages/{package_id}/cancel-delete")
    assert restored.status_code == 200
    assert restored.json()["state"] == "AVAILABLE"


def test_delete_failed_package_skips_cascade_check(client, monkeypatch):
    """A FAILED package never reached AVAILABLE, so DELETE removes its row directly without running the cascade query."""
    monkeypatch.setattr("app.main._validate_package", lambda location: (_ for _ in ()).throw(KeyError("boom")))
    package_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]

    resp = client.delete(f"/packages/{package_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


def test_delete_available_package_with_no_dependents_succeeds(client, monkeypatch):
    """Deleting an AVAILABLE package with no child and no open usage moves it to DELETING."""
    package_id = _make_available_package(client, monkeypatch)
    resp = client.delete(f"/packages/{package_id}")
    assert resp.status_code == 200
    assert resp.json()["state"] == "DELETING"


def test_delete_blocked_by_dependent_child_package(client, db_session_factory, monkeypatch):
    """Onboarding/rApp Mgmt LLD section 4's cascade-delete guard, one half:
    an AVAILABLE/DEPRECATED child package blocks its parent's deletion.
    """
    parent_id = uuid.UUID(_make_available_package(client, monkeypatch, integrity_hash="parent-hash"))
    child_id = uuid.UUID(_make_available_package(client, monkeypatch, integrity_hash="child-hash"))

    with db_session_factory() as session:
        child = session.get(ApplicationPackage, child_id)
        child.parent_package_id = parent_id
        session.commit()

    resp = client.delete(f"/packages/{parent_id}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "SERVICE_NAME_CONFLICT"


def test_delete_blocked_by_active_usage_registration(client, monkeypatch):
    """The cascade-delete guard's other half: a usage registration with no
    stopped_at blocks deletion — the actual condition
    TerminateInstance's usage/stop call (rapp-mgmt) exists to clear.
    """
    package_id = _make_available_package(client, monkeypatch)
    client.post(f"/packages/{package_id}/usage/start", params={"consumer_id": "instance-1"})

    resp = client.delete(f"/packages/{package_id}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "SERVICE_NAME_CONFLICT"


def test_delete_succeeds_once_usage_registration_is_stopped(client, monkeypatch):
    """Stopping the open usage registration lifts the delete guard."""
    package_id = _make_available_package(client, monkeypatch)
    reg = client.post(f"/packages/{package_id}/usage/start", params={"consumer_id": "instance-1"}).json()

    stop_resp = client.post(f"/packages/{package_id}/usage/{reg['registrationId']}/stop")
    assert stop_resp.status_code == 200

    resp = client.delete(f"/packages/{package_id}")
    assert resp.status_code == 200
    assert resp.json()["state"] == "DELETING"


def test_prime_moves_available_package_to_primed(client, monkeypatch):
    """PRIME takes an AVAILABLE package to PRIMED in one request (PRIMING is never visible)."""
    package_id = _make_available_package(client, monkeypatch)

    resp = client.post(f"/packages/{package_id}/prime")
    assert resp.status_code == 200
    assert resp.json()["state"] == "PRIMED"


def test_deprime_moves_primed_package_back_to_available(client, monkeypatch):
    """DEPRIME takes a PRIMED package back to AVAILABLE in one request."""
    package_id = _make_available_package(client, monkeypatch)
    client.post(f"/packages/{package_id}/prime")

    resp = client.post(f"/packages/{package_id}/deprime")
    assert resp.status_code == 200
    assert resp.json()["state"] == "AVAILABLE"


def test_deprime_blocked_by_active_usage_registration(client, monkeypatch):
    """The reference's own deprimeRapp guard: 'Unable to deprime as there
    are active rapp instances.'
    """
    package_id = _make_available_package(client, monkeypatch)
    client.post(f"/packages/{package_id}/prime")
    client.post(f"/packages/{package_id}/usage/start", params={"consumer_id": "instance-1"})

    resp = client.post(f"/packages/{package_id}/deprime")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "SERVICE_NAME_CONFLICT"

    with_package = client.get("/packages", params={"state": "PRIMED"}).json()["items"]
    assert [p["packageId"] for p in with_package] == [package_id]


def test_deprime_succeeds_once_usage_registration_is_stopped(client, monkeypatch):
    """Stopping the open usage registration lifts the deprime guard."""
    package_id = _make_available_package(client, monkeypatch)
    client.post(f"/packages/{package_id}/prime")
    reg = client.post(f"/packages/{package_id}/usage/start", params={"consumer_id": "instance-1"}).json()
    client.post(f"/packages/{package_id}/usage/{reg['registrationId']}/stop")

    resp = client.post(f"/packages/{package_id}/deprime")
    assert resp.status_code == 200
    assert resp.json()["state"] == "AVAILABLE"


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """The BFF's module-status probe, GET /health, answers 200 {"status": "healthy"}."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_query_packages_exposes_identity_fields_for_the_gui(client, db_session_factory):
    """The package list carries name, version, vendor and applicationType, so an operator can tell packages apart."""
    with db_session_factory() as session:
        session.add(ApplicationPackage(package_id=uuid.uuid4(), application_type="rApp", name="energy-saving", version="1.0.0",
                                        vendor="acme", state="AVAILABLE", manifest_ref="m"))
        session.commit()
    [pkg] = client.get("/packages").json()["items"]
    assert (pkg["name"], pkg["version"], pkg["vendor"], pkg["applicationType"]) == ("energy-saving", "1.0.0", "acme", "rApp")
    assert pkg["nfDeploymentDescriptorId"] is None


def test_package_row_is_committed_before_nfo_create_descriptor_is_called(tmp_path, monkeypatch):
    """The package row is committed before NFO is called: NFO is another process with its own connection and a foreign key on the row.

    On PostgreSQL an uncommitted row is invisible to NFO and its insert would fail, ending every onboarding FAILED. A file-backed SQLite with one connection per session
    reproduces that visibility; the shared-connection fixture above cannot.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'onboarding.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[ApplicationPackage.__table__, Artifact.__table__, PackageUsageRegistration.__table__])
    factory = sessionmaker(bind=engine)

    def override_get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    seen_by_nfo = []

    def fake_nfo_create_descriptor(self, path, json=None, **kw):
        with factory() as other_connection:
            seen_by_nfo.append(other_connection.get(ApplicationPackage, uuid.UUID(json["packageId"])) is not None)
        return FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())})

    _mock_fetch(monkeypatch, _real_package_bytes())
    monkeypatch.setattr("app.main.R1Client.post", fake_nfo_create_descriptor)
    app.dependency_overrides[get_session] = override_get_session
    try:
        package_id = TestClient(app).post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]
        state = TestClient(app).get(f"/packages/{package_id}/onboarding-status").json()["state"]
    finally:
        app.dependency_overrides.clear()
    assert seen_by_nfo == [True]
    assert state == "AVAILABLE"


def test_list_usage_registrations_shows_what_blocks_delete(client, db_session_factory):
    """The usage list shows each registration, whether it is still active and its stop time, and the artifact list shows the registered files (call flow 06).
    """
    package_id = uuid.uuid4()
    with db_session_factory() as session:
        session.add(ApplicationPackage(package_id=package_id, application_type="rApp", name="p", version="1",
                                        state="AVAILABLE", manifest_ref="m"))
        session.add(Artifact(package_id=package_id, path="Files/Helm/app.tgz", access_url="http://x/app.tgz"))
        session.commit()
    reg = client.post(f"/packages/{package_id}/usage/start", params={"consumer_id": "instance-1"}).json()

    [usage] = client.get(f"/packages/{package_id}/usage").json()["items"]
    assert (usage["registrationId"], usage["consumerId"], usage["active"]) == (reg["registrationId"], "instance-1", True)
    client.post(f"/packages/{package_id}/usage/{reg['registrationId']}/stop")
    [usage] = client.get(f"/packages/{package_id}/usage").json()["items"]
    assert usage["active"] is False and usage["stoppedAt"]

    assert [a["path"] for a in client.get(f"/packages/{package_id}/artifacts").json()["items"]] == ["Files/Helm/app.tgz"]


# ---------------------------------------------------------------- Wave 7: runtime profiles (W7-03)

def _onboard_with_manifest(client, monkeypatch, manifest_yaml):
    """Onboards a package carrying `manifest_yaml` with NFO faked and returns the package as listed."""
    _mock_fetch(monkeypatch, _real_package_bytes(manifest_yaml=manifest_yaml))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))
    package_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]
    return next(p for p in client.get("/packages").json()["items"] if p["packageId"] == package_id)


def test_onboard_parses_execution_modes_and_runtime_profiles(client, monkeypatch):
    """Execution modes, autonomy modes and runtime profiles at the top level of the manifest (the SMO_Wave_10 layout) are stored in aiCapabilities.
    """
    manifest_yaml = (
        "rappManifest:\n  manifestVersion: \"1.0\"\n"
        "executionModes: [TRAINING, INFERENCE]\n"
        "autonomyModes: [SHADOW, ASSIST, AUTONOMOUS]\n"
        "runtimeProfiles:\n"
        "  TRAINING: {cpu: 8, memory: 16Gi, gpu: 0}\n"
        "  INFERENCE: {cpu: 2, memory: 4Gi, gpu: 0}\n"
    )
    pkg = _onboard_with_manifest(client, monkeypatch, manifest_yaml)
    assert pkg["state"] == "AVAILABLE"
    caps = pkg["aiCapabilities"]
    assert caps["executionModes"] == ["TRAINING", "INFERENCE"]
    assert caps["autonomyModes"] == ["SHADOW", "ASSIST", "AUTONOMOUS"]
    assert caps["runtimeProfiles"] == {"TRAINING": {"cpu": 8, "gpu": 0, "memory": "16Gi"},
                                       "INFERENCE": {"cpu": 2, "gpu": 0, "memory": "4Gi"}}



# Each case is a runtimeProfiles declaration that must fail onboarding: a negative cpu, an unknown mode, a mode not in executionModes, a non-mapping, and a memory that is not
# a Kubernetes quantity (`16 GB`, a bool, a negative value).
@pytest.mark.parametrize("profiles", [
    "runtimeProfiles:\n  TRAINING: {cpu: -1}\n",                                   # negative cpu
    "runtimeProfiles:\n  COMPILING: {cpu: 1}\n",                                   # unknown mode
    "executionModes: [INFERENCE]\nruntimeProfiles:\n  TRAINING: {cpu: 1}\n",       # undeclared mode
    "runtimeProfiles: [1, 2]\n",                                                   # not a mapping
    "runtimeProfiles:\n  TRAINING: {cpu: 1, memory: 16 GB}\n",                     # PR-RAPP-2.1: memory is a Kubernetes quantity
    "runtimeProfiles:\n  TRAINING: {cpu: 1, memory: true}\n",
    "runtimeProfiles:\n  TRAINING: {cpu: 1, memory: -4Gi}\n",
])
def test_onboard_fails_on_an_invalid_runtime_profile(client, monkeypatch, profiles):
    """An invalid runtime profile ends the package FAILED instead of reaching AVAILABLE."""
    pkg = _onboard_with_manifest(client, monkeypatch, profiles)
    assert pkg["state"] == "FAILED"


# ---------------------------------------------------------------- AI-10.1: limits in the manifest

def test_onboard_reads_the_limits_of_the_manifest(client, monkeypatch):
    """`limits.configJobsPerHour` under `rappManifest` is stored in aiCapabilities.limits."""
    pkg = _onboard_with_manifest(client, monkeypatch, "rappManifest:\n  manifestVersion: \"1.0\"\n  limits:\n    configJobsPerHour: 12\n")
    assert pkg["state"] == "AVAILABLE"
    assert pkg["aiCapabilities"]["limits"] == {"configJobsPerHour": 12}


def test_limits_are_accepted_at_the_top_level_and_are_optional(client, monkeypatch):
    """`limits` is also read from the top level of the manifest, like the other AI-runtime keys."""
    assert _onboard_with_manifest(client, monkeypatch, "limits: {configJobsPerHour: 3}\n")["aiCapabilities"]["limits"] == {"configJobsPerHour": 3}


def test_onboard_reads_blast_radius_and_magnitude_limits(client, monkeypatch):
    """maxElementsPerJob (whole) and maxChangePercent (fractions allowed) are stored next to configJobsPerHour."""
    pkg = _onboard_with_manifest(client, monkeypatch, "limits: {configJobsPerHour: 4, maxElementsPerJob: 3, maxChangePercent: 12.5}\n")
    assert pkg["state"] == "AVAILABLE"
    assert pkg["aiCapabilities"]["limits"] == {"configJobsPerHour": 4, "maxElementsPerJob": 3, "maxChangePercent": 12.5}


# Each case is a maxElementsPerJob or maxChangePercent value the platform cannot enforce: a fraction where a whole number is needed, out of range, zero, negative, a bool,
# a string or NaN.
@pytest.mark.parametrize("limits", [
    "limits: {maxElementsPerJob: 2.5}\n",             # elements are whole
    "limits: {maxElementsPerJob: 10001}\n",
    "limits: {maxChangePercent: 0}\n",
    "limits: {maxChangePercent: -1}\n",
    "limits: {maxChangePercent: 10001}\n",
    "limits: {maxChangePercent: true}\n",
    "limits: {maxChangePercent: '20'}\n",
    "limits: {maxChangePercent: .nan}\n",
])
def test_onboard_fails_on_an_invalid_blast_radius_or_magnitude(client, monkeypatch, limits):
    """A blast-radius or magnitude limit that cannot be enforced fails onboarding instead of being ignored."""
    assert _onboard_with_manifest(client, monkeypatch, limits)["state"] == "FAILED"


# Each case is a configJobsPerHour value or limits section that must fail: zero, a fraction, a bool, above the maximum, a string, an unknown limit name, or a non-mapping.
@pytest.mark.parametrize("limits", [
    "limits: {configJobsPerHour: 0}\n",             # not positive
    "limits: {configJobsPerHour: 1.5}\n",           # not whole
    "limits: {configJobsPerHour: true}\n",          # a bool is not a number
    "limits: {configJobsPerHour: 100001}\n",        # above the largest accepted
    "limits: {configJobsPerHour: '5'}\n",           # a string
    "limits: {blastRadius: 5}\n",                   # a limit the platform does not know
    "limits: [1, 2]\n",                             # not a mapping
])
def test_onboard_fails_on_an_invalid_limit(client, monkeypatch, limits):
    """An invalid or unknown limit fails onboarding instead of being silently ignored."""
    assert _onboard_with_manifest(client, monkeypatch, limits)["state"] == "FAILED"


# ---------------------------------------------------------------- OI-2-package-redeploy

@pytest.mark.parametrize("earlier_state", ["DELETING", "FAILED"])
def test_a_csar_can_be_onboarded_again_once_its_package_is_deleted_or_failed(client, db_session_factory, monkeypatch, earlier_state):
    """The duplicate-content check ignores DELETING and FAILED packages, so the same CSAR is not locked out for good."""
    package_bytes = _real_package_bytes()
    _mock_fetch(monkeypatch, package_bytes)
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))
    first_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]
    with db_session_factory() as session:
        session.get(ApplicationPackage, uuid.UUID(first_id)).state = earlier_state
        session.commit()

    second_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]

    assert client.get(f"/packages/{second_id}/onboarding-status").json()["state"] == "AVAILABLE"


def test_a_deleted_package_can_be_onboarded_again_end_to_end(client, monkeypatch):
    """Deleting a package through the route and onboarding the same CSAR again gives a new AVAILABLE package."""
    _mock_fetch(monkeypatch, _real_package_bytes())
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))
    first_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]
    assert client.delete(f"/packages/{first_id}").json()["state"] == "DELETING"

    second_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]
    assert client.get(f"/packages/{second_id}/onboarding-status").json()["state"] == "AVAILABLE"


@pytest.mark.parametrize("earlier_state", ["AVAILABLE", "PRIMED", "DEPRECATED"])
def test_a_live_package_still_blocks_a_duplicate(client, db_session_factory, monkeypatch, earlier_state):
    """While the earlier package is AVAILABLE, PRIMED or DEPRECATED, a second onboarding of the same content ends FAILED."""
    _mock_fetch(monkeypatch, _real_package_bytes())
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))
    first_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]
    with db_session_factory() as session:
        session.get(ApplicationPackage, uuid.UUID(first_id)).state = earlier_state
        session.commit()

    second_id = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()["packageId"]
    assert client.get(f"/packages/{second_id}/onboarding-status").json()["state"] == "FAILED"


# ---------------------------------------------------------------- OI-2-lcm-error-mapping

def _set_state(db_session_factory, package_id, state):
    """Writes `state` straight onto the package row, to set up a state no route reaches in one step."""
    with db_session_factory() as session:
        session.get(ApplicationPackage, uuid.UUID(package_id)).state = state
        session.commit()


@pytest.mark.parametrize("route,state,event", [
    ("deprecate", "PRIMED", "DEPRECATE"),
    ("deprecate", "DEPRECATED", "DEPRECATE"),
    ("prime", "DEPRECATED", "PRIME"),
    ("prime", "PRIMED", "PRIME"),
    ("deprime", "AVAILABLE", "DEPRIME"),
    ("cancel-delete", "AVAILABLE", "CANCEL_DELETE"),
])
def test_illegal_lifecycle_events_are_409_naming_state_and_event(client, db_session_factory, monkeypatch, route, state, event):
    """An event with no edge from the current state is 409 LIFECYCLE_ILLEGAL_TRANSITION and the message names the state and the event."""
    package_id = _make_available_package(client, monkeypatch)
    _set_state(db_session_factory, package_id, state)

    resp = client.post(f"/packages/{package_id}/{route}")

    assert resp.status_code == 409
    problem = resp.json()["detail"]
    assert problem["title"] == "LIFECYCLE_ILLEGAL_TRANSITION"
    assert f"event {event}" in problem["detail"] and f"state {state}" in problem["detail"]


@pytest.mark.parametrize("state", ["PRIMED", "DELETING", "ONBOARDING"])
def test_delete_from_a_state_with_no_delete_edge_is_an_illegal_transition_not_a_dependent(client, db_session_factory, monkeypatch, state):
    """DELETE from PRIMED, DELETING or ONBOARDING is an illegal transition, not a "blocked by a dependent" refusal."""
    package_id = _make_available_package(client, monkeypatch)
    _set_state(db_session_factory, package_id, state)

    resp = client.delete(f"/packages/{package_id}")

    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "LIFECYCLE_ILLEGAL_TRANSITION"
    assert f"state {state}" in resp.json()["detail"]["detail"]


@pytest.mark.parametrize("method,path", [
    ("post", "/packages/{id}/deprecate"), ("post", "/packages/{id}/prime"), ("post", "/packages/{id}/deprime"),
    ("post", "/packages/{id}/cancel-delete"), ("delete", "/packages/{id}"),
    ("get", "/packages/{id}/artifacts"), ("get", "/packages/{id}/usage"),
])
def test_unknown_package_is_404_on_every_lifecycle_route(client, method, path):
    """Every route that takes a package id answers 404 PACKAGE_NOT_FOUND for an unknown one."""
    resp = getattr(client, method)(path.format(id=uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json()["detail"]["title"] == "PACKAGE_NOT_FOUND"


def test_usage_start_on_an_unknown_package_is_404(client):
    """A usage registration cannot be opened on a package that does not exist."""
    resp = client.post(f"/packages/{uuid.uuid4()}/usage/start", params={"consumer_id": "i-1"})
    assert resp.status_code == 404


def test_usage_stop_on_an_unknown_or_foreign_registration_is_404(client, monkeypatch):
    """Stopping a registration that does not exist, or that belongs to another package, is 404 and leaves the other package's registration open."""
    package_id = _make_available_package(client, monkeypatch, integrity_hash="a")
    other_id = _make_available_package(client, monkeypatch, integrity_hash="b")
    reg = client.post(f"/packages/{other_id}/usage/start", params={"consumer_id": "i-1"}).json()["registrationId"]

    unknown = client.post(f"/packages/{package_id}/usage/{uuid.uuid4()}/stop")
    foreign = client.post(f"/packages/{package_id}/usage/{reg}/stop")

    assert unknown.status_code == 404 and foreign.status_code == 404
    assert unknown.json()["detail"]["title"] == "PACKAGE_USAGE_REGISTRATION_NOT_FOUND"
    assert client.get(f"/packages/{other_id}/usage").json()["items"][0]["active"] is True  # untouched


def test_usage_stop_is_idempotent(client, monkeypatch):
    """A second stop answers 200 and keeps the first stoppedAt."""
    package_id = _make_available_package(client, monkeypatch)
    reg = client.post(f"/packages/{package_id}/usage/start", params={"consumer_id": "i-1"}).json()["registrationId"]
    client.post(f"/packages/{package_id}/usage/{reg}/stop")
    first = client.get(f"/packages/{package_id}/usage").json()["items"][0]["stoppedAt"]

    assert client.post(f"/packages/{package_id}/usage/{reg}/stop").status_code == 200
    assert client.get(f"/packages/{package_id}/usage").json()["items"][0]["stoppedAt"] == first


@pytest.mark.parametrize("location", [
    "http://169.254.169.254/latest/meta-data/pkg.csar",   # cloud metadata endpoint
    "http://127.0.0.1:8000/pkg.csar",
    "http://localhost/pkg.csar",
    "file:///etc/pkg.csar",
])
def test_onboard_never_fetches_a_disallowed_location(client, monkeypatch, location):
    """A location that fails the SSRF guard (metadata address, loopback, localhost, a non-http scheme) ends FAILED and the network is never touched (CodeQL py/full-ssrf).
    """
    def must_not_fetch(*args, **kwargs):
        raise AssertionError("a disallowed package location was fetched")
    monkeypatch.setattr("app.main.httpx.get", must_not_fetch)

    resp = client.post("/packages", json={"location": location})
    assert resp.status_code == 202
    status = client.get(f"/packages/{resp.json()['packageId']}/onboarding-status")
    assert status.json()["state"] == "FAILED"


# ---------------------------------------------------------------- fuzz findings (fuzz/fuzz_csar_parsers.py)

def _zip_with(**entries):
    """Builds an in-memory zip from keyword entries; `__` in a name becomes `/` and `_` becomes `.` (so `Files__Sme__a_json` is `Files/Sme/a.json`).
    """
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in entries.items():
            z.writestr(name.replace("__", "/").replace("_", "."), content)
    return zipfile.ZipFile(BytesIO(buf.getvalue()))


def test_a_non_utf8_sme_declaration_is_a_validation_failure_not_a_crash():
    """A `Files/Sme` file that is not UTF-8 raises an error from ONBOARD_VALIDATION_FAILURES (found by the fuzzer: it used to escape as an unhandled UnicodeDecodeError).
    """
    from app.main import ONBOARD_VALIDATION_FAILURES, _parse_sme_declarations

    z = _zip_with(Files__Sme__serviceapis__a_json=b"\xf7\xff\xfe")
    with pytest.raises(ONBOARD_VALIDATION_FAILURES):
        _parse_sme_declarations(z)


# Each case is a manifest.yaml or capabilities.yaml whose top level, or whose rappManifest / capabilities section, is a list instead of a mapping.
@pytest.mark.parametrize("name, content", [
    ("manifest_yaml", "- a\n- b\n"),                  # a list, not a mapping
    ("manifest_yaml", "rappManifest: [1, 2]\n"),      # rappManifest not a mapping
    ("capabilities_yaml", "- consumes\n"),
    ("capabilities_yaml", "capabilities: [x]\n"),
])
def test_a_manifest_or_capabilities_file_that_is_not_a_mapping_fails_validation(name, content):
    """A manifest or capabilities file of the wrong shape raises PackageValidationFailed (a validation failure), not an unhandled error."""
    from app.main import PackageValidationFailed, _parse_ai_capabilities

    with pytest.raises(PackageValidationFailed):
        _parse_ai_capabilities(_zip_with(**{name: content}))


def test_onboarding_a_package_with_a_non_utf8_sme_file_ends_in_failed(client, monkeypatch):
    """The same non-UTF-8 SME file through the route gives a 202 and a FAILED package, not a 500."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("TOSCA-Metadata/TOSCA.meta", "Entry-Definitions: Definitions/asd.yaml\n")
        z.writestr("Definitions/asd.yaml", "application_name: demo\n")
        z.writestr("Files/Sme/serviceapis/a.json", b"\xf7\xff\xfe")

    class FakeHttpResponse:
        content = buf.getvalue()
        def raise_for_status(self):
            pass

    monkeypatch.setattr("app.main.httpx.get", lambda location, timeout=None: FakeHttpResponse())
    resp = client.post("/packages", json={"location": "http://example/pkg.csar"})
    assert resp.status_code == 202
    status = client.get(f"/packages/{resp.json()['packageId']}/onboarding-status")
    assert status.json()["state"] == "FAILED"


def test_a_concurrent_writer_turns_a_transition_into_a_409_and_the_repeat_succeeds(client, monkeypatch):
    """PR-ST-2: ApplicationPackage is versioned, so a write made on a stale row is 409 CONCURRENT_MODIFICATION, not a lost update, and repeating the request succeeds.
    """
    package_id = _make_available_package(client, monkeypatch)

    with concurrent_commit_on("application_package") as fired:
        stale = client.post(f"/packages/{package_id}/deprecate")
    assert fired and stale.status_code == 409
    assert stale.json()["detail"]["title"] == "CONCURRENT_MODIFICATION"

    repeat = client.post(f"/packages/{package_id}/deprecate")
    assert repeat.status_code == 200 and repeat.json()["state"] == "DEPRECATED"


def test_an_application_type_the_column_does_not_accept_is_a_422_not_a_500(client):
    """applicationType is validated against the column's CHECK values, so an unknown one is a 422 (the authenticated DAST scan, V-7d, got a 500 from it).
    """
    response = client.post("/packages", json={"location": "http://example.invalid/x.csar", "applicationType": "string"})
    assert response.status_code == 422


# ---------------------------------------------------------------- GUI-8.2: operatorUi in the manifest (docs/adr/0004-operator-ui-declaration.md)

_EXAMPLE = Path(__file__).resolve().parents[2] / "docs" / "schemas" / "operator-ui.energy-saving.example.yaml"


def _onboard_raw(client, monkeypatch, manifest_yaml):
    """Onboards a package carrying `manifest_yaml` with NFO faked and returns (the POST /packages answer, the package as listed)."""
    _mock_fetch(monkeypatch, _real_package_bytes(manifest_yaml=manifest_yaml))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))
    answer = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()
    return answer, next(p for p in client.get("/packages").json()["items"] if p["packageId"] == answer["packageId"])


def _manifest_with(operator_ui, *, under_rapp_manifest=False) -> str:
    """A manifest.yaml text whose operatorUi is `operator_ui`, at the top level or (`under_rapp_manifest`) inside rappManifest."""
    if under_rapp_manifest:
        return yaml.safe_dump({"rappManifest": {"manifestVersion": "1.0", "operatorUi": operator_ui}})
    return yaml.safe_dump({"rappManifest": {"manifestVersion": "1.0"}, "operatorUi": operator_ui})


def _example() -> dict:
    """The operatorUi declaration of the ADR worked example (docs/schemas/operator-ui.energy-saving.example.yaml)."""
    return yaml.safe_load(_EXAMPLE.read_text())["operatorUi"]


def test_the_adr_worked_example_onboards_and_is_stored_in_ai_capabilities(client, monkeypatch):
    """The example of docs/adr/0004-operator-ui-declaration.md onboards, is stored under aiCapabilities.operatorUi and is readable from onboarding-status (the route rapp-mgmt and the GUI backend use).
    """
    answer, pkg = _onboard_raw(client, monkeypatch, _EXAMPLE.read_text())
    assert pkg["state"] == "AVAILABLE" and "failureReason" not in answer
    stored = pkg["aiCapabilities"]["operatorUi"]
    assert [p["id"] for p in stored["panels"]] == ["instance", "controls", "cells"] and stored["readOnly"] is False
    # the GUI backend reads it from the route rapp-mgmt already uses
    status = client.get(f"/packages/{answer['packageId']}/onboarding-status").json()
    assert status["aiCapabilities"]["operatorUi"] == stored


def test_operator_ui_is_accepted_under_rapp_manifest_too(client, monkeypatch):
    """operatorUi is accepted inside rappManifest as well as at the top level."""
    _, pkg = _onboard_raw(client, monkeypatch, _manifest_with(_example(), under_rapp_manifest=True))
    assert pkg["state"] == "AVAILABLE" and "operatorUi" in pkg["aiCapabilities"]


def test_a_package_without_operator_ui_is_unchanged(client, monkeypatch):
    """A manifest without operatorUi onboards with no operatorUi key and no failureReason."""
    answer, pkg = _onboard_raw(client, monkeypatch, "rappManifest:\n  manifestVersion: \"1.0\"\n")
    assert pkg["state"] == "AVAILABLE" and "operatorUi" not in pkg["aiCapabilities"] and "failureReason" not in answer


def test_extension_keys_are_dropped_from_the_stored_declaration(client, monkeypatch):
    """An `x-` extension key in operatorUi is accepted but not stored."""
    d = _example()
    d["x-vendor"] = {"a": 1}
    _, pkg = _onboard_raw(client, monkeypatch, _manifest_with(d))
    assert pkg["state"] == "AVAILABLE" and "x-vendor" not in pkg["aiCapabilities"]["operatorUi"]


def _mutate(fn):
    """The example declaration after `fn` has changed it, for building one bad declaration per case."""
    d = _example()
    fn(d)
    return d


# Each case is the example with one thing broken, and the text that failureReason must contain (the place in the declaration and the rule): a bad kind, a non-GET
# source, a non-mutating action, `..` in a path, too many panels or blocks, duplicate ids, a bad rowDetail, an unknown key or version, an oversize value, readOnly with
# actions, and a declaration that is not a mapping.
@pytest.mark.parametrize("declaration, message", [
    (_mutate(lambda d: d["panels"][0].update(kind="map")), "operatorUi.panels[0].kind: 'map' is not a panel kind"),
    (_mutate(lambda d: d["panels"][0]["source"].update(method="POST")), "operatorUi.panels[0].source.method: a panel source must be a GET"),
    (_mutate(lambda d: d["panels"][1]["actions"][0].update(method="GET")), "operatorUi.panels[1].actions[0].method: an action must change something"),
    (_mutate(lambda d: d["panels"][1]["actions"][0].update(path="/instances/{instanceId}/../admin")), "must not contain '..'"),
    (_mutate(lambda d: d["panels"][0]["source"].update(path="/a/%2e%2e/b")), "operatorUi.panels[0].source.path"),
    (_mutate(lambda d: d["panels"][2]["columns"][0].update(path="a..b")), "operatorUi.panels[2].columns[0].path: must not contain '..'"),
    (_mutate(lambda d: d["panels"].extend({**d["panels"][1], "id": f"extra-{i}"} for i in range(20))), "operatorUi.panels: must have 1 to 20 entries"),
    (_mutate(lambda d: d["panels"][2].update(id="instance")), "'instance' is used by an earlier panel"),
    (_mutate(lambda d: d["panels"][1]["actions"][1].update(id="evaluate")), "'evaluate' is used twice"),
    (_mutate(lambda d: d["panels"][2]["rowDetail"]["blocks"][0].update(kind="map")), "operatorUi.panels[2].rowDetail.blocks[0].kind: 'map' is not a rowDetail block kind"),
    (_mutate(lambda d: d["panels"][2]["rowDetail"]["blocks"].extend([d["panels"][2]["rowDetail"]["blocks"][1]] * 4)), "rowDetail.blocks: must have 1 to 6 entries"),
    (_mutate(lambda d: d["panels"][2]["rowDetail"]["blocks"][2]["source"].update(method="DELETE")), "rowDetail.blocks[2].source.method: a panel source must be a GET"),
    (_mutate(lambda d: d["panels"][2]["rowDetail"]["blocks"][2]["source"].update(path="/instances/{instanceId}/../x")), "must not contain '..'"),
    (_mutate(lambda d: d["panels"][2]["rowDetail"]["blocks"][2]["source"]["query"].update(cell_id="{row.nope}")), "{row.nope} names a field"),
    (_mutate(lambda d: d["panels"][2]["rowDetail"]["blocks"][1].update(rowDetail={"blocks": []})), "cannot be nested inside a rowDetail"),
    (_mutate(lambda d: d.update(version=2)), "operatorUi.version: 2 is not supported"),
    (_mutate(lambda d: d.update(colour="red")), "unknown key 'colour'"),
    (_mutate(lambda d: d["panels"][0].update(title="x" * 100_000)), "over the limit of 65536"),
    (_mutate(lambda d: d.update(readOnly=True)), "operatorUi is readOnly"),
    ("not a mapping", "operatorUi: must be a mapping"),
])
def test_a_bad_declaration_is_refused_with_the_place_and_the_rule(client, monkeypatch, declaration, message):
    """A bad operatorUi ends the package FAILED, returns the place and rule as failureReason, and stores neither aiCapabilities nor a descriptor id.
    """
    answer, pkg = _onboard_raw(client, monkeypatch, _manifest_with(declaration))
    assert pkg["state"] == "FAILED"
    assert message in answer["failureReason"]
    assert pkg["aiCapabilities"] is None and pkg["nfDeploymentDescriptorId"] is None


def test_a_refusal_is_logged_with_the_reason(client, monkeypatch, caplog):
    """The failure reason is written to the log as a warning, since it is returned but never stored."""
    import logging
    with caplog.at_level(logging.WARNING, logger="app.main"):
        _onboard_raw(client, monkeypatch, _manifest_with(_mutate(lambda d: d["panels"][0].update(kind="map"))))
    assert any("failed validation" in r.getMessage() and "not a panel kind" in r.getMessage() for r in caplog.records)


def test_other_failures_report_their_kind_not_internals(client, monkeypatch):
    """A failure that is not a PackageValidationFailed reports only the exception class name as failureReason."""
    class FakeHttpResponse:
        content = b"not a real zip file"

        def raise_for_status(self):
            pass

    monkeypatch.setattr("app.main.httpx.get", lambda location, timeout=None: FakeHttpResponse())
    answer = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()
    assert answer["failureReason"] == "BadZipFile"


def test_the_row_detail_is_stored_and_its_per_row_source_is_a_declared_read(client, monkeypatch):
    """The rowDetail blocks are stored, and the per-row source is among the GET routes the declaration declares."""
    from smo_shared.operator_ui import declared_routes
    _, pkg = _onboard_raw(client, monkeypatch, _EXAMPLE.read_text())
    stored = pkg["aiCapabilities"]["operatorUi"]
    assert [b["kind"] for b in stored["panels"][2]["rowDetail"]["blocks"]] == ["chart", "json", "table"]
    assert ("GET", "/instances/{instanceId}/decisions") in declared_routes(stored)


# ---------------------------------------------------------------- PR-RAPP-2.1: the profile as container resources in the NFO descriptor

def test_the_descriptor_carries_the_manifest_cpu_and_memory_as_requests_and_limits_per_mode(client, monkeypatch):
    """PR-RAPP-2.1: the descriptor sent to NFO carries containerResourcesByMode with the manifest's cpu and memory as equal requests and limits for each mode.
    """
    sent = []

    def post(self, path, json=None, **kw):
        sent.append((path, json))
        return FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())})
    manifest = "executionModes: [TRAINING, INFERENCE]\nruntimeProfiles:\n  TRAINING: {cpu: 8, memory: 16Gi, gpu: 1}\n  INFERENCE: {cpu: 0.5, memory: 512Mi}\n"
    _mock_fetch(monkeypatch, _real_package_bytes(manifest_yaml=manifest))
    monkeypatch.setattr("app.main.R1Client.post", post)
    assert client.post("/packages", json={"location": "http://example/pkg.csar"}).status_code == 202
    [(path, body)] = sent
    assert path == "/nfo/descriptors" and body["workloadTemplate"]["toscaEntryDefinitions"] == "Definitions/main.yaml"
    assert body["workloadTemplate"]["containerResourcesByMode"] == {
        "TRAINING": {"requests": {"cpu": "8", "memory": "16Gi"}, "limits": {"cpu": "8", "memory": "16Gi"}},
        "INFERENCE": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}}}


def test_a_package_without_runtime_profiles_gets_the_descriptor_it_always_got(client, monkeypatch):
    """Without runtimeProfiles the workload template has only toscaEntryDefinitions, with no containerResourcesByMode key."""
    sent = []
    _mock_fetch(monkeypatch, _real_package_bytes(manifest_yaml="rappManifest:\n  manifestVersion: \"1.0\"\n"))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: sent.append(json) or FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))
    client.post("/packages", json={"location": "http://example/pkg.csar"})
    assert sent[0]["workloadTemplate"] == {"toscaEntryDefinitions": "Definitions/main.yaml"}


def test_a_mode_with_only_a_gpu_adds_no_resources_for_that_mode(client, monkeypatch):
    """A mode whose profile has no cpu or memory yields no container resources, so it is left out of containerResourcesByMode."""
    sent = []
    _mock_fetch(monkeypatch, _real_package_bytes(manifest_yaml="runtimeProfiles:\n  INFERENCE: {gpu: 1}\n  TRAINING: {cpu: 2}\n"))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: sent.append(json) or FakeR1Response(201, {"nfDeploymentDescriptorId": str(uuid.uuid4())}))
    client.post("/packages", json={"location": "http://example/pkg.csar"})
    assert list(sent[0]["workloadTemplate"]["containerResourcesByMode"]) == ["TRAINING"]


def test_an_unsigned_zip_bomb_ends_the_package_failed_with_the_reason(client, monkeypatch):
    """SEC-15.17 end to end: a package whose manifest inflates to 60 MiB is FAILED with `failureReason` naming the size, no descriptor is created and no aiCapabilities stored."""
    answer, pkg = _onboard_raw(client, monkeypatch, "x: " + "0" * (60 * 1024 * 1024))
    assert pkg["state"] == "FAILED" and "too large to read" in answer["failureReason"]
    assert pkg["aiCapabilities"] is None and pkg["nfDeploymentDescriptorId"] is None
