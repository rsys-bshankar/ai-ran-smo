"""Tests of the TS 29.482 parts of the model registry (`app/registry/mlr.py`, MLMR's until S2.1): the MLModel attributes, usage requirements (SA-MLMR-8), phase and lineage
(SA-MLMR-7), storeDiscReqs enforcement (SA-MLMR-6), whole-object discovery (SA-MLMR-9) and storages with profiles (SA-MLMR-1).

Run: `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_registry_mlr_conformance.py -q`. Uses the `client` and
`db_session_factory` fixtures of `test_registry.py` (SQLite, no Postgres). Caller identity is simulated with the
`X-R1-Invoker-Id` header that R1 Termination would forward.
"""

import base64
import datetime
import json
import uuid

import pytest

from test_registry import client, db_session_factory  # noqa: F401  (pytest fixtures)

from app.registry.models import MLModel
from app.registry.mlr import subset_match

HDR = "X-R1-Invoker-Id"


def _register(client, model_type="m", version="1", **kw) -> str:
    """Registers a model through the API with extra fields in `kw`, asserts 201 and returns its id."""
    r = client.post("/models", json={"modelType": model_type, "version": version, **kw})
    assert r.status_code == 201, r.text
    return r.json()["modelId"]


def _upload(client, model_id, content=b"PK-model"):
    """Uploads `content` as a .zip artifact of the model and returns the response."""
    return client.post(f"/models/{model_id}/artifact", files={"file": ("m.zip", content, "application/zip")})


def _discover(client, criteria, headers=None, **params):
    """Calls the discovery form of `GET /models` with `criteria` as `filt-criteria` JSON."""
    return client.get("/models", params={"filt-criteria": json.dumps(criteria), **params}, headers=headers or {})


# ---------------------------------------------------------------- SA-MLMR-8 usage

def test_usage_reqs_are_stored_validated_and_returned(client):
    """usageReqs round-trips, and an unknown usage or an empty list is a 422."""
    model_id = _register(client, usageReqs={"usages": ["TRAINING", "INFERENCE"]})
    assert client.get(f"/models/{model_id}").json()["usageReqs"] == {"usages": ["TRAINING", "INFERENCE"]}
    assert client.post("/models", json={"modelType": "x", "version": "1", "usageReqs": {"usages": ["DEPLOY"]}}).status_code == 422
    assert client.post("/models", json={"modelType": "x", "version": "1", "usageReqs": {"usages": []}}).status_code == 422


def test_an_update_keeps_spec_attributes_it_does_not_send(client):
    """A PUT that omits TS 29.482 attributes keeps them, so AIMgF's phaseInfo cannot be erased by a metadata update; one that
    sends them replaces them.
    """
    model_id = _register(client, usageReqs={"usages": ["INFERENCE"]}, interopInfo="onnx-1.15")
    client.put(f"/models/{model_id}", json={"modelType": "m", "version": "1", "description": "new"})
    body = client.get(f"/models/{model_id}").json()
    assert body["description"] == "new" and body["interopInfo"] == "onnx-1.15" and body["usageReqs"] == {"usages": ["INFERENCE"]}
    client.put(f"/models/{model_id}", json={"modelType": "m", "version": "1", "usageReqs": {"usages": ["TRAINING"]}})
    assert client.get(f"/models/{model_id}").json()["usageReqs"] == {"usages": ["TRAINING"]}


# ---------------------------------------------------------------- SA-MLMR-7 phase / lineage

def test_phase_info_is_declared_validated_and_returned(client):
    """phaseInfo with training lineage round-trips and malformed phaseInfo variants are refused with 422."""
    info = {"phase": "TRAINED", "trainingInfo": {"baseModelId": "base-1", "dataSources": "dme:cells", "dataVolume": 1024, "freshness": "DAILY"},
            "observedPerf": {"resourceAddr": 40}, "contTrainInd": True, "contTrainParams": "lr=0.1"}
    model_id = _register(client, phaseInfo=info)
    assert client.get(f"/models/{model_id}").json()["phaseInfo"] == info
    for bad in ({"phase": "BAKED"}, {"phase": "TRAINED", "trainingInfo": {}}, {"phase": "TRAINED", "trainingInfo": {"freshness": "YEARLY"}},
                {"phase": "TRAINED", "observedPerf": {"resourceAddr": 101}}, {"trainingInfo": {"baseModelId": "x"}}):
        assert client.post("/models", json={"modelType": "b", "version": "1", "phaseInfo": bad}).status_code == 422


