"""Tests of MLMR's own routes: model registration, update and delete, artifact upload and download, coordination groups,
and the TS 28.105 views and repositories. The TS 29.482 routes (storages, discovery, storeDiscReqs) are in
`test_mlr_conformance.py`.

Run: `cd smo/mlmr && PYTHONPATH=.:../shared python -m pytest tests -q`. No Postgres is needed: the fixtures build the tables
on SQLite and override `get_session`. Foreign keys are not enforced there, so the database-level cascades are not covered.
Fixtures here (`db_session_factory`, `client`) are imported by `test_mlr_conformance.py`.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from smo_shared import audit
from smo_shared.db import Base, get_session
from smo_shared.idempotency import IdempotencyKey
from smo_shared.testing import make_test_engine

from app.main import app
from app.models import MLModel, MLModelCoordinationGroup, MLModelRepository, ModelArtifact, MLModelProfile, MLModelsStorage


@pytest.fixture
def db_session_factory():
    """Builds the MLMR tables and the idempotency-key table on an in-memory SQLite engine and returns a session factory. Uses `make_test_engine()` because the
    coordination group's array column needs its UUID-aware JSON fallback.
    """
    # make_test_engine(), not a plain create_engine("sqlite://", ...) —
    # MLModelCoordinationGroup.member_model_ids is an ARRAY(Uuid), whose
    # SQLite JSON fallback needs the UUID-aware serializer make_test_engine
    # provides (see smo_shared/testing.py's own docstring).
    engine = make_test_engine()
    Base.metadata.create_all(engine, tables=[
        MLModelRepository.__table__, MLModel.__table__, MLModelCoordinationGroup.__table__, ModelArtifact.__table__,
        MLModelsStorage.__table__, MLModelProfile.__table__, audit.AuditEntry.__table__, audit.AuditHead.__table__, IdempotencyKey.__table__,
    ])
    return sessionmaker(bind=engine)


@pytest.fixture
def client(db_session_factory):
    """A TestClient whose `get_session` dependency yields sessions from `db_session_factory`; the override is removed afterwards."""
    def override_get_session():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_model(db_session_factory, model_type="t") -> uuid.UUID:
    """Inserts a bare model directly through the ORM (bypassing the route) and returns its id."""
    model_id = uuid.uuid4()
    with db_session_factory() as session:
        session.add(MLModel(model_id=model_id, registration_id=str(uuid.uuid4()), model_type=model_type, version="1.0"))
        session.commit()
    return model_id


def test_get_model_by_id_returns_its_fields(client):
    """A model that was registered can be read back by id with its identity fields."""
    created = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()

    resp = client.get(f"/models/{created['modelId']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["modelId"] == created["modelId"]
    assert body["modelType"] == "coverage-predictor"
    assert body["version"] == "1.0"


def test_register_model_stores_and_exposes_registration_metadata(client):
    """Registration metadata (description, author, owner, data types, target environments) is stored and returned on read."""
    target_environments = [{"platformName": "k8s-cluster-1", "environmentType": "PRODUCTION", "dependencyList": "numpy==1.26"}]
    resp = client.post("/models", json={
        "modelType": "coverage-predictor", "version": "1.0", "description": "predicts coverage gaps",
        "author": "team-ran", "owner": "team-ran-oncall", "inputDataType": "csv", "outputDataType": "json",
        "targetEnvironments": target_environments,
    })
    model_id = resp.json()["modelId"]

    view = client.get(f"/models/{model_id}").json()
    assert view["description"] == "predicts coverage gaps"
    assert view["author"] == "team-ran"
    assert view["owner"] == "team-ran-oncall"
    assert view["inputDataType"] == "csv"
    assert view["outputDataType"] == "json"
    assert view["targetEnvironments"] == target_environments


# Headers R1 Termination stamps: the role and the invoker id of the caller.
_RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-client"}
_INTERNAL = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "smo-aimgf"}


def _registered_owner(client, headers, **body):
    """Registers a model with the given gateway headers and request fields, and returns the `owner` MLMR stores for it."""
    resp = client.post("/models", json={"modelType": "t-" + str(uuid.uuid4())[:8], "version": "1", **body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return client.get(f"/models/{resp.json()['modelId']}").json()["owner"]


def test_a_model_an_rapp_registers_is_owned_by_that_rapp(client):
    """SEC-15.3b: an rApp (role rapp) that names no owner is stamped as the owner with its invoker id; an empty owner counts as none."""
    assert _registered_owner(client, _RAPP) == "es-client"
    assert _registered_owner(client, _RAPP, owner="") == "es-client"


def test_an_owner_the_request_names_is_kept(client):
    """An internal caller registering for an rApp names the owner itself and it is kept; so is the owner an rApp names when it is its own id (SEC-15.3c)."""
    assert _registered_owner(client, _INTERNAL, owner="mobility-client") == "mobility-client"
    assert _registered_owner(client, _RAPP, owner="es-client") == "es-client"


def test_an_rapp_cannot_register_a_model_for_another_owner(client):
    """SEC-15.3c: an rApp naming an owner other than its own invoker id is refused 403 MODEL_OWNER_IMMUTABLE and nothing is stored; an internal caller and a call with no role may name anyone."""
    resp = client.post("/models", json={"modelType": "forged", "version": "1", "owner": "mobility-client"}, headers=_RAPP)
    assert resp.status_code == 403 and "MODEL_OWNER_IMMUTABLE" in resp.text
    assert client.get("/models", params={"model_type": "forged"}, headers=_INTERNAL).json()["items"] == []
    assert _registered_owner(client, {}, owner="mobility-client") == "mobility-client"


def _register_for(client, headers, **body) -> str:
    """Registers a model of a random type with the given gateway headers and returns its id."""
    resp = client.post("/models", json={"modelType": "t-" + str(uuid.uuid4())[:8], "version": "1", **body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["modelId"]


def _put_body(client, model_id, **fields) -> dict:
    """The PUT body that repeats the stored identity, plus the given fields."""
    stored = client.get(f"/models/{model_id}").json()
    return {"modelType": stored["modelType"], "version": stored["version"], **fields}


def test_the_owning_rapp_may_update_its_model_but_not_its_owner(client):
    """SEC-15.3c: the owner rApp updates the other fields; an `owner` equal to the stored one or omitted keeps it, and any other value, including an empty one, is 403 MODEL_OWNER_IMMUTABLE."""
    model_id = _register_for(client, _RAPP)
    for owner in ("es-client", None):
        body = _put_body(client, model_id, description=f"d-{owner}", **({} if owner is None else {"owner": owner}))
        resp = client.put(f"/models/{model_id}", json=body, headers=_RAPP)
        assert resp.status_code == 200 and resp.json()["owner"] == "es-client" and resp.json()["description"] == f"d-{owner}"
    for owner in ("other-client", ""):
        resp = client.put(f"/models/{model_id}", json=_put_body(client, model_id, description="x", owner=owner), headers=_RAPP)
        assert resp.status_code == 403 and "MODEL_OWNER_IMMUTABLE" in resp.text
    stored = client.get(f"/models/{model_id}").json()
    assert stored["owner"] == "es-client" and stored["description"] == "d-None"


def test_an_rapp_cannot_update_a_model_another_rapp_owns(client):
    """SEC-15.3c: another rApp's PUT on an owned model is 403 MODEL_ACCESS_DENIED, even when it repeats the stored owner, and changes nothing."""
    model_id = _register_for(client, {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "other-client"}, description="mine")
    for extra in ({}, {"owner": "other-client"}, {"owner": "es-client"}):
        resp = client.put(f"/models/{model_id}", json=_put_body(client, model_id, description="hijacked", **extra), headers=_RAPP)
        assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    assert client.get(f"/models/{model_id}").json()["description"] == "mine"


