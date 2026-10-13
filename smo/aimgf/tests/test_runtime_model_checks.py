"""SEC-15.3: the runtime routes (deploy, activate, inference) check MLMR's model: it exists, its phase can serve, and an rApp caller owns it.

Fixtures and helpers come from `test_main.py` (`client`, `mlmr`, `db_session_factory`, `_set_lifecycle`); `mlmr.add_model(**attributes)` sets what MLMR answers for the model
(`owner`, `phaseInfo`, `storeDiscReqs`). The gateway's headers are sent by hand: `X-R1-Role` and `X-R1-Invoker-Id`. SQLite, no network. Run with
`cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_runtime_model_checks.py -q`.
"""

import uuid

import pytest

from test_main import _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)
from app.statemachine import ModelLifecycleState, RuntimeLifecycleState

OWNER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-client"}
OTHER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "someone-else"}
OPERATOR = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-invoker"}


def _certified(db_session_factory, model_id, runtime=None):
    """Puts the model's lifecycle at CERTIFIED (and the runtime at `runtime` when given), so only the checks under test can refuse a request."""
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED, runtime_lifecycle_state=runtime)


def _deploy(client, model_id, headers=None):
    """POST the runtime deploy of the model."""
    return client.post(f"/models/{model_id}/runtime/deploy", headers=headers or {})


def _activate(client, model_id, headers=None):
    """POST the runtime activation of the model."""
    return client.post(f"/models/{model_id}/runtime/activate", headers=headers or {})


def _infer(client, model_id, headers=None):
    """POST an inference job for the model."""
    return client.post(f"/models/{model_id}/inference-jobs", headers=headers or {})


# ---------------------------------------------------------------- exists

def test_activating_an_unknown_model_is_404_and_leaves_no_lifecycle_row(client, db_session_factory, mlmr):
    """Activate asked MLMR nothing before: an id MLMR does not know is now 404 `MODEL_NOT_FOUND` and no lifecycle row is created for it."""
    from app.models import ModelLifecycle
    resp = _activate(client, uuid.uuid4())
    assert resp.status_code == 404 and "MODEL_NOT_FOUND" in resp.text
    with db_session_factory() as db:
        assert db.query(ModelLifecycle).count() == 0


@pytest.mark.parametrize("call", [_deploy, _infer])
def test_deploy_and_inference_of_an_unknown_model_are_404(client, mlmr, call):
    """Deploy and inference keep answering 404 `MODEL_NOT_FOUND` for a model MLMR does not know."""
    assert call(client, uuid.uuid4()).status_code == 404


# ---------------------------------------------------------------- phase

# Table: the MLMR phases that have no trained model to serve, for the three routes.
@pytest.mark.parametrize("phase", ["NOT_TRAINED", "IN_TRAINING"])
@pytest.mark.parametrize("call", [_deploy, _activate, _infer])
def test_a_model_that_is_not_trained_cannot_be_served(client, mlmr, db_session_factory, call, phase):
    """A model MLMR shows as NOT_TRAINED or IN_TRAINING is 409 on deploy, activate and inference, even when AIMgF's own lifecycle says CERTIFIED."""
    model_id = mlmr.add_model(phaseInfo={"phase": phase})
    _certified(db_session_factory, model_id, RuntimeLifecycleState.ACTIVE)
    resp = call(client, model_id)
    assert resp.status_code == 409 and phase in resp.text
    assert mlmr.nfo.descriptors == {}


@pytest.mark.parametrize("phase", ["TRAINED", "IN_RETRAINING", "DEPLOYED", None])
def test_a_trained_or_unrecorded_phase_still_deploys(client, mlmr, db_session_factory, phase):
    """TRAINED, IN_RETRAINING, DEPLOYED, and a model whose phase was never written back all deploy as before (the lifecycle gate still applies)."""
    model_id = mlmr.add_model(**({"phaseInfo": {"phase": phase}} if phase else {}))
    _certified(db_session_factory, model_id)
    assert _deploy(client, model_id).status_code == 201


# ---------------------------------------------------------------- ownership

def test_an_rapp_that_does_not_own_the_model_cannot_deploy_or_activate_it(client, mlmr, db_session_factory):
    """Another rApp is 403 `MODEL_ACCESS_DENIED` on deploy and on activate, and nothing is created in NFO."""
    model_id = mlmr.add_model(owner="es-client")
    _certified(db_session_factory, model_id, RuntimeLifecycleState.DEPLOYED)
    for call in (_deploy, _activate):
        resp = call(client, model_id, OTHER)
        assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    assert mlmr.nfo.descriptors == {}


