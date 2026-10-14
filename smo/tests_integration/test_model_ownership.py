"""SEC-15.3b, SEC-15.3c and SEC-15.3d (MLMR delete, artifact, phase-info and groups) through the mesh: the rApp that registers a model at MLMR owns it, and AIMgF refuses another rApp's scale, terminate, node-groups and model-loading calls on it
(SEC-15.3b), its training, validation, emulation and update requests, its lifecycle reads and its MLMF subscriptions (SEC-15.3c); MLMR keeps an rApp from setting or changing the owner (SEC-15.3c) and from deleting, uploading to, downloading from, phasing or grouping a model it does not own (SEC-15.3d).

The in-process mesh has no gateway, so the headers R1 Termination would stamp on the inbound call (`X-R1-Role`, `X-R1-Invoker-Id`) are sent by hand, as `test_on_behalf_of.py` does.
AIMgF's own onward call to MLMR goes through the mesh as an SMO module's. Run with `cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_model_ownership.py -q`.
"""

import uuid

ES = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-client"}
OTHER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "other-client"}
SMO = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-invoker"}


def _register(mesh, headers, **body) -> str:
    """Registers a model at MLMR with the given gateway headers and returns its id."""
    resp = mesh["mlmr"].post("/models", headers=headers, json={"modelType": "own-" + uuid.uuid4().hex[:8], "version": "1", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()["modelId"]


def test_the_registering_rapp_owns_the_model_and_another_rapp_is_refused(mesh):
    """A model an rApp registers carries its invoker id as `owner`; the owner places it (node-groups 200), another rApp is 403 `MODEL_ACCESS_DENIED` on node-groups, scale and
    terminate, and on a loading request for it (AIMgF reads the owner from MLMR over the mesh); the operator (role internal) is not refused.
    """
    model_id = _register(mesh, ES)
    assert mesh["mlmr"].get(f"/models/{model_id}").json()["owner"] == "es-client"

    patch = {"clearedNodeGroups": ["ng1"]}
    assert mesh["aimgf"].patch(f"/models/{model_id}/runtime/node-groups", headers=OTHER, json=patch).status_code == 403
    assert mesh["aimgf"].post(f"/models/{model_id}/runtime/scale", headers=OTHER).status_code == 403
    assert mesh["aimgf"].post(f"/models/{model_id}/runtime/terminate", headers=OTHER).status_code == 403
    function_id = mesh["aimgf"].post("/aiml-inference-functions", json={}).json()["id"]
    loading = mesh["aimgf"].post("/ml-model-loading-requests", headers=OTHER, json={"aIMLInferenceFunctionRef": function_id, "mLModelToLoadRef": [model_id]})
    assert loading.status_code == 403 and "MODEL_ACCESS_DENIED" in loading.text

    assert mesh["aimgf"].patch(f"/models/{model_id}/runtime/node-groups", headers=ES, json=patch).status_code == 200
    assert mesh["aimgf"].patch(f"/models/{model_id}/runtime/node-groups", headers=SMO, json={"clearedNodeGroups": ["ng2"]}).status_code == 200


def test_a_model_an_smo_module_registers_stays_open_to_any_rapp(mesh):
    """A model registered by an SMO module without an owner is unowned, and any rApp passes the ownership check on it; one registered for a named rApp is that rApp's alone."""
    open_model = _register(mesh, SMO)
    assert mesh["mlmr"].get(f"/models/{open_model}").json()["owner"] is None
    assert mesh["aimgf"].patch(f"/models/{open_model}/runtime/node-groups", headers=OTHER, json={"clearedNodeGroups": ["ng1"]}).status_code == 200
    named = _register(mesh, SMO, owner="es-client")
    assert mesh["aimgf"].patch(f"/models/{named}/runtime/node-groups", headers=OTHER, json={"clearedNodeGroups": ["ng1"]}).status_code == 403
    assert mesh["aimgf"].patch(f"/models/{named}/runtime/node-groups", headers=ES, json={"clearedNodeGroups": ["ng1"]}).status_code == 200


def test_aimgf_start_and_read_routes_apply_the_owner_rule_through_the_mesh(mesh):
    """SEC-15.3c: another rApp is 403 `MODEL_ACCESS_DENIED` on training, validation, emulation and update requests, the lifecycle and its history and the MLMF subscriptions of a model
    (AIMgF reads the owner from MLMR over the mesh); the owner, the operator and a model with no owner pass, and nothing is created for the refused call.
    """
    model_id = _register(mesh, ES)
    aimgf = mesh["aimgf"]
    training = {"modelId": model_id, "producerId": "other"}
    subscription = {"model_id": model_id, "dme_type_id": _mlmf_dme_type(mesh, "ModelPerformance")}
    refused = [
        aimgf.post("/training-jobs", headers=OTHER, json=training),
        aimgf.post("/ml-training-requests", headers=OTHER, json={"mLModelRef": model_id, "trainingRequestSource": "other"}),
        aimgf.post("/validation-jobs", headers=OTHER, json=training),
        aimgf.post("/emulation-jobs", headers=OTHER, json=training),
        aimgf.post("/ml-update-requests", headers=OTHER, json={"mLModelRefList": [model_id]}),
        aimgf.get(f"/models/{model_id}/lifecycle", headers=OTHER),
        aimgf.get(f"/models/{model_id}/lifecycle-history", headers=OTHER),
        aimgf.post("/mlmf/subscriptions", headers=OTHER, params=subscription, json={"metric_types": ["accuracy"]}),
        aimgf.get("/mlmf/subscriptions", headers=OTHER, params={"model_id": model_id}),
    ]
    assert [r.status_code for r in refused] == [403] * len(refused), [r.text for r in refused]
    assert all("MODEL_ACCESS_DENIED" in r.text for r in refused)
    assert aimgf.get("/training-jobs", params={"model_id": model_id}).json()["total"] == 0

    # the registering rApp (the same client id) reads and starts; the console and an SMO module are not asked
    assert aimgf.get(f"/models/{model_id}/lifecycle", headers=ES).status_code == 200
    assert aimgf.get(f"/models/{model_id}/lifecycle-history", headers=ES).status_code == 200
    assert aimgf.get(f"/models/{model_id}/lifecycle", headers=SMO).status_code == 200
    assert aimgf.post("/mlmf/subscriptions", headers=ES, params=subscription, json={"metric_types": ["accuracy"]}).status_code == 201
    assert aimgf.get("/mlmf/subscriptions", headers=OTHER).json()["items"] == []     # not another rApp's
    assert aimgf.post("/training-jobs", headers=ES, json={"modelId": model_id, "producerId": "es"}).status_code == 201
    open_model = _register(mesh, SMO)
    assert aimgf.get(f"/models/{open_model}/lifecycle", headers=OTHER).status_code == 200


def test_mlmr_put_and_post_keep_the_owner_out_of_an_rapps_hands_through_the_mesh(mesh):
    """SEC-15.3c: an rApp cannot register a model for another owner, nor change, clear or claim the owner through PUT, nor update a model another rApp owns; the owner updates its own
    model's other fields, and the operator (role internal) still sets the owner.
    """
    mlmr = mesh["mlmr"]
    forged = mlmr.post("/models", headers=ES, json={"modelType": "forged-" + uuid.uuid4().hex[:6], "version": "1", "owner": "other-client"})
    assert forged.status_code == 403 and "MODEL_OWNER_IMMUTABLE" in forged.text
    model_id = _register(mesh, ES, owner="es-client")
    stored = mlmr.get(f"/models/{model_id}").json()
    body = {"modelType": stored["modelType"], "version": stored["version"]}
    assert mlmr.put(f"/models/{model_id}", headers=ES, json={**body, "description": "mine", "owner": "es-client"}).json()["description"] == "mine"
    for owner in ("other-client", ""):
        refused = mlmr.put(f"/models/{model_id}", headers=ES, json={**body, "owner": owner})
        assert refused.status_code == 403 and "MODEL_OWNER_IMMUTABLE" in refused.text
    foreign = mlmr.put(f"/models/{model_id}", headers=OTHER, json={**body, "description": "hijacked"})
    assert foreign.status_code == 403 and "MODEL_ACCESS_DENIED" in foreign.text
    assert mlmr.get(f"/models/{model_id}").json()["owner"] == "es-client"
    assert mlmr.put(f"/models/{model_id}", headers=SMO, json={**body, "owner": "other-client"}).json()["owner"] == "other-client"


def test_mlmr_delete_upload_download_phase_and_groups_apply_the_owner_rule_through_the_mesh(mesh):
    """SEC-15.3d: another rApp is 403 `MODEL_ACCESS_DENIED` on MLMR's DELETE, artifact upload, artifact download (the model sets no `accessReqs`), phase-info write and on a coordination group naming
    the model, and nothing changes; the owner (the same client id the SDK registers with) uploads, downloads, groups and deletes; the console and AIMgF (role internal) are not asked, a model with
    no owner is open to every rApp, and a model that is PUBLICLY_AVAILABLE can be downloaded by another rApp.
    """
    mlmr = mesh["mlmr"]
    zip_file = {"file": ("model.zip", b"pkzip-bytes", "application/zip")}
    model_id = _register(mesh, ES)

    assert mlmr.post(f"/models/{model_id}/artifact", headers=OTHER, files=zip_file).status_code == 403
    assert mlmr.get(f"/models/{model_id}").json()["artifactLocation"] is None
    assert mlmr.post(f"/models/{model_id}/artifact", headers=ES, files=zip_file).status_code == 201
    assert mlmr.post(f"/models/{model_id}/artifact", headers=SMO, files=zip_file).status_code == 201

    denied = mlmr.get(f"/models/{model_id}/artifact/1", headers=OTHER)
    assert denied.status_code == 403 and "MODEL_ACCESS_DENIED" in denied.text
    assert mlmr.get(f"/models/{model_id}/artifact/1", headers=ES).content == b"pkzip-bytes"
    assert mlmr.get(f"/models/{model_id}/artifact/1", headers=SMO).content == b"pkzip-bytes"
    shared = _register(mesh, ES, storeDiscReqs={"accessReqs": {"accessReq": "PUBLICLY_AVAILABLE"}})
    mlmr.post(f"/models/{shared}/artifact", headers=ES, files=zip_file)
    assert mlmr.get(f"/models/{shared}/artifact/1", headers=OTHER).status_code == 200

    other_model = _register(mesh, OTHER)
    assert mlmr.post("/coordination-groups", headers=ES, json={"memberModelIds": [model_id, other_model]}).status_code == 403
    assert mlmr.post("/coordination-groups", headers=ES, json={"memberModelIds": [model_id, shared]}).status_code == 201
    # AIMgF writes the phase as an SMO module; another rApp may not
    assert mlmr.patch(f"/models/{model_id}/phase-info", headers=OTHER, json={"phase": "DEPLOYED"}).status_code == 403
    assert mlmr.patch(f"/models/{model_id}/phase-info", headers=SMO, json={"phase": "TRAINED"}).status_code == 200

    assert mlmr.delete(f"/models/{model_id}", headers=OTHER).status_code == 403
    assert mlmr.get(f"/models/{model_id}").status_code == 200
    assert mlmr.delete(f"/models/{model_id}", headers=ES).status_code == 204
    assert mlmr.get(f"/models/{model_id}").status_code == 404
    assert mlmr.delete(f"/models/{shared}", headers=SMO).status_code == 204
    open_model = _register(mesh, SMO)
    assert mlmr.delete(f"/models/{open_model}", headers=OTHER).status_code == 204


def _mlmf_dme_type(mesh, name: str) -> str:
    """Registers a DME type (as RAN NF OAM, an SMO module) and returns its id: an MLMF subscription must name a type DME knows (call flow 13a)."""
    return mesh["dme"].post("/production-capabilities", json={
        "namespace": "RAN", "name": name, "version": "1.0.0", "typeName": f"RAN.{name}", "producerId": "ran-nf-oam",
        "dataProductionSchema": {"type": "object"}, "producerHealthCallbackUrl": "http://ran-nf-oam:8000/health",
        "jobCallbackUrl": "http://ran-nf-oam:8000/dme-jobs"}).json()["registrationId"]