def test_internal_and_roleless_callers_keep_full_control_of_the_owner(client):
    """SEC-15.3c: an internal caller (an SMO module, the console) and a call with no role set, change and clear `owner` through PUT as before."""
    model_id = _register_for(client, _RAPP)
    resp = client.put(f"/models/{model_id}", json=_put_body(client, model_id, owner="mobility-client"), headers=_INTERNAL)
    assert resp.status_code == 200 and resp.json()["owner"] == "mobility-client"
    assert client.put(f"/models/{model_id}", json=_put_body(client, model_id, owner="x"), headers={}).json()["owner"] == "x"
    assert client.put(f"/models/{model_id}", json=_put_body(client, model_id), headers=_INTERNAL).json()["owner"] is None


def test_a_model_without_an_owner_stays_open_to_any_rapp_but_cannot_be_claimed(client):
    """SEC-15.3c: any rApp may update the other fields of an owner-less model; an empty or omitted `owner` leaves it unowned, and naming one (claiming it) is 403 MODEL_OWNER_IMMUTABLE."""
    model_id = _register_for(client, _INTERNAL)
    for extra in ({}, {"owner": ""}):
        resp = client.put(f"/models/{model_id}", json=_put_body(client, model_id, description="open", **extra), headers=_RAPP)
        assert resp.status_code == 200 and resp.json()["owner"] is None and resp.json()["description"] == "open"
    resp = client.put(f"/models/{model_id}", json=_put_body(client, model_id, owner="es-client"), headers=_RAPP)
    assert resp.status_code == 403 and "MODEL_OWNER_IMMUTABLE" in resp.text
    assert client.get(f"/models/{model_id}").json()["owner"] is None


_OTHER_RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "other-client"}
_ZIP = {"file": ("model.zip", b"pkzip-bytes", "application/zip")}


def _model_exists(db_session_factory, model_id) -> bool:
    """True when the model row is still in the database (read through a session of its own, not through the API)."""
    with db_session_factory() as db:
        return db.get(MLModel, uuid.UUID(model_id)) is not None


def _artifact_count(db_session_factory, model_id) -> int:
    """The number of artifact rows stored for the model, read straight from the database."""
    with db_session_factory() as db:
        return db.query(ModelArtifact).filter(ModelArtifact.model_id == uuid.UUID(model_id)).count()


def test_an_rapp_deletes_only_its_own_model(client, db_session_factory):
    """SEC-15.3d: the owner rApp's DELETE removes the model; another rApp's DELETE is 403 MODEL_ACCESS_DENIED and the model and its artifact are still in the database."""
    model_id = _register_for(client, _RAPP)
    client.post(f"/models/{model_id}/artifact", files=_ZIP, headers=_RAPP)
    resp = client.delete(f"/models/{model_id}", headers=_OTHER_RAPP)
    assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    assert _model_exists(db_session_factory, model_id) and _artifact_count(db_session_factory, model_id) == 1
    assert client.delete(f"/models/{model_id}", headers=_RAPP).status_code == 204
    assert not _model_exists(db_session_factory, model_id) and _artifact_count(db_session_factory, model_id) == 0


