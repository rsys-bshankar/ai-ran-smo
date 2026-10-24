"""Tests of NFO's routes: descriptors, Instantiate with its guards, Terminate, Heal, Scale, queries, listing, concurrency
and idempotency.

Run: `cd smo/nfo && PYTHONPATH=.:../shared python -m pytest tests -q`. No Postgres: the fixtures build the tables on SQLite (plus a
stub `application_package` table), so the foreign keys are not enforced and the delete-order bug they guard against is only
covered indirectly. FOCOM is faked by patching `app.main.R1Client.get`. The asynchronous terminate and the deployment manager's
notifications are in `test_dms_lifecycle.py`, which imports the fixtures and helpers defined here.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, Column, Table
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import Uuid as UuidType

from smo_shared.db import Base, get_session
from smo_shared.idempotency import IdempotencyKey
from smo_shared.testing import concurrent_commit_on

from app.main import app
from app.models import LCMOperation, NFDeployment, NFDeploymentDescriptor, NFOCloudResource


class FakeR1Response:
    """Stands in for an `httpx.Response` (`status_code` and `json()` only)."""
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def db_session_factory():
    """Builds the NFO tables, a stub `application_package` table and the idempotency table on an in-memory SQLite engine; returns a session factory."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if "application_package" not in Base.metadata.tables:
        Table("application_package", Base.metadata, Column("package_id", UuidType, primary_key=True))
    Base.metadata.create_all(engine, tables=[
        Base.metadata.tables["application_package"], NFDeploymentDescriptor.__table__, NFDeployment.__table__,
        NFOCloudResource.__table__, LCMOperation.__table__, IdempotencyKey.__table__,
    ])
    return sessionmaker(bind=engine)


@pytest.fixture
def client(db_session_factory):
    """A TestClient with `get_session` overridden. `raise_server_exceptions=False` so a server error is returned as a 500 response
    rather than raised into the test.
    """
    def override_get_session():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    # raise_server_exceptions=False: this suite intentionally exercises an
    # unguarded-None error path (querying a deleted deployment) and asserts
    # on the resulting HTTP status, not the raised Python exception.
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _create_descriptor(client) -> str:
    """Creates a descriptor through the API and returns its id."""
    resp = client.post("/descriptors", json={
        "packageId": str(uuid.uuid4()), "name": "Definitions/main.yaml",
        "workloadTemplate": {"toscaEntryDefinitions": "Definitions/main.yaml"},
    })
    return resp.json()["nfDeploymentDescriptorId"]


def _instantiate(client, monkeypatch, name="nf-1", descriptor_id=None, cluster_id="c1"):
    """Patches the FOCOM inventory read to return `cluster_id`, creates a descriptor unless one is given, and POSTs an
    Instantiate; returns the response.
    """
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeR1Response(200, {"oCloudId": cluster_id}))
    descriptor_id = descriptor_id or _create_descriptor(client)
    return client.post("/deployments", json={"nfDeploymentDescriptorId": descriptor_id, "name": name})


def test_create_descriptor_persists_a_real_row(client, db_session_factory):
    """`POST /descriptors` writes a descriptor row with the package id, resource type and workload template that were sent."""
    package_id = uuid.uuid4()
    resp = client.post("/descriptors", json={
        "packageId": str(package_id), "name": "Definitions/main.yaml",
        "workloadTemplate": {"toscaEntryDefinitions": "Definitions/main.yaml"},
        "requiredResourceTypeId": "gpu-l40",
    })
    assert resp.status_code == 201
    descriptor_id = uuid.UUID(resp.json()["nfDeploymentDescriptorId"])

    with db_session_factory() as session:
        descriptor = session.get(NFDeploymentDescriptor, descriptor_id)
        assert descriptor is not None
        assert descriptor.package_id == package_id
        assert descriptor.required_resource_type_id == "gpu-l40"
        assert descriptor.workload_template == {"toscaEntryDefinitions": "Definitions/main.yaml"}


