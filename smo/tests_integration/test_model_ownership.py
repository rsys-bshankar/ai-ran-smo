"""SEC-15.3b through the mesh: the rApp that registers a model at MLMR owns it, and AIMgF refuses another rApp's scale, terminate, node-groups and model-loading calls on it.

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