def test_internal_roleless_and_unowned_delete_are_not_refused(client, db_session_factory):
    """SEC-15.3d: an internal caller and a call with no role delete another rApp's model; a model with no owner is deleted by any rApp; an unknown id stays an idempotent 204 for an rApp."""
    for headers in (_INTERNAL, {}):
        model_id = _register_for(client, _RAPP)
        assert client.delete(f"/models/{model_id}", headers=headers).status_code == 204
        assert not _model_exists(db_session_factory, model_id)
    open_model = _register_for(client, _INTERNAL)
    assert client.delete(f"/models/{open_model}", headers=_OTHER_RAPP).status_code == 204
    assert not _model_exists(db_session_factory, open_model)
    assert client.delete(f"/models/{uuid.uuid4()}", headers=_OTHER_RAPP).status_code == 204


def test_an_rapp_uploads_an_artifact_only_for_its_own_model(client, db_session_factory):
    """SEC-15.3d: the owner rApp uploads (201, version 1); another rApp is 403 MODEL_ACCESS_DENIED and no artifact row or artifact location is written; an unknown model is 404 for every caller."""
    model_id = _register_for(client, _RAPP)
    resp = client.post(f"/models/{model_id}/artifact", files=_ZIP, headers=_OTHER_RAPP)
    assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    assert _artifact_count(db_session_factory, model_id) == 0 and client.get(f"/models/{model_id}").json()["artifactLocation"] is None
    assert client.post(f"/models/{model_id}/artifact", files=_ZIP, headers=_RAPP).json()["artifactVersion"] == 1
    for headers in (_OTHER_RAPP, _RAPP, _INTERNAL, {}):
        assert client.post(f"/models/{uuid.uuid4()}/artifact", files=_ZIP, headers=headers).status_code == 404


def test_internal_roleless_and_unowned_upload_are_not_refused(client):
    """SEC-15.3d: an internal caller and a call with no role upload for another rApp's model, and any rApp uploads for a model with no owner."""
    owned = _register_for(client, _RAPP)
    for headers in (_INTERNAL, {}):
        assert client.post(f"/models/{owned}/artifact", files=_ZIP, headers=headers).status_code == 201
    open_model = _register_for(client, _INTERNAL)
    assert client.post(f"/models/{open_model}/artifact", files=_ZIP, headers=_OTHER_RAPP).status_code == 201


def test_an_rapp_downloads_another_rapps_artifact_only_when_the_model_allows_it(client):
    """SEC-15.3d: another rApp is 403 MODEL_ACCESS_DENIED on an owned model that sets no accessReqs, or PRIVATE_USE_ONLY, or RESTRICTED without its id; PUBLICLY_AVAILABLE and RESTRICTED naming it pass. The
    owner, an internal caller, a call with no role and any rApp on a model with no owner download as before.
    """
    plain = _register_for(client, _RAPP)
    client.post(f"/models/{plain}/artifact", files=_ZIP, headers=_RAPP)
    assert client.get(f"/models/{plain}/artifact/1", headers=_OTHER_RAPP).status_code == 403
    for headers in (_RAPP, _INTERNAL, {}):
        assert client.get(f"/models/{plain}/artifact/1", headers=headers).content == b"pkzip-bytes"
    cases = {"PRIVATE_USE_ONLY": {}, "RESTRICTED": {"valClientIds": ["third-client"]}}
    for kind, extra in cases.items():
        model_id = _register_for(client, _RAPP, storeDiscReqs={"accessReqs": {"accessReq": kind, **extra}})
        client.post(f"/models/{model_id}/artifact", files=_ZIP, headers=_RAPP)
        assert client.get(f"/models/{model_id}/artifact/1", headers=_OTHER_RAPP).status_code == 403
    for access in ({"accessReq": "PUBLICLY_AVAILABLE"}, {"accessReq": "RESTRICTED", "valClientIds": ["other-client"]}):
        model_id = _register_for(client, _RAPP, storeDiscReqs={"accessReqs": access})
        client.post(f"/models/{model_id}/artifact", files=_ZIP, headers=_RAPP)
        assert client.get(f"/models/{model_id}/artifact/1", headers=_OTHER_RAPP).status_code == 200
    open_model = _register_for(client, _INTERNAL)
    client.post(f"/models/{open_model}/artifact", files=_ZIP, headers=_INTERNAL)
    assert client.get(f"/models/{open_model}/artifact/1", headers=_OTHER_RAPP).status_code == 200
    assert client.get(f"/models/{uuid.uuid4()}/artifact/1", headers=_OTHER_RAPP).status_code == 404


def test_an_rapp_may_write_phase_info_only_of_its_own_model(client):
    """SEC-15.3d: PATCH /models/{id}/phase-info by another rApp is 403 MODEL_ACCESS_DENIED and the phase is not written; the owner, an internal caller (AIMgF) and any rApp on an owner-less model write it; unknown is 404."""
    model_id = _register_for(client, _RAPP)
    refused = client.patch(f"/models/{model_id}/phase-info", json={"phase": "DEPLOYED"}, headers=_OTHER_RAPP)
    assert refused.status_code == 403 and "MODEL_ACCESS_DENIED" in refused.text
    assert client.get(f"/models/{model_id}").json().get("phaseInfo") is None
    for headers in (_RAPP, _INTERNAL, {}):
        assert client.patch(f"/models/{model_id}/phase-info", json={"phase": "TRAINED"}, headers=headers).status_code == 200
    open_model = _register_for(client, _INTERNAL)
    assert client.patch(f"/models/{open_model}/phase-info", json={"phase": "TRAINED"}, headers=_OTHER_RAPP).status_code == 200
    assert client.patch(f"/models/{uuid.uuid4()}/phase-info", json={"phase": "TRAINED"}, headers=_OTHER_RAPP).status_code == 404


