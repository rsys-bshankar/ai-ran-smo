"""Tests of the deploy gate, `POST /models/{id}/deploy` (`app/loading/gate.py`, the MLLF service until S2.2): the CERTIFIED-or-PROMOTED rule, the node
groups written on AIMgF's own lifecycle row in the same transaction, and the owner rule (MLLF-NODEGROUP-OWNER).

Run: `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_loading_gate.py -q`. No MLLF container, no R1 double: the gate is an in-process
check of the real `model_lifecycle` row in the SQLite database of `test_main.py`'s fixtures; only the registry is the `mlmr` double, which stands in for a
model's existence and owner.
"""

import uuid

import pytest

from test_main import _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)

from app.governance.statemachine import ModelLifecycleState

RAPP_A = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-a"}
RAPP_B = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-b"}
CONSOLE = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-invoker"}


def _model(mlmr, db_session_factory, state, owner=None) -> uuid.UUID:
    """A model in the registry double with `owner`, and its lifecycle row at `state`; returns its id."""
    model_id = mlmr.add_model(owner=owner)
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=state)
    return model_id


def _cleared(client, model_id) -> list[str]:
    """The node groups AIMgF's lifecycle row holds for the model now."""
    return client.get(f"/models/{model_id}/lifecycle").json()["clearedNodeGroups"]


# Each row is a lifecycle state that is not placeable.
@pytest.mark.parametrize("state", [ModelLifecycleState.TRAINING, ModelLifecycleState.APPROVED, ModelLifecycleState.DEPRECATED, ModelLifecycleState.RETIRED])
def test_a_model_that_is_not_certified_or_promoted_is_refused_and_nothing_is_written(client, mlmr, db_session_factory, state):
    """409 MODEL_NOT_CERTIFIED naming the state; the node groups stay as they were."""
    model_id = _model(mlmr, db_session_factory, state)
    resp = client.post(f"/models/{model_id}/deploy", json=["ng1"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "MODEL_NOT_CERTIFIED" and state in resp.json()["detail"]["detail"]
    assert _cleared(client, model_id) == []


# Each row is a placeable lifecycle state.
@pytest.mark.parametrize("state", [ModelLifecycleState.CERTIFIED, ModelLifecycleState.PROMOTED])
def test_a_certified_or_promoted_model_gets_its_node_groups_written_on_the_lifecycle_row(client, mlmr, db_session_factory, state):
    """The node groups replace the stored list (no merge) on AIMgF's own row, which the lifecycle read shows; the answer echoes them."""
    model_id = _model(mlmr, db_session_factory, state)
    assert client.post(f"/models/{model_id}/deploy", json=["ng1", "ng2"]).json() == {"modelId": str(model_id), "clearedNodeGroups": ["ng1", "ng2"]}
    assert client.post(f"/models/{model_id}/deploy", json=["ng3"]).status_code == 200
    assert _cleared(client, model_id) == ["ng3"]


def test_a_model_the_registry_does_not_know_is_404(client, mlmr):
    """An id the registry has no model for is 404 MODEL_NOT_FOUND, and no lifecycle row is made for it."""
    resp = client.post(f"/models/{uuid.uuid4()}/deploy", json=["ng1"])
    assert resp.status_code == 404 and resp.json()["detail"]["title"] == "MODEL_NOT_FOUND"


def test_a_model_with_no_lifecycle_row_is_registered_and_so_refused(client, mlmr):
    """A model AIMgF has never acted on starts at REGISTERED, which is not placeable: 409."""
    model_id = mlmr.add_model()
    resp = client.post(f"/models/{model_id}/deploy", json=["ng1"])
    assert resp.status_code == 409 and "REGISTERED" in resp.json()["detail"]["detail"]


def test_a_second_rapp_cannot_place_another_rapps_model(client, mlmr, db_session_factory):
    """rApp B's deploy request on rApp A's model is 403 MODEL_ACCESS_DENIED naming the model, and no node groups are written."""
    model_id = _model(mlmr, db_session_factory, ModelLifecycleState.CERTIFIED, owner="rapp-a")
    resp = client.post(f"/models/{model_id}/deploy", json=["ng1"], headers=RAPP_B)
    assert resp.status_code == 403
    assert resp.json()["detail"]["title"] == "MODEL_ACCESS_DENIED" and str(model_id) in resp.json()["detail"]["detail"]
    assert _cleared(client, model_id) == []


def test_the_owner_and_the_console_place_the_model(client, mlmr, db_session_factory):
    """The owner rApp and the console (role internal, acting for no rApp) both get the node groups written; an owner-less model is open to any rApp."""
    model_id = _model(mlmr, db_session_factory, ModelLifecycleState.CERTIFIED, owner="rapp-a")
    for headers, groups in ((RAPP_A, ["ng-owner"]), (CONSOLE, ["ng-console"])):
        resp = client.post(f"/models/{model_id}/deploy", json=groups, headers=headers)
        assert resp.status_code == 200 and resp.json()["clearedNodeGroups"] == groups
    open_model = _model(mlmr, db_session_factory, ModelLifecycleState.PROMOTED)
    assert client.post(f"/models/{open_model}/deploy", json=["ng1"], headers=RAPP_B).status_code == 200


def test_a_module_acting_for_another_rapp_is_judged_as_that_rapp(client, mlmr, db_session_factory):
    """An SMO module passing on rApp B's request (`X-R1-On-Behalf-Of`) is refused as rApp B would be (MLLF-NODEGROUP-OWNER)."""
    model_id = _model(mlmr, db_session_factory, ModelLifecycleState.CERTIFIED, owner="rapp-a")
    acting_for_b = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "rapp-b", "X-R1-On-Behalf-Of": "rapp-b"}
    assert client.post(f"/models/{model_id}/deploy", json=["ng1"], headers=acting_for_b).status_code == 403


def test_the_body_must_be_a_list_of_node_group_names(client, mlmr, db_session_factory):
    """A body that is not a list of strings is 422."""
    model_id = _model(mlmr, db_session_factory, ModelLifecycleState.CERTIFIED)
    assert client.post(f"/models/{model_id}/deploy", json={"nodeGroups": ["ng1"]}).status_code == 422