def test_phase_info_patch_merges_and_the_first_write_needs_a_phase(client):
    """The first phase-info write needs a phase, later writes merge (trainingInfo key by key), and unknown models and fields are 404 and 422."""
    model_id = _register(client)
    assert client.patch(f"/models/{model_id}/phase-info", json={"trainingInfo": {"baseModelId": "b"}}).status_code == 422
    first = client.patch(f"/models/{model_id}/phase-info", json={"phase": "IN_TRAINING", "trainingInfo": {"dataSources": "s3://x"}})
    assert first.json()["phaseInfo"] == {"phase": "IN_TRAINING", "trainingInfo": {"dataSources": "s3://x"}}
    again = client.patch(f"/models/{model_id}/phase-info", json={"phase": "IN_RETRAINING", "trainingInfo": {"baseModelId": model_id}})
    assert again.json()["phaseInfo"] == {"phase": "IN_RETRAINING", "trainingInfo": {"dataSources": "s3://x", "baseModelId": model_id}}
    assert client.patch(f"/models/{uuid.uuid4()}/phase-info", json={"phase": "TRAINED"}).status_code == 404
    assert client.patch(f"/models/{model_id}/phase-info", json={"phase": "TRAINED", "bogus": 1}).status_code == 422


# ---------------------------------------------------------------- SA-MLMR-6 storeDiscReqs

def test_store_disc_reqs_validation(client):
    """storeDiscReqs accepts a valid duration and access requirement and refuses empty, unknown, negative and incomplete ones."""
    ok = {"duration": 3600, "accessReqs": {"accessReq": "RESTRICTED", "valClientIds": ["a"], "timePeriod": 60}}
    assert client.post("/models", json={"modelType": "ok", "version": "1", "storeDiscReqs": ok}).status_code == 201
    for bad in ({}, {"accessReqs": {"accessReq": "SECRET"}}, {"duration": -1}, {"accessReqs": {"valClientIds": ["a"]}}):
        assert client.post("/models", json={"modelType": "bad", "version": "1", "storeDiscReqs": bad}).status_code == 422


def test_a_private_model_downloads_only_for_its_owner(client):
    """PRIVATE_USE_ONLY lets only the owner download; another caller, or none, gets 403 MODEL_ACCESS_DENIED."""
    model_id = _register(client, owner="rapp-owner", storeDiscReqs={"accessReqs": {"accessReq": "PRIVATE_USE_ONLY"}})
    _upload(client, model_id)
    url = f"/models/{model_id}/artifact/1"
    assert client.get(url, headers={HDR: "rapp-owner"}).status_code == 200
    denied = client.get(url, headers={HDR: "other"})
    assert denied.status_code == 403 and denied.json()["detail"]["title"] == "MODEL_ACCESS_DENIED"
    assert client.get(url).status_code == 403  # no caller id at all


def test_a_restricted_model_downloads_for_the_owner_and_named_ids(client):
    """RESTRICTED admits the owner and the ids in valServerIds / valClientIds, and refuses anyone else."""
    model_id = _register(client, owner="o", storeDiscReqs={"accessReqs": {"accessReq": "RESTRICTED", "valServerIds": ["srv"], "valClientIds": ["cli"]}})
    _upload(client, model_id)
    url = f"/models/{model_id}/artifact/1"
    assert [client.get(url, headers={HDR: who}).status_code for who in ("o", "srv", "cli", "stranger")] == [200, 200, 200, 403]