def test_create_descriptor_without_a_package_id_for_a_model_runtime(client, db_session_factory):
    """A descriptor without a package (as created for a model runtime) is accepted, stored with no package id, and listed with `packageId` None."""
    resp = client.post("/descriptors", json={
        "name": "aimgf-model-<id>-runtime", "workloadTemplate": {"modelId": "some-model-id"},
    })
    assert resp.status_code == 201
    descriptor_id = uuid.UUID(resp.json()["nfDeploymentDescriptorId"])

    with db_session_factory() as session:
        descriptor = session.get(NFDeploymentDescriptor, descriptor_id)
        assert descriptor.package_id is None

    listed = client.get("/descriptors").json()["items"]
    assert next(d for d in listed if d["nfDeploymentDescriptorId"] == str(descriptor_id))["packageId"] is None


def test_instantiate_resolves_cluster_via_focom(client, monkeypatch):
    """Instantiate takes the cluster id from FOCOM's inventory and answers RUNNING."""
    resp = _instantiate(client, monkeypatch, cluster_id="focom-resolved-cluster")
    assert resp.status_code == 202
    assert resp.json()["clusterId"] == "focom-resolved-cluster"
    assert resp.json()["state"] == "RUNNING"


# Each row is a FOCOM answer that names no O-Cloud: an error status, a 200 without `oCloudId`, no answer at all.
@pytest.mark.parametrize("answer", [FakeR1Response(503, {}), FakeR1Response(200, {}), None])
def test_instantiate_is_refused_when_focom_names_no_o_cloud(client, monkeypatch, db_session_factory, answer):
    """Call flow 15b: a workload is never placed on a cluster nobody named. 503 ENDPOINT_UNREACHABLE and no deployment is created."""
    import httpx

    def get(self, path, **kw):
        if answer is None:
            raise httpx.ConnectError("down")
        return answer
    descriptor_id = _create_descriptor(client)
    monkeypatch.setattr("app.main.R1Client.get", get)
    resp = client.post("/deployments", json={"nfDeploymentDescriptorId": descriptor_id, "name": "nf-1"})
    assert resp.status_code == 503 and resp.json()["detail"]["title"] == "ENDPOINT_UNREACHABLE"
    with db_session_factory() as session:
        assert session.query(NFDeployment).count() == 0


def test_instantiate_creates_a_resource_link(client, monkeypatch):
    """Instantiate writes one COMPUTE resource link whose reference is the cluster id."""
    resp = _instantiate(client, monkeypatch, cluster_id="focom-resolved-cluster")
    nf_deployment_id = resp.json()["nfDeploymentId"]

    links = client.get(f"/deployments/{nf_deployment_id}/resources").json()
    assert len(links) == 1
    assert links[0]["resourceRef"] == "focom-resolved-cluster"
    assert links[0]["vresourceType"] == "COMPUTE"