def test_the_owner_the_operator_and_an_unowned_model_pass(client, mlmr, db_session_factory):
    """The owner rApp, the operator's console (role internal) and any rApp for a model with no owner set deploy as before."""
    owned = mlmr.add_model(owner="es-client")
    _certified(db_session_factory, owned)
    assert _deploy(client, owned, OWNER).status_code == 201
    operated = mlmr.add_model(owner="es-client")
    _certified(db_session_factory, operated)
    assert _deploy(client, operated, OPERATOR).status_code == 201
    unowned = mlmr.add_model()
    _certified(db_session_factory, unowned)
    assert _deploy(client, unowned, OTHER).status_code == 201


def test_inference_by_a_non_owner_depends_on_the_models_access_requirements(client, mlmr, db_session_factory):
    """Inference is 403 for a non-owner rApp unless MLMR's `accessReqs` lets it use the model (PUBLICLY_AVAILABLE, or RESTRICTED naming it); the owner always may."""
    cases = {
        "private": ({"accessReq": "PRIVATE_USE_ONLY"}, 403),
        "no-requirements": (None, 403),
        "public": ({"accessReq": "PUBLICLY_AVAILABLE"}, 201),
        "restricted-named": ({"accessReq": "RESTRICTED", "valClientIds": ["someone-else"]}, 201),
        "restricted-other": ({"accessReq": "RESTRICTED", "valClientIds": ["a-third"]}, 403),
    }
    for name, (access, expected) in cases.items():
        attributes = {"owner": "es-client", "storeDiscReqs": {"accessReqs": access}} if access else {"owner": "es-client"}
        model_id = mlmr.add_model(**attributes)
        _certified(db_session_factory, model_id, RuntimeLifecycleState.ACTIVE)
        assert _infer(client, model_id, OTHER).status_code == expected, name
        assert _infer(client, model_id, OWNER).status_code == 201, name


def test_public_access_does_not_let_another_rapp_deploy_the_model(client, mlmr, db_session_factory):
    """`accessReqs` is about using a model, not about changing its serving runtime: a PUBLICLY_AVAILABLE model is still deployed only by its owner."""
    model_id = mlmr.add_model(owner="es-client", storeDiscReqs={"accessReqs": {"accessReq": "PUBLICLY_AVAILABLE"}})
    _certified(db_session_factory, model_id)
    assert _deploy(client, model_id, OTHER).status_code == 403


# ---------------------------------------------------------------- SEC-15.3b: scale, terminate, node-groups, model loading

def _scale(client, model_id, headers=None):
    """POST the runtime scale of the model."""
    return client.post(f"/models/{model_id}/runtime/scale", headers=headers or {})


def _terminate(client, model_id, headers=None):
    """POST the runtime termination of the model."""
    return client.post(f"/models/{model_id}/runtime/terminate", headers=headers or {})


def _node_groups(client, model_id, headers=None):
    """PATCH the cleared node groups of the model."""
    return client.patch(f"/models/{model_id}/runtime/node-groups", json={"clearedNodeGroups": ["ng1"]}, headers=headers or {})


@pytest.mark.parametrize("call", [_scale, _terminate, _node_groups])
def test_scale_terminate_and_node_groups_of_an_unknown_model_are_404_and_leave_no_row(client, db_session_factory, mlmr, call):
    """The three routes asked MLMR nothing before: an id MLMR does not know is 404 `MODEL_NOT_FOUND`, and no lifecycle row is created for it."""
    from app.models import ModelLifecycle
    resp = call(client, uuid.uuid4())
    assert resp.status_code == 404 and "MODEL_NOT_FOUND" in resp.text
    with db_session_factory() as db:
        assert db.query(ModelLifecycle).count() == 0


@pytest.mark.parametrize("call", [_scale, _terminate, _node_groups])
def test_another_rapp_cannot_scale_terminate_or_place_a_model(client, db_session_factory, mlmr, call):
    """An rApp that is not the owner is 403 `MODEL_ACCESS_DENIED` on scale, terminate and node-groups: the runtime stays as it was, NFO is not called, the node groups are not written."""
    from app.models import ModelLifecycle
    model_id = mlmr.add_model(owner="es-client")
    _certified(db_session_factory, model_id, RuntimeLifecycleState.ACTIVE)
    deployments_before = dict(mlmr.nfo.deployments)
    resp = call(client, model_id, OTHER)
    assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    assert mlmr.nfo.deployments == deployments_before
    with db_session_factory() as db:
        row = db.query(ModelLifecycle).one()
        assert row.runtime_lifecycle_state == RuntimeLifecycleState.ACTIVE and not row.cleared_node_groups