def test_a_public_or_unrestricted_model_is_open(client):
    """A PUBLICLY_AVAILABLE model, and a model with no access requirement, can be downloaded by anyone."""
    public = _register(client, model_type="p", storeDiscReqs={"accessReqs": {"accessReq": "PUBLICLY_AVAILABLE"}})
    plain = _register(client, model_type="q")
    for model_id in (public, plain):
        _upload(client, model_id)
        assert client.get(f"/models/{model_id}/artifact/1").status_code == 200


def _age(db_session_factory, model_id, seconds):
    """Moves a model's `registered_at` back by `seconds`, so expiry can be tested without waiting."""
    with db_session_factory() as session:
        session.get(MLModel, uuid.UUID(model_id)).registered_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=seconds)
        session.commit()


def test_duration_expires_download_and_discovery_but_not_the_record(client, db_session_factory):
    """Past its duration a model is 410 on download and absent from discovery, but the registry record is still readable."""
    model_id = _register(client, storeDiscReqs={"duration": 100})
    _upload(client, model_id)
    assert client.get(f"/models/{model_id}/artifact/1").status_code == 200 and _discover(client, {"mlModelId": model_id}).status_code == 200
    _age(db_session_factory, model_id, 500)
    expired = client.get(f"/models/{model_id}/artifact/1")
    assert expired.status_code == 410 and expired.json()["detail"]["title"] == "MODEL_EXPIRED"
    assert _discover(client, {"mlModelId": model_id}).status_code == 404
    assert client.get(f"/models/{model_id}").status_code == 200  # the registry record stays


def test_the_access_time_period_closes_access(client, db_session_factory):
    """Past `accessReqs.timePeriod` access is refused (403) even for a publicly available model."""
    model_id = _register(client, storeDiscReqs={"accessReqs": {"accessReq": "PUBLICLY_AVAILABLE", "timePeriod": 100}})
    _upload(client, model_id)
    assert client.get(f"/models/{model_id}/artifact/1").status_code == 200
    _age(db_session_factory, model_id, 500)
    assert client.get(f"/models/{model_id}/artifact/1").status_code == 403


# ---------------------------------------------------------------- SA-MLMR-9 discovery

def test_subset_match_rules():
    """Scalars must be equal, a criteria list must be contained in the record list, objects match recursively, and a missing key never matches."""
    assert subset_match({"a": 1}, {"a": 1, "b": 2}) and not subset_match({"a": 2}, {"a": 1})
    assert subset_match({"v": ["x"]}, {"v": ["x", "y"]}) and not subset_match({"v": ["z"]}, {"v": ["x"]})
    assert subset_match({"p": {"q": 1}}, {"p": {"q": 1, "r": 2}}) and not subset_match({"p": {"q": 1}}, {"p": 5})
    assert not subset_match({"missing": 1}, {"a": 1})


def test_discovery_matches_the_whole_mlmodel_object(client):
    """Discovery filters on any combination of MLModel attributes (domain, vendors, usage, phase, nested training info) and an
    empty criteria matches all.
    """
    a = _register(client, model_type="a", domain="IMAGE_RECOGNITION", vendors=["acme", "globex"], usageReqs={"usages": ["INFERENCE"]},
                  phaseInfo={"phase": "TRAINED", "trainingInfo": {"freshness": "DAILY"}})
    b = _register(client, model_type="b", domain="SPEECH_RECOGNITION", vendors=["acme"], usageReqs={"usages": ["TRAINING"]},
                  phaseInfo={"phase": "DEPLOYED"})
    ids = lambda criteria: {p["mlModelInfo"]["mlModelId"] for p in _discover(client, criteria).json()["profiles"]}  # noqa: E731
    assert ids({"domain": "IMAGE_RECOGNITION"}) == {a}
    assert ids({"vendors": ["acme"]}) == {a, b} and ids({"vendors": ["acme", "globex"]}) == {a}
    assert ids({"usageReqs": {"usages": ["TRAINING"]}}) == {b}
    assert ids({"phaseInfo": {"phase": "TRAINED"}}) == {a}
    assert ids({"phaseInfo": {"phase": "TRAINED", "trainingInfo": {"freshness": "DAILY"}}}) == {a}
    assert ids({"domain": "IMAGE_RECOGNITION", "vendors": ["globex"], "phaseInfo": {"phase": "TRAINED"}}) == {a}
    assert ids({}) == {a, b}