def test_an_rapp_groups_only_models_it_owns_or_that_have_no_owner(client, db_session_factory):
    """SEC-15.3d: a coordination group naming another rApp's model is 403 MODEL_ACCESS_DENIED from an rApp and no group is stored; own models, owner-less models and ids MLMR does not know pass; internal and no role are not asked."""
    mine, mine2 = _register_for(client, _RAPP), _register_for(client, _RAPP)
    theirs, open_model = _register_for(client, _OTHER_RAPP), _register_for(client, _INTERNAL)
    resp = client.post("/coordination-groups", json={"memberModelIds": [mine, theirs]}, headers=_RAPP)
    assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    with db_session_factory() as db:
        assert db.query(MLModelCoordinationGroup).count() == 0
    for members in ([mine, mine2], [mine, open_model], [mine, str(uuid.uuid4())]):
        assert client.post("/coordination-groups", json={"memberModelIds": members}, headers=_RAPP).status_code == 201
    for headers in (_INTERNAL, {}):
        assert client.post("/coordination-groups", json={"memberModelIds": [mine, theirs]}, headers=headers).status_code == 201


def test_a_model_an_smo_module_registers_without_an_owner_stays_unowned(client):
    """An internal caller that names no owner, and a call with no role at all (a test, an in-process call), leave the owner unset: only an rApp is stamped, and a module acting on behalf of an rApp is not."""
    assert _registered_owner(client, _INTERNAL) is None
    assert _registered_owner(client, {**_INTERNAL, "X-R1-On-Behalf-Of": "es-client"}) is None
    assert _registered_owner(client, {}) is None


def test_register_model_without_metadata_defaults_to_empty(client):
    """Every registration metadata field is optional; unset ones read back as None or an empty list."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]

    view = client.get(f"/models/{model_id}").json()
    assert view["description"] is None
    assert view["author"] is None
    assert view["targetEnvironments"] == []


def test_update_model_changes_registration_metadata(client):
    """A PUT with the same identity replaces the registration metadata."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]

    resp = client.put(f"/models/{model_id}", json={
        "modelType": "coverage-predictor", "version": "1.0", "description": "updated description",
        "author": "team-ran", "targetEnvironments": [{"platformName": "k8s-cluster-2", "environmentType": "STAGING", "dependencyList": ""}],
    })
    assert resp.status_code == 200
    assert resp.json()["description"] == "updated description"
    assert resp.json()["author"] == "team-ran"
    assert resp.json()["targetEnvironments"][0]["platformName"] == "k8s-cluster-2"


def test_register_model_rejects_duplicate_type_and_version(client):
    """A second registration of the same (modelType, version) is a 409 MODEL_ALREADY_REGISTERED and leaves exactly one row."""
    first = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"})
    assert first.status_code == 201

    resp = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "MODEL_ALREADY_REGISTERED"

    all_models = client.get("/models").json()["items"]
    assert len(all_models) == 1


def test_register_model_allows_a_different_version_of_the_same_type(client):
    """Uniqueness is on the pair, so a new version of an existing type registers."""
    client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"})
    resp = client.post("/models", json={"modelType": "coverage-predictor", "version": "2.0"})
    assert resp.status_code == 201


def test_register_model_allows_the_same_version_of_a_different_type(client):
    """Uniqueness is on the pair, so the same version string under another type registers."""
    client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"})
    resp = client.post("/models", json={"modelType": "throughput-predictor", "version": "1.0"})
    assert resp.status_code == 201