def test_instantiate_rejects_unknown_descriptor(client, monkeypatch):
    """A descriptor id that does not exist is refused up front with 422 NFDEPLOYMENT_DESCRIPTOR_NOT_FOUND."""
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeR1Response(200, {"oCloudId": "c1"}))
    resp = client.post("/deployments", json={"nfDeploymentDescriptorId": str(uuid.uuid4()), "name": "nf-1"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "NFDEPLOYMENT_DESCRIPTOR_NOT_FOUND"


def test_instantiate_rejects_duplicate_name(client, monkeypatch):
    """A second deployment with an existing name is a 409 NFDEPLOYMENT_NAME_CONFLICT."""
    _instantiate(client, monkeypatch, name="nf-1")
    resp = _instantiate(client, monkeypatch, name="nf-1")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "NFDEPLOYMENT_NAME_CONFLICT"


def test_a_descriptor_backs_several_deployments(client, monkeypatch):
    """Call flow 15b (DOC-CF-1.4): a descriptor is a template, so a second Instantiate under another name is a second deployment (one per rApp
    instance of a package); the name stays unique."""
    descriptor_id = _create_descriptor(client)
    first = _instantiate(client, monkeypatch, name="nf-1", descriptor_id=descriptor_id)
    second = _instantiate(client, monkeypatch, name="nf-2", descriptor_id=descriptor_id)
    assert first.status_code == second.status_code == 202
    assert first.json()["nfDeploymentId"] != second.json()["nfDeploymentId"]


def test_deployment_state_changes_are_published_on_the_event_bus(client, monkeypatch):
    """Call flow 15e: every state change of a deployment is `org.oran.smo.nf-deployment.state-changed` about it, the end of a terminate included."""
    from smo_shared import events
    monkeypatch.setenv("SMO_EVENT_BUS", "memory")
    monkeypatch.setattr(events.MEMORY, "events", [])
    dep_id = _instantiate(client, monkeypatch).json()["nfDeploymentId"]
    client.post(f"/deployments/{dep_id}/dms-notifications", json={"event": "RUNTIME_FAILURE", "detail": "oom"})
    client.post(f"/deployments/{dep_id}/heal")
    client.delete(f"/deployments/{dep_id}")

    mine = [e for e in events.MEMORY.events if e["subject"] == dep_id]
    assert {e["type"] for e in mine} == {"org.oran.smo.nf-deployment.state-changed"}
    assert [(e["data"]["event"], e["data"]["to"]) for e in mine] == [
        ("INSTANTIATE", "INSTANTIATING"), ("INSTANTIATE_COMPLETE", "RUNNING"), ("RUNTIME_FAILURE", "ABNORMAL"), ("HEAL", "RUNNING"),
        ("TERMINATE", "TERMINATING"), ("UNINSTALL_COMPLETE", "DELETING"), ("DELETE_COMPLETE", "DELETED")]


def test_terminate_removes_deployment(client, monkeypatch):
    """A synchronous Terminate answers 204 and the deployment no longer exists (its placement query is a 404, which once answered 500)."""
    created = _instantiate(client, monkeypatch).json()

    del_resp = client.delete(f"/deployments/{created['nfDeploymentId']}")
    assert del_resp.status_code == 204

    # a placement query against a deleted deployment is a 404 (the contract test found it answering 500)
    resp = client.get(f"/deployments/{created['nfDeploymentId']}/placement")
    assert resp.status_code == 404


def test_terminate_from_running_also_removes_its_resource_link(client, monkeypatch, db_session_factory):
    """Terminate removes the deployment's resource link rows as well."""
    created = _instantiate(client, monkeypatch).json()
    client.delete(f"/deployments/{created['nfDeploymentId']}")

    with db_session_factory() as session:
        remaining = session.query(NFOCloudResource).filter_by(nf_deployment_id=uuid.UUID(created["nfDeploymentId"])).all()
        assert remaining == []


def test_terminate_removes_its_lcm_operation_history(client, monkeypatch, db_session_factory):
    """Terminate removes the operation history, including the TERMINATE row it just added; on Postgres the foreign key would
    otherwise reject the delete.
    """
    created = _instantiate(client, monkeypatch).json()
    nf_deployment_id = uuid.UUID(created["nfDeploymentId"])

    del_resp = client.delete(f"/deployments/{nf_deployment_id}")
    assert del_resp.status_code == 204

    with db_session_factory() as session:
        remaining = session.query(LCMOperation).filter_by(nf_deployment_id=nf_deployment_id).all()
        assert remaining == []


def test_terminate_after_a_scale_still_removes_its_lcm_operation_history(client, monkeypatch, db_session_factory):
    """Deploy, scale, terminate leaves no operation rows. Guards the `flush()` before the bulk delete in `_remove_deployment`:
    the session does not autoflush, so without it the just-added TERMINATE row would be missed and the foreign key would fail on
    Postgres (SQLite does not enforce it, so this test only re-runs the sequence).
    """
    created = _instantiate(client, monkeypatch).json()
    nf_deployment_id = uuid.UUID(created["nfDeploymentId"])
    assert client.post(f"/deployments/{nf_deployment_id}/scale").status_code == 200

    del_resp = client.delete(f"/deployments/{nf_deployment_id}")
    assert del_resp.status_code == 204

    with db_session_factory() as session:
        assert session.query(LCMOperation).filter_by(nf_deployment_id=nf_deployment_id).all() == []


def test_terminate_again_on_already_terminating_deployment_is_a_noop(client, monkeypatch, db_session_factory):
    """Terminate on a TERMINATING deployment changes nothing and does not delete it."""
    created = _instantiate(client, monkeypatch).json()
    nf_deployment_id = uuid.UUID(created["nfDeploymentId"])

    with db_session_factory() as session:
        d = session.get(NFDeployment, nf_deployment_id)
        d.state = "TERMINATING"
        session.commit()

    resp = client.delete(f"/deployments/{nf_deployment_id}")
    assert resp.status_code == 204

    with db_session_factory() as session:
        assert session.get(NFDeployment, nf_deployment_id) is not None
        assert session.get(NFDeployment, nf_deployment_id).state == "TERMINATING"


def test_terminate_on_deleting_deployment_flips_to_abnormal_instead_of_deleting_twice(client, monkeypatch, db_session_factory):
    """Terminate on a DELETING deployment (a double-terminate race) makes it ABNORMAL instead of deleting it twice."""
    created = _instantiate(client, monkeypatch).json()
    nf_deployment_id = uuid.UUID(created["nfDeploymentId"])

    with db_session_factory() as session:
        d = session.get(NFDeployment, nf_deployment_id)
        d.state = "DELETING"
        session.commit()

    resp = client.delete(f"/deployments/{nf_deployment_id}")
    assert resp.status_code == 204

    with db_session_factory() as session:
        d = session.get(NFDeployment, nf_deployment_id)
        assert d is not None
        assert d.state == "ABNORMAL"


def test_heal_recovers_abnormal_deployment_to_running(client, monkeypatch, db_session_factory):
    """Heal takes an ABNORMAL deployment back to RUNNING."""
    created = _instantiate(client, monkeypatch).json()
    nf_deployment_id = uuid.UUID(created["nfDeploymentId"])
    with db_session_factory() as session:
        session.get(NFDeployment, nf_deployment_id).state = "ABNORMAL"
        session.commit()

    resp = client.post(f"/deployments/{nf_deployment_id}/heal")
    assert resp.status_code == 200
    assert resp.json()["state"] == "RUNNING"


def test_heal_on_already_running_deployment_is_idempotent(client, monkeypatch):
    """Heal on a RUNNING deployment succeeds and leaves it RUNNING."""
    created = _instantiate(client, monkeypatch).json()
    resp = client.post(f"/deployments/{created['nfDeploymentId']}/heal")
    assert resp.status_code == 200
    assert resp.json()["state"] == "RUNNING"


def test_heal_rejects_a_deployment_mid_instantiate(client, monkeypatch, db_session_factory):
    """Heal from INSTANTIATING is a 409 NFDEPLOYMENT_ILLEGAL_OPERATION: that state has no HEAL edge."""
    created = _instantiate(client, monkeypatch).json()
    nf_deployment_id = uuid.UUID(created["nfDeploymentId"])
    with db_session_factory() as session:
        session.get(NFDeployment, nf_deployment_id).state = "INSTANTIATING"
        session.commit()

    resp = client.post(f"/deployments/{nf_deployment_id}/heal")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "NFDEPLOYMENT_ILLEGAL_OPERATION"


def test_heal_unknown_deployment_is_404(client):
    """Heal on an unknown deployment is a 404."""
    resp = client.post(f"/deployments/{uuid.uuid4()}/heal")
    assert resp.status_code == 404


def test_scale_moves_running_deployment_through_updating_back_to_running(client, monkeypatch):
    """Scale on a RUNNING deployment completes in the request and leaves it RUNNING."""
    created = _instantiate(client, monkeypatch).json()
    resp = client.post(f"/deployments/{created['nfDeploymentId']}/scale")
    assert resp.status_code == 200
    assert resp.json()["state"] == "RUNNING"


def test_scale_rejects_a_deployment_not_running(client, monkeypatch, db_session_factory):
    """Scale on a deployment that is not RUNNING is a 409 NFDEPLOYMENT_ILLEGAL_OPERATION."""
    created = _instantiate(client, monkeypatch).json()
    nf_deployment_id = uuid.UUID(created["nfDeploymentId"])
    with db_session_factory() as session:
        session.get(NFDeployment, nf_deployment_id).state = "ABNORMAL"
        session.commit()

    resp = client.post(f"/deployments/{nf_deployment_id}/scale")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "NFDEPLOYMENT_ILLEGAL_OPERATION"


def test_scale_unknown_deployment_is_404(client):
    """Scale on an unknown deployment is a 404."""
    resp = client.post(f"/deployments/{uuid.uuid4()}/scale")
    assert resp.status_code == 404


def test_query_operation_status_after_instantiate(client, db_session_factory, monkeypatch):
    """The INSTANTIATE operation Instantiate wrote can be read back through `GET /operations/{id}` as COMPLETED."""
    created = _instantiate(client, monkeypatch).json()

    with db_session_factory() as session:
        op = session.query(LCMOperation).filter_by(
            nf_deployment_id=uuid.UUID(created["nfDeploymentId"]), operation_type="INSTANTIATE",
        ).one()

    resp = client.get(f"/operations/{op.operation_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "COMPLETED"


def test_query_operation_status_returns_completed_for_heal_and_scale(client, db_session_factory, monkeypatch):
    """The operation Heal writes is COMPLETED and readable through the operations route, not only stored."""
    created = _instantiate(client, monkeypatch).json()
    nf_deployment_id = uuid.UUID(created["nfDeploymentId"])
    with db_session_factory() as session:
        session.get(NFDeployment, nf_deployment_id).state = "ABNORMAL"
        session.commit()

    heal_resp = client.post(f"/deployments/{nf_deployment_id}/heal")
    assert heal_resp.status_code == 200

    with db_session_factory() as session:
        op = session.query(LCMOperation).filter_by(nf_deployment_id=nf_deployment_id, operation_type="HEAL").one()

    status_resp = client.get(f"/operations/{op.operation_id}")
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "COMPLETED"


def test_query_cluster_placement_for_existing_deployment(client, monkeypatch):
    """The placement query returns the cluster id and deployment id of an existing deployment."""
    resp_created = _instantiate(client, monkeypatch, cluster_id="focom-cluster")
    created = resp_created.json()

    resp = client.get(f"/deployments/{created['nfDeploymentId']}/placement")
    assert resp.status_code == 200
    assert resp.json()["clusterId"] == "focom-cluster"
    assert resp.json()["nfDeploymentId"] == created["nfDeploymentId"]


def test_terminate_unknown_deployment_is_idempotent(client):
    """Terminate on an id that was never created, or is already gone, is 204."""
    resp = client.delete(f"/deployments/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_query_resources_for_unknown_deployment_is_empty(client):
    """The resources route answers an empty list, not a 404, for an unknown deployment."""
    resp = client.get(f"/deployments/{uuid.uuid4()}/resources")
    assert resp.status_code == 200
    assert resp.json() == []


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """`/health` answers 200 `{status: healthy}`, which the GUI BFF's module status probe relies on."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_list_deployments_filters_by_state(client, monkeypatch):
    """`GET /deployments` lists deployments and its `state` filter keeps only those in that state."""
    assert client.get("/deployments").json()["items"] == []
    created = _instantiate(client, monkeypatch, name="d1").json()

    listed = client.get("/deployments").json()["items"]
    assert [(d["nfDeploymentId"], d["name"], d["state"]) for d in listed] == [(created["nfDeploymentId"], "d1", created["state"])]
    assert client.get("/deployments", params={"state": created["state"]}).json()["items"] == listed
    assert client.get("/deployments", params={"state": "ABNORMAL"}).json()["items"] == []


def test_list_descriptors_and_deployment_operations(client, monkeypatch):
    """The descriptor list and a deployment's operation history (in order: INSTANTIATE, then HEAL) are readable."""
    created = _instantiate(client, monkeypatch, name="d1").json()
    assert len(client.get("/descriptors").json()["items"]) == 1
    client.post(f"/deployments/{created['nfDeploymentId']}/heal")
    ops = client.get(f"/deployments/{created['nfDeploymentId']}/operations").json()["items"]
    assert [o["operationType"] for o in ops] == ["INSTANTIATE", "HEAL"]


def test_a_concurrent_writer_turns_a_heal_into_a_409_and_the_repeat_succeeds(client, monkeypatch, db_session_factory):
    """A write that loses a race on the versioned deployment row is a 409 CONCURRENT_MODIFICATION instead of overwriting, and repeating it succeeds."""
    created = _instantiate(client, monkeypatch).json()
    nf_deployment_id = uuid.UUID(created["nfDeploymentId"])
    with db_session_factory() as session:
        session.get(NFDeployment, nf_deployment_id).state = "ABNORMAL"
        session.commit()

    with concurrent_commit_on("nf_deployment") as fired:
        stale = client.post(f"/deployments/{nf_deployment_id}/heal")
    assert fired and stale.status_code == 409
    assert stale.json()["detail"]["title"] == "CONCURRENT_MODIFICATION"

    repeat = client.post(f"/deployments/{nf_deployment_id}/heal")
    assert repeat.status_code == 200 and repeat.json()["state"] == "RUNNING"


def test_instantiate_and_scale_with_an_idempotency_key_run_once(client, monkeypatch, db_session_factory):
    """A repeated Instantiate or Scale with the same Idempotency-Key is answered from the first answer and writes nothing twice."""
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeR1Response(200, {"oCloudId": "c1"}))
    descriptor_id = _create_descriptor(client)
    body = {"nfDeploymentDescriptorId": descriptor_id, "name": "nf-idem"}

    first = client.post("/deployments", json=body, headers={"Idempotency-Key": "inst-1"})
    again = client.post("/deployments", json=body, headers={"Idempotency-Key": "inst-1"})
    assert first.status_code == again.status_code == 202
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    with db_session_factory() as session:
        assert session.query(NFDeployment).count() == 1

    deployment_id = first.json()["nfDeploymentId"]
    scaled = client.post(f"/deployments/{deployment_id}/scale", headers={"Idempotency-Key": "scale-1"})
    repeated = client.post(f"/deployments/{deployment_id}/scale", headers={"Idempotency-Key": "scale-1"})
    assert scaled.status_code == repeated.status_code == 200 and repeated.headers["Idempotent-Replayed"] == "true"
    with db_session_factory() as session:
        assert session.query(LCMOperation).filter_by(operation_type="SCALE").count() == 1


def test_a_heal_with_an_idempotency_key_is_replayed_and_records_one_operation(client, monkeypatch, db_session_factory):
    """SA SMOS's RECONNECT sends a key derived from the MDAF report (SA-REPLAY): its repeat gets the first answer and records no second HEAL operation."""
    deployment_id = _instantiate(client, monkeypatch).json()["nfDeploymentId"]
    first = client.post(f"/deployments/{deployment_id}/heal", headers={"Idempotency-Key": "sa-smos-action:1"})
    again = client.post(f"/deployments/{deployment_id}/heal", headers={"Idempotency-Key": "sa-smos-action:1"})
    assert first.status_code == again.status_code == 200 and again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    with db_session_factory() as session:
        assert session.query(LCMOperation).filter_by(operation_type="HEAL").count() == 1


def test_an_unknown_operation_is_404(client):
    """An operation id that does not exist is a 404."""
    assert client.get("/operations/e3e70682-c209-1cac-a29f-6fbed82c07cd").status_code == 404


def test_delete_descriptor_removes_it_and_is_idempotent(client, db_session_factory):
    """DeleteDescriptor (call flow 06c) answers 204 and the row is gone; deleting it again, or an unknown id, is 204 too."""
    descriptor_id = _create_descriptor(client)

    assert client.delete(f"/descriptors/{descriptor_id}").status_code == 204
    with db_session_factory() as session:
        assert session.get(NFDeploymentDescriptor, uuid.UUID(descriptor_id)) is None
    assert client.delete(f"/descriptors/{descriptor_id}").status_code == 204
    assert client.delete(f"/descriptors/{uuid.uuid4()}").status_code == 204


def test_delete_descriptor_refused_while_deployed_then_allowed_after_terminate(client, monkeypatch):
    """A descriptor a deployment uses is 409 NFDEPLOYMENT_DESCRIPTOR_ALREADY_DEPLOYED; once that deployment is terminated the delete succeeds."""
    descriptor_id = _create_descriptor(client)
    deployment_id = _instantiate(client, monkeypatch, descriptor_id=descriptor_id).json()["nfDeploymentId"]

    refused = client.delete(f"/descriptors/{descriptor_id}")
    assert refused.status_code == 409 and refused.json()["detail"]["title"] == "NFDEPLOYMENT_DESCRIPTOR_ALREADY_DEPLOYED"

    client.delete(f"/deployments/{deployment_id}")
    assert client.delete(f"/descriptors/{descriptor_id}").status_code == 204