def test_discovery_404_when_nothing_matches_and_422_for_a_bad_criteria(client):
    """No match is 404; criteria that are not an MLModel (unknown domain, unknown field, invalid JSON) are 422."""
    _register(client)
    assert _discover(client, {"domain": "IMAGE_PROCESSING"}).status_code == 404
    assert _discover(client, {"domain": "NOT_A_DOMAIN"}).status_code == 422
    assert _discover(client, {"bogus": 1}).status_code == 422
    assert client.get("/models", params={"filt-criteria": "{not json"}).status_code == 422


def test_discovery_respects_access_requirements(client):
    """Discovery lists a private model only to its owner, and nothing private to a caller without an id."""
    open_model = _register(client, model_type="open")
    private = _register(client, model_type="priv", owner="me", storeDiscReqs={"accessReqs": {"accessReq": "PRIVATE_USE_ONLY"}})
    seen = lambda who: {p["mlModelInfo"]["mlModelId"] for p in _discover(client, {}, headers={HDR: who} if who else {}).json()["profiles"]}  # noqa: E731
    assert seen("someone") == {open_model} and seen("me") == {open_model, private} and seen(None) == {open_model}


def test_discovery_can_return_the_model_files(client):
    """With include-models the answer carries the latest artifact base64 encoded and `indicator` true; without it `indicator` is false."""
    model_id = _register(client)
    _upload(client, model_id, b"weights-v1")
    _upload(client, model_id, b"weights-v2")
    body = _discover(client, {"mlModelId": model_id}, **{"include-models": "true"}).json()
    assert body["indicator"] is True and "profiles" not in body
    assert body["mlModels"][0]["mlModelId"] == model_id and base64.b64decode(body["mlModels"][0]["mlModel"]) == b"weights-v2"
    assert _discover(client, {"mlModelId": model_id}).json()["indicator"] is False


def test_the_plain_list_is_unchanged_without_criteria(client):
    """Without `filt-criteria` `GET /models` is the plain list with its `model_type` filter and total."""
    _register(client, model_type="a")
    _register(client, model_type="b")
    assert client.get("/models").json()["total"] == 2 and client.get("/models", params={"model_type": "a"}).json()["total"] == 1


# ---------------------------------------------------------------- SA-MLMR-1 storages and profiles

def test_a_storage_holds_profiles_that_name_registered_models(client):
    """A storage keeps profiles that point at registered models, the profile's model info updates the model, and discovery returns the stored profile."""
    model_id = _register(client)
    _upload(client, model_id, b"abcd")
    body = {"mlModels": [{"aimleServId": "srv-1", "aimleRepId": "rep-1", "mlModelInfo": {"mlModelId": model_id, "domain": "LOCATION_PREDICTION", "vendors": ["acme"]},
                          "mlModelUri": {"uri": "https://store/models/1"}}],
            "mlModelsAddresses": [{"fqdn": "store.example.com"}], "suppFeat": "0"}
    storage = client.post("/storages", json=body)
    assert storage.status_code == 201, storage.text
    out = storage.json()
    profile = out["mlModels"][0]
    assert profile["aimleServId"] == "srv-1" and profile["mlModelUri"] == {"uri": "https://store/models/1"}
    assert profile["mlModelInfo"]["mlModelId"] == model_id and profile["mlModelInfo"]["mlModelSize"] == 4
    assert out["mlModelsAddresses"] == [{"fqdn": "store.example.com"}] and out["suppFeat"] == "0"
    # the profile's mlModelInfo updated the model record it names
    assert client.get(f"/models/{model_id}").json()["domain"] == "LOCATION_PREDICTION"
    assert client.get(f"/storages/{out['storageId']}").json() == out
    # a model's discovery profile is its stored one
    found = _discover(client, {"mlModelId": model_id}).json()["profiles"][0]
    assert found["mlModelProfId"] == profile["mlModelProfId"] and found["aimleRepId"] == "rep-1"