def test_get_unknown_model_is_404(client):
    """Reading an id that was never registered is a 404."""
    resp = client.get(f"/models/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------- Wave 3: TS29482_MLR_MLModelManagement.yaml

def test_register_model_rejects_unknown_domain(client):
    """A domain outside the TS 29.482 enum is refused on register with 422 SCHEMA_VALIDATION_FAILED."""
    resp = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0", "domain": "TELEPATHY"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"


def test_register_model_stores_and_exposes_domain_and_vendors(client):
    """domain, customDomain and vendors are stored and returned."""
    model_id = client.post("/models", json={
        "modelType": "coverage-predictor", "version": "1.0", "domain": "CUSTOM",
        "customDomain": "coverage-optimization", "vendors": ["acme", "globex"],
    }).json()["modelId"]

    view = client.get(f"/models/{model_id}").json()
    assert view["domain"] == "CUSTOM"
    assert view["customDomain"] == "coverage-optimization"
    assert view["vendors"] == ["acme", "globex"]


def test_register_model_without_domain_defaults_to_empty(client):
    """An omitted domain reads back as None and omitted vendors as an empty list."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]
    view = client.get(f"/models/{model_id}").json()
    assert view["domain"] is None
    assert view["customDomain"] is None
    assert view["vendors"] == []


def test_update_model_rejects_unknown_domain(client):
    """The domain check also applies on PUT."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]
    resp = client.put(f"/models/{model_id}", json={"modelType": "coverage-predictor", "version": "1.0", "domain": "TELEPATHY"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"


def test_update_model_changes_domain_and_vendors(client):
    """A PUT can change domain and vendors."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]
    resp = client.put(f"/models/{model_id}", json={
        "modelType": "coverage-predictor", "version": "1.0", "domain": "IMAGE_RECOGNITION", "vendors": ["acme"],
    })
    assert resp.status_code == 200
    assert resp.json()["domain"] == "IMAGE_RECOGNITION"
    assert resp.json()["vendors"] == ["acme"]


def test_upload_model_artifact_records_size_bytes(client):
    """The upload answer reports the size of the bytes actually stored."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]
    resp = client.post(f"/models/{model_id}/artifact", files={"file": ("model.zip", b"twelve-bytes", "application/zip")})
    assert resp.status_code == 201
    assert resp.json()["sizeBytes"] == len(b"twelve-bytes")


def test_upload_model_artifact_stamps_version_one_and_records_location(client):
    """The first upload is artifact version 1 and sets the model's artifactLocation."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]

    resp = client.post(f"/models/{model_id}/artifact", files={"file": ("model.zip", b"pkzip-bytes", "application/zip")})
    assert resp.status_code == 201
    body = resp.json()
    assert body["modelId"] == model_id
    assert body["artifactVersion"] == 1

    view = client.get(f"/models/{model_id}").json()
    assert view["artifactLocation"] == f"model-artifact:{model_id}:1"


def test_upload_model_artifact_versions_increment_independently_of_model_version(client):
    """A second upload becomes artifact version 2 and does not change the model's own version."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]
    client.post(f"/models/{model_id}/artifact", files={"file": ("model.zip", b"first", "application/zip")})

    second = client.post(f"/models/{model_id}/artifact", files={"file": ("model.zip", b"second", "application/zip")})
    assert second.json()["artifactVersion"] == 2

    unchanged = client.get(f"/models/{model_id}").json()
    assert unchanged["version"] == "1.0"


def test_upload_model_artifact_rejects_non_zip(client):
    """A file name that does not end in .zip is refused (415)."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]

    resp = client.post(f"/models/{model_id}/artifact", files={"file": ("model.tar", b"not-a-zip", "application/x-tar")})
    assert resp.status_code == 415


def test_upload_model_artifact_for_unknown_model_is_404(client):
    """Uploading for a model that does not exist is a 404, not an orphan artifact."""
    resp = client.post(f"/models/{uuid.uuid4()}/artifact", files={"file": ("model.zip", b"bytes", "application/zip")})
    assert resp.status_code == 404


def test_download_model_artifact_round_trips_the_uploaded_bytes(client):
    """Download returns exactly the uploaded bytes, as application/zip."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]
    client.post(f"/models/{model_id}/artifact", files={"file": ("model.zip", b"pkzip-bytes", "application/zip")})

    resp = client.get(f"/models/{model_id}/artifact/1")
    assert resp.status_code == 200
    assert resp.content == b"pkzip-bytes"
    assert resp.headers["content-type"] == "application/zip"


def test_download_unknown_artifact_version_is_404(client):
    """Asking for an artifact version that was never uploaded is a 404."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]
    resp = client.get(f"/models/{model_id}/artifact/1")
    assert resp.status_code == 404


def test_update_model_changes_metadata_fields(client):
    """A PUT with the same identity is accepted when it also carries the write-only fields (resource type, lineage, hash) and
    the visible description changes.
    """
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0", "requiredResourceTypeId": "gpu-a"}).json()["modelId"]

    resp = client.put(f"/models/{model_id}", json={
        "modelType": "coverage-predictor", "version": "1.0", "requiredResourceTypeId": "gpu-b",
        "trainingDataLineage": {"source": "dme-type-1"}, "integrityHash": "sha256:abc",
        "description": "updated via metadata test",
    })
    assert resp.status_code == 200
    assert resp.json()["description"] == "updated via metadata test"

    view = client.get(f"/models/{model_id}").json()
    assert view["description"] == "updated via metadata test"


def test_update_model_rejects_changing_its_identity(client):
    """A PUT that changes modelType or version is a 400 MODEL_IDENTITY_IMMUTABLE and changes nothing."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]

    resp = client.put(f"/models/{model_id}", json={"modelType": "coverage-predictor", "version": "2.0"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["title"] == "MODEL_IDENTITY_IMMUTABLE"

    unchanged = client.get(f"/models/{model_id}").json()
    assert unchanged["version"] == "1.0"


def test_update_unknown_model_is_404(client):
    """A PUT to an unknown id is a 404."""
    resp = client.put(f"/models/{uuid.uuid4()}", json={"modelType": "t", "version": "1.0"})
    assert resp.status_code == 404


def test_delete_model_removes_it(client):
    """Deleting a model answers 204 and the model is gone afterwards."""
    model_id = client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"]

    resp = client.delete(f"/models/{model_id}")
    assert resp.status_code == 204
    assert client.get(f"/models/{model_id}").status_code == 404


def test_delete_unknown_model_is_idempotent(client):
    """Deleting an unknown id is 204, not an error."""
    resp = client.delete(f"/models/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_delete_model_cascades_its_own_artifacts(client):
    """Deleting a model also removes its artifacts. Only MLMR's own table is checked; the cascades into other modules' tables are
    foreign keys and are not enforced on SQLite.
    """
    model_id = uuid.UUID(client.post("/models", json={"modelType": "coverage-predictor", "version": "1.0"}).json()["modelId"])
    client.post(f"/models/{model_id}/artifact", files={"file": ("model.zip", b"bytes", "application/zip")})

    resp = client.delete(f"/models/{model_id}")
    assert resp.status_code == 204
    assert client.get(f"/models/{model_id}/artifact/1").status_code == 404


def test_list_coordination_groups_returns_members(client, db_session_factory):
    """A created group is listed with its member ids in order."""
    model_id_1 = _make_model(db_session_factory, model_type="t1")
    model_id_2 = _make_model(db_session_factory, model_type="t2")
    group_id = client.post("/coordination-groups", json={"memberModelIds": [str(model_id_1), str(model_id_2)]}).json()["groupId"]

    groups = client.get("/coordination-groups").json()["items"]
    assert [(g["groupId"], g["memberModelIds"]) for g in groups] == [(group_id, [str(model_id_1), str(model_id_2)])]


def test_a_repeated_coordination_group_with_the_same_key_creates_one_group(client, db_session_factory):
    """SDK-RETRY-IDEMPOTENCY: an SDK retry of POST /coordination-groups gets the first groupId back instead of a second group over the same members
    (a breach would then trigger the group retrain twice); the same key with another body is 422 IDEMPOTENCY_KEY_REUSED and stores nothing."""
    members = [str(_make_model(db_session_factory, model_type=t)) for t in ("t1", "t2", "t3")]
    first = client.post("/coordination-groups", json={"memberModelIds": members[:2]}, headers={"Idempotency-Key": "grp-1"})
    again = client.post("/coordination-groups", json={"memberModelIds": members[:2]}, headers={"Idempotency-Key": "grp-1"})
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    reused = client.post("/coordination-groups", json={"memberModelIds": members[1:]}, headers={"Idempotency-Key": "grp-1"})
    assert reused.status_code == 422 and reused.json()["detail"]["title"] == "IDEMPOTENCY_KEY_REUSED"
    with db_session_factory() as session:
        assert session.query(MLModelCoordinationGroup).count() == 1


def test_create_coordination_group_rejects_fewer_than_two_members(client, db_session_factory):
    """A group of one or no members is a 422 (COORDINATION_GROUP_TOO_SMALL for one); the SQLite schema has no CHECK, so the route must catch it."""
    model_id = _make_model(db_session_factory)
    resp = client.post("/coordination-groups", json={"memberModelIds": [str(model_id)]})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "COORDINATION_GROUP_TOO_SMALL"

    resp = client.post("/coordination-groups", json={"memberModelIds": []})
    assert resp.status_code == 422


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """`/health` answers 200 `{status: healthy}`, which the GUI BFF's module status probe relies on."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


# ---------------------------------------------------------------- Wave 4: TS 28.105 MLModel / MLModelRepository / MLModelCoordinationGroup

def _fake_aimgf_refs(monkeypatch, payload=None, status=200):
    """Replaces `R1Client.get` so the AIMgF `nrm-refs` call returns `payload` with `status`; returns the list of paths called."""
    class Resp:
        status_code = status

        def json(self):
            return payload

    calls = []

    def fake_get(self, path, **kw):
        calls.append(path)
        return Resp()

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    return calls


def test_register_with_ts28105_attributes_and_nrm_view(client, monkeypatch):
    """TS 28.105 attributes registered on a model come back in the `/ml-models/{id}` view together with AIMgF's read-only refs,
    and the model appears in its repository.
    """
    repo = client.post("/ml-model-repositories", json={"userLabel": "mlmr-1"}).json()
    resp = client.post("/models", json={
        "modelType": "energy-saving", "version": "1.0", "aIMLInferenceName": "NG_RAN_NETWORK_ENERGY_SAVING",
        "trainingContext": {"dataProviderRef": ["PRB_UTILIZATION"]},
        "supportedPerformanceIndicators": [{"performanceIndicatorName": "MAE", "isSupportedForTraining": True}],
        "mLCapabilitiesInfoList": [{"capabilityName": "sleep-recommendation"}],
        "inferenceScope": ["NG_RAN_NETWORK_ENERGY_SAVING"], "mLModelRepositoryRef": repo["id"],
    })
    model_id = resp.json()["modelId"]
    calls = _fake_aimgf_refs(monkeypatch, {"mLTrainingType": "INITIAL_TRAINING", "aIMLInferenceReportRefList": ["r1"],
                                           "usedByFunctionRefList": ["f1"]})
    attrs = client.get(f"/ml-models/{model_id}").json()["attributes"]
    assert calls == [f"/aimgf/ml-models/{model_id}/nrm-refs"]
    assert attrs["mLModelId"] == model_id and attrs["mLModelVersion"] == "1.0"
    assert attrs["aIMLInferenceName"] == "NG_RAN_NETWORK_ENERGY_SAVING"
    assert attrs["trainingContext"] == {"dataProviderRef": ["PRB_UTILIZATION"]}
    assert attrs["supportedPerformanceIndicators"][0]["isSupportedForTesting"] is False
    assert (attrs["mLTrainingType"], attrs["usedByFunctionRefList"]) == ("INITIAL_TRAINING", ["f1"])
    assert client.get(f"/ml-model-repositories/{repo['id']}").json()["MLModel"] == [model_id]
    # this build's own view carries the writable spec attributes too
    assert client.get(f"/models/{model_id}").json()["mLModelRepositoryRef"] == repo["id"]


def test_nrm_view_degrades_when_aimgf_unreachable(client, monkeypatch):
    """When AIMgF does not answer 200, the NRM view still returns, with the AIMgF-owned attributes empty."""
    model_id = client.post("/models", json={"modelType": "m", "version": "1"}).json()["modelId"]
    _fake_aimgf_refs(monkeypatch, None, status=503)
    attrs = client.get(f"/ml-models/{model_id}").json()["attributes"]
    assert attrs["mLTrainingType"] is None and attrs["aIMLInferenceReportRefList"] == []


def test_ts28105_attributes_are_validated(client):
    """Empty required lists, unknown nested fields and references to a repository or source model that do not exist are refused (422 and 404)."""
    assert client.post("/models", json={"modelType": "m", "version": "1",
                                        "supportedPerformanceIndicators": []}).status_code == 422
    assert client.post("/models", json={"modelType": "m", "version": "1",
                                        "trainingContext": {"notInSpec": 1}}).status_code == 422
    assert client.post("/models", json={"modelType": "m", "version": "1",
                                        "mLModelRepositoryRef": str(uuid.uuid4())}).status_code == 404
    assert client.post("/models", json={"modelType": "m", "version": "1",
                                        "sourceTrainedMLModelRef": str(uuid.uuid4())}).status_code == 404


def test_source_trained_model_ref_and_update(client):
    """A model can name the model it was trained from, and a PUT can set a TS 28.105 attribute."""
    base = client.post("/models", json={"modelType": "m", "version": "1"}).json()["modelId"]
    derived = client.post("/models", json={"modelType": "m", "version": "2", "sourceTrainedMLModelRef": base}).json()["modelId"]
    assert client.get(f"/models/{derived}").json()["sourceTrainedMLModelRef"] == base
    resp = client.put(f"/models/{derived}", json={"modelType": "m", "version": "2", "aIMLInferenceName": "X"})
    assert resp.json()["aIMLInferenceName"] == "X"


def test_coordination_group_nrm_view_and_repository_delete_uncontains(client):
    """Deleting a repository does not delete its models or groups; they become uncontained."""
    repo = client.post("/ml-model-repositories", json={}).json()
    a = client.post("/models", json={"modelType": "a", "version": "1", "mLModelRepositoryRef": repo["id"]}).json()["modelId"]
    b = client.post("/models", json={"modelType": "b", "version": "1"}).json()["modelId"]
    group_id = client.post("/coordination-groups", json={"memberModelIds": [a, b], "mLModelRepositoryRef": repo["id"]}).json()["groupId"]
    view = client.get(f"/ml-model-coordination-groups/{group_id}").json()
    assert view["attributes"]["memberMLModelRefList"] == [a, b]
    assert client.get(f"/ml-model-repositories/{repo['id']}").json()["MLModelCoordinationGroup"] == [group_id]
    assert client.delete(f"/ml-model-repositories/{repo['id']}").status_code == 204
    assert client.get(f"/models/{a}").json()["mLModelRepositoryRef"] is None
    assert client.get(f"/ml-model-coordination-groups/{group_id}").json()["attributes"]["mLModelRepositoryRef"] is None
    assert client.get(f"/ml-model-repositories/{repo['id']}").status_code == 404


_ACTING = {**_INTERNAL, "X-R1-Acting-User": "alice"}


def _audit_rows(db_session_factory) -> list:
    """Every row of the audit chain, oldest first."""
    with db_session_factory() as db:
        return list(db.scalars(select(audit.AuditEntry).order_by(audit.AuditEntry.seq)))


def test_an_operator_gives_an_owner_less_model_an_owner_and_it_is_audited(client, db_session_factory):
    """SEC-15.3c part 3: PUT /models/{id}/owner from role internal stores the owner, answers the old and new one, and writes one audit row naming the person, the model and both owners."""
    model_id = _register_for(client, _INTERNAL)
    resp = client.put(f"/models/{model_id}/owner", json={"owner": "es-client"}, headers=_ACTING)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"modelId": model_id, "owner": "es-client", "previousOwner": None, "changed": True}
    assert client.get(f"/models/{model_id}").json()["owner"] == "es-client"
    (row,) = _audit_rows(db_session_factory)
    assert (row.actor, row.actor_role, row.action, row.target) == ("alice", "internal", "SET_MODEL_OWNER", f"/mlmr/models/{model_id}/owner")
    assert row.detail == {"modelId": model_id, "previousOwner": None, "newOwner": "es-client"}
    with db_session_factory() as db:
        assert audit.verify(db) is None


def test_an_operator_may_change_an_existing_owner_and_the_old_one_is_audited(client, db_session_factory):
    """A model that has an owner can be given another by the operator; the audit row keeps the old owner; without an acting user the caller's invoker id is the actor."""
    model_id = _register_for(client, _RAPP)
    resp = client.put(f"/models/{model_id}/owner", json={"owner": "mobility-client"}, headers=_INTERNAL)
    assert resp.status_code == 200 and resp.json()["previousOwner"] == "es-client" and resp.json()["changed"] is True
    (row,) = _audit_rows(db_session_factory)
    assert row.actor == "smo-aimgf" and row.detail["previousOwner"] == "es-client" and row.detail["newOwner"] == "mobility-client"
    assert client.put(f"/models/{model_id}/owner", json={"owner": "x"}, headers={}).status_code == 200
    assert _audit_rows(db_session_factory)[-1].actor == "unknown"


def test_setting_the_same_owner_again_changes_and_audits_nothing(client, db_session_factory):
    """Idempotent: the owner already stored is 200 with changed false and no audit row is added."""
    model_id = _register_for(client, _RAPP)
    resp = client.put(f"/models/{model_id}/owner", json={"owner": "es-client"}, headers=_INTERNAL)
    assert resp.status_code == 200 and resp.json()["changed"] is False
    assert _audit_rows(db_session_factory) == []


def test_an_rapp_cannot_set_the_owner_of_any_model_even_its_own(client, db_session_factory):
    """An rApp is refused 403 ROLE_NOT_PERMITTED, for an owned, an owner-less and an unknown model alike, and nothing is changed or audited."""
    open_model = _register_for(client, _INTERNAL)
    own_model = _register_for(client, _RAPP)
    for model_id in (open_model, own_model, str(uuid.uuid4())):
        resp = client.put(f"/models/{model_id}/owner", json={"owner": "es-client"}, headers=_OTHER_RAPP)
        assert resp.status_code == 403 and "ROLE_NOT_PERMITTED" in resp.text
    assert client.get(f"/models/{open_model}").json()["owner"] is None
    assert client.get(f"/models/{own_model}").json()["owner"] == "es-client"
    assert _audit_rows(db_session_factory) == []


def test_setting_the_owner_of_an_unknown_model_is_404(client):
    """An id MLMR does not know is 404 MODEL_NOT_FOUND."""
    resp = client.put(f"/models/{uuid.uuid4()}/owner", json={"owner": "es-client"}, headers=_INTERNAL)
    assert resp.status_code == 404 and "MODEL_NOT_FOUND" in resp.text


def test_the_owner_must_be_one_token_and_clearing_is_not_offered(client, db_session_factory):
    """An empty owner, one with a space or a control character, one over 255 characters and an extra field are 422, and the stored owner is kept."""
    model_id = _register_for(client, _RAPP)
    for body in ({"owner": ""}, {"owner": "two words"}, {"owner": "tab\there"}, {"owner": "x" * 256}, {"owner": None}, {}, {"owner": "ok", "extra": 1}):
        assert client.put(f"/models/{model_id}/owner", json=body, headers=_INTERNAL).status_code == 422, body
    assert client.put(f"/models/{model_id}/owner", json={"owner": "x" * 255}, headers=_INTERNAL).status_code == 200
    assert client.get(f"/models/{model_id}").json()["owner"] == "x" * 255
    assert len(_audit_rows(db_session_factory)) == 1


def test_once_an_operator_gives_the_owner_the_owner_rule_applies(client):
    """The point of the route: after the operator names an owner for a legacy model, another rApp is refused on it and the named one is let through."""
    model_id = _register_for(client, _INTERNAL)
    assert client.put(f"/models/{model_id}", json=_put_body(client, model_id, description="open"), headers=_OTHER_RAPP).status_code == 200
    client.put(f"/models/{model_id}/owner", json={"owner": "es-client"}, headers=_INTERNAL)
    assert client.put(f"/models/{model_id}", json=_put_body(client, model_id, description="x"), headers=_OTHER_RAPP).status_code == 403
    assert client.put(f"/models/{model_id}", json=_put_body(client, model_id, description="x"), headers=_RAPP).status_code == 200


def test_a_put_that_changes_the_owner_is_audited_and_one_that_keeps_it_is_not(client, db_session_factory):
    """The other way an operator can change the owner (a full PUT as role internal) writes the same audit row, including a clear; a PUT that leaves the owner as it is writes none."""
    model_id = _register_for(client, _RAPP)
    client.put(f"/models/{model_id}", json=_put_body(client, model_id, description="same", owner="es-client"), headers=_ACTING)
    assert _audit_rows(db_session_factory) == []
    client.put(f"/models/{model_id}", json=_put_body(client, model_id, owner="mobility-client"), headers=_ACTING)
    client.put(f"/models/{model_id}", json=_put_body(client, model_id), headers=_ACTING)
    first, second = _audit_rows(db_session_factory)
    assert (first.actor, first.detail["previousOwner"], first.detail["newOwner"]) == ("alice", "es-client", "mobility-client")
    assert (second.detail["previousOwner"], second.detail["newOwner"]) == ("mobility-client", None)


# ---------------------------------------------------------------- call flow 26e (DOC-CF-1.7): a model that still serves is not deleted

# Each row is AIMgF's runtime state for the model and whether the delete goes through.
@pytest.mark.parametrize("state,deleted", [(None, True), ("NOT_DEPLOYED", True), ("TERMINATED", True),
                                           ("DEPLOYED", False), ("ACTIVE", False), ("SCALING", False)])
def test_a_model_with_a_live_runtime_is_not_deleted(client, aimgf_runtimes, state, deleted):
    """A model whose serving runtime AIMgF has deployed is 409 `MODEL_IN_USE` and kept; one AIMgF does not know, never deployed or terminated is deleted."""
    model_id = client.post("/models", json={"modelType": "energy-saving", "version": "1.0"}).json()["modelId"]
    if state is not None:
        aimgf_runtimes[model_id] = state
    resp = client.delete(f"/models/{model_id}")
    assert resp.status_code == (204 if deleted else 409)
    assert (client.get(f"/models/{model_id}").status_code == 404) is deleted
    if not deleted:
        assert resp.json()["detail"]["title"] == "MODEL_IN_USE" and state in resp.json()["detail"]["detail"]


# Each row is how AIMgF fails to answer: a transport error, or a status that is neither 200 nor 404.
@pytest.mark.parametrize("failure", ["transport", 500])
def test_a_model_is_not_deleted_when_aimgf_cannot_say(client, monkeypatch, failure):
    """No answer from AIMgF, or a 5xx, is 503 `ENDPOINT_UNREACHABLE` and nothing is deleted: a delete on a guess could orphan a running workload."""
    model_id = client.post("/models", json={"modelType": "energy-saving", "version": "1.0"}).json()["modelId"]

    def failing_get(self, path, **kw):
        """AIMgF fails as the row says."""
        if failure == "transport":
            raise ConnectionError("down")
        return type("A", (), {"status_code": failure, "json": lambda self: {}})()
    monkeypatch.setattr("app.main.R1Client.get", failing_get)

    assert client.delete(f"/models/{model_id}").status_code == 503
    assert client.get(f"/models/{model_id}").status_code == 200


def test_the_live_runtime_read_is_made_on_mlmrs_own_account(client, monkeypatch):
    """MLMR asks AIMgF for the runtime for nobody (no `X-R1-On-Behalf-Of`), also when an rApp deletes its model: the route judged ownership already, and AIMgF's
    owner rule (MLLF-NODEGROUP-OWNER) must not turn the safety read into a refusal that reads as "AIMgF did not answer"."""
    from smo_shared.invoker import get_originator
    model_id = _register_for(client, _RAPP)
    originators = []

    def recording_get(self, path, **kw):
        """AIMgF does not know the model (404), and the originator the call would carry is recorded."""
        originators.append(get_originator())
        return type("A", (), {"status_code": 404, "json": lambda self: {}})()
    monkeypatch.setattr("app.main.R1Client.get", recording_get)

    assert client.delete(f"/models/{model_id}", headers=_RAPP).status_code == 204
    assert originators == [None]