@pytest.mark.parametrize("call", [_scale, _terminate, _node_groups])
def test_the_owner_the_operator_and_an_unowned_model_pass_scale_terminate_and_node_groups(client, db_session_factory, mlmr, call):
    """The owner rApp, the operator's console (role internal), a call with no role and any rApp on a model with no owner get past the ownership check on the three routes."""
    for attributes, headers in (({"owner": "es-client"}, OWNER), ({"owner": "es-client"}, OPERATOR), ({"owner": "es-client"}, None), ({}, OTHER)):
        model_id = mlmr.add_model(**attributes)
        _certified(db_session_factory, model_id, RuntimeLifecycleState.ACTIVE)
        assert call(client, model_id, headers).status_code == 200


def test_the_phase_does_not_stop_a_terminate(client, db_session_factory, mlmr):
    """Only existence and ownership are checked on terminate: a model MLMR shows as IN_TRAINING (a runtime from an earlier version being torn down) is not refused on its phase."""
    model_id = mlmr.add_model(owner="es-client", phaseInfo={"phase": "IN_TRAINING"})
    _certified(db_session_factory, model_id, RuntimeLifecycleState.ACTIVE)
    assert _terminate(client, model_id, OWNER).status_code == 200


def _function(client):
    """Creates an AIMLInferenceFunction and returns its id."""
    return client.post("/aiml-inference-functions", json={}).json()["id"]


def test_model_loading_refuses_a_model_of_another_rapp(client, db_session_factory, mlmr):
    """MLModelLoadingRequest (create and resume) and the loading policy trigger are 403 `MODEL_ACCESS_DENIED` for an rApp that does not own the model; nothing is deployed in NFO and no
    request row is stored. The owner, the operator and an unowned model load.
    """
    function_id = _function(client)
    foreign = mlmr.add_model(owner="es-client")
    _certified(db_session_factory, foreign)
    body = {"aIMLInferenceFunctionRef": function_id, "mLModelToLoadRef": [str(foreign)]}
    resp = client.post("/ml-model-loading-requests", json=body, headers=OTHER)
    assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    assert client.get("/ml-model-loading-requests").json()["total"] == 0 and mlmr.nfo.descriptors == {}

    # a request suspended by the owner and resumed by another rApp is refused at the resume too
    suspended = client.post("/ml-model-loading-requests", json={**body, "suspendRequest": True}, headers=OWNER)
    assert suspended.status_code == 201
    resumed = client.patch(f"/ml-model-loading-requests/{suspended.json()['id']}", json={"suspendRequest": False}, headers=OTHER)
    assert resumed.status_code == 403 and mlmr.nfo.descriptors == {}

    policy = client.post("/ml-model-loading-policies", json={"aIMLInferenceFunctionRef": function_id, "mLModelRef": [str(foreign)]}).json()["id"]
    assert client.post(f"/ml-model-loading-policies/{policy}/trigger", headers=OTHER).status_code == 403
    assert mlmr.nfo.descriptors == {}

    assert client.post("/ml-model-loading-requests", json=body, headers=OWNER).status_code == 201
    unowned = mlmr.add_model()
    _certified(db_session_factory, unowned)
    assert client.post("/ml-model-loading-requests", json={**body, "mLModelToLoadRef": [str(unowned)]}, headers=OTHER).status_code == 201
    operated = mlmr.add_model(owner="es-client")
    _certified(db_session_factory, operated)
    assert client.post("/ml-model-loading-requests", json={**body, "mLModelToLoadRef": [str(operated)]}, headers=OPERATOR).status_code == 201


def test_model_loading_is_all_or_nothing_across_owners(client, db_session_factory, mlmr):
    """A request naming one model of the caller and one of another rApp is refused as a whole: the caller's own model is not deployed either."""
    function_id = _function(client)
    mine, theirs = mlmr.add_model(owner="someone-else"), mlmr.add_model(owner="es-client")
    _certified(db_session_factory, mine)
    _certified(db_session_factory, theirs)
    resp = client.post("/ml-model-loading-requests", json={
        "aIMLInferenceFunctionRef": function_id, "mLModelToLoadRef": [str(mine), str(theirs)]}, headers=OTHER)
    assert resp.status_code == 403 and mlmr.nfo.descriptors == {}