def test_storage_validation(client):
    """Storage bodies that lack a model or address, have two address kinds, name an unknown or malformed model id, or carry
    unknown fields are refused; an unknown storage is 404.
    """
    model_id = _register(client)
    assert client.post("/storages", json={}).status_code == 422  # needs mlModels or mlModelsAddresses
    assert client.post("/storages", json={"mlModelsAddresses": []}).status_code == 422
    assert client.post("/storages", json={"mlModelsAddresses": [{"uri": "u", "fqdn": "f"}]}).status_code == 422  # exactly one
    assert client.post("/storages", json={"mlModels": [{"mlModelInfo": {}}]}).status_code == 422  # mlModelId required
    assert client.post("/storages", json={"mlModels": [{"mlModelInfo": {"mlModelId": str(uuid.uuid4())}}]}).status_code == 404
    assert client.post("/storages", json={"mlModels": [{"mlModelInfo": {"mlModelId": "not-an-id"}}]}).status_code == 422
    assert client.post("/storages", json={"mlModels": [{"mlModelInfo": {"mlModelId": model_id}, "bogus": 1}]}).status_code == 422
    assert client.get(f"/storages/{uuid.uuid4()}").status_code == 404


def test_storage_list_filters_replace_patch_and_delete(client):
    """Storages can be listed by storage or profile id, replaced, patched (a present list replaces, an absent one is kept) and
    deleted twice without error; the models they named stay.
    """
    m1, m2 = _register(client, model_type="a"), _register(client, model_type="b")
    s1 = client.post("/storages", json={"mlModels": [{"mlModelInfo": {"mlModelId": m1}}]}).json()
    s2 = client.post("/storages", json={"mlModels": [{"mlModelInfo": {"mlModelId": m2}}], "mlModelsAddresses": [{"uri": "u"}]}).json()
    assert client.get("/storages").json()["total"] == 2
    assert [s["storageId"] for s in client.get("/storages", params={"storage-ids": s1["storageId"]}).json()["items"]] == [s1["storageId"]]
    wanted = s2["mlModels"][0]["mlModelProfId"]
    assert [s["storageId"] for s in client.get("/storages", params={"profile-ids": wanted}).json()["items"]] == [s2["storageId"]]
    assert client.get("/storages", params={"storage-ids": "nope"}).status_code == 422
    replaced = client.put(f"/storages/{s1['storageId']}", json={"mlModels": [{"mlModelInfo": {"mlModelId": m2}}]}).json()
    assert [p["mlModelInfo"]["mlModelId"] for p in replaced["mlModels"]] == [m2] and "mlModelsAddresses" not in replaced
    patched = client.patch(f"/storages/{s2['storageId']}", json={"mlModelsAddresses": [{"fqdn": "f"}]}).json()
    assert patched["mlModelsAddresses"] == [{"fqdn": "f"}] and len(patched["mlModels"]) == 1  # profiles kept
    assert client.patch(f"/storages/{s2['storageId']}", json={"mlModels": [{"mlModelInfo": {"mlModelId": m1}}]}).json()["mlModels"][0]["mlModelInfo"]["mlModelId"] == m1
    assert client.delete(f"/storages/{s2['storageId']}").status_code == 204 and client.delete(f"/storages/{s2['storageId']}").status_code == 204
    assert client.get(f"/storages/{s2['storageId']}").status_code == 404 and client.get(f"/models/{m1}").status_code == 200  # the model stays


def test_deregistering_a_model_removes_its_profiles(client):
    """Deleting a model removes the profiles that named it, so the storage no longer lists them."""
    model_id = _register(client)
    storage = client.post("/storages", json={"mlModels": [{"mlModelInfo": {"mlModelId": model_id}}], "mlModelsAddresses": [{"uri": "u"}]}).json()
    assert client.delete(f"/models/{model_id}").status_code == 204
    assert "mlModels" not in client.get(f"/storages/{storage['storageId']}").json()
