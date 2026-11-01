"""MLLF-NODEGROUP-OWNER: AIMgF's ownership rule also judges an SMO module (role `internal`) that acts for an rApp (`X-R1-On-Behalf-Of`), as that rApp.

Covered: `PATCH /models/{id}/runtime/node-groups` (the call MLLF makes for a deploy request), and every route of the SEC-15.3c ownership table in
`test_model_ownership_routes.py` (`ROUTES`), so the rule is the same wherever `_owner_may_act` decides. For each: a module acting for another rApp is 403
`MODEL_ACCESS_DENIED`, a module acting for the owner passes, and a module with no `X-R1-On-Behalf-Of` (the console, a module on its own account) passes as before.
Also the inference job's `requested_by` is the rApp a module acted for.

Fixtures and helpers come from `test_main.py` (`client`, `mlmr`, `db_session_factory`, `_set_lifecycle`); `mlmr.add_model(owner=...)` sets the model's owner.
The gateway's headers are sent by hand. SQLite, no network. Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_on_behalf_owner.py -q`.
"""

import pytest

from test_main import _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)
from test_model_ownership_routes import ROUTES
from app.models import InferenceJob, ModelLifecycle
from app.governance.statemachine import ModelLifecycleState, RuntimeLifecycleState

FOR_OTHER = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "mllf-invoker", "X-R1-On-Behalf-Of": "someone-else"}
FOR_OWNER = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "mllf-invoker", "X-R1-On-Behalf-Of": "es-client"}
MODULE = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "mllf-invoker"}


def _node_groups(client, model_id, headers):
    """PATCH the cleared node groups of the model, as MLLF does."""
    return client.patch(f"/models/{model_id}/runtime/node-groups", json={"clearedNodeGroups": ["ng1"]}, headers=headers)


def test_a_module_acting_for_another_rapp_cannot_place_the_model(client, db_session_factory, mlmr):
    """MLLF's write for rApp B on rApp A's model is 403 `MODEL_ACCESS_DENIED` and the node groups stay unwritten: a module passing B's request on is B asking."""
    model_id = mlmr.add_model(owner="es-client")
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    resp = _node_groups(client, model_id, FOR_OTHER)
    assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    with db_session_factory() as db:
        assert not db.query(ModelLifecycle).one().cleared_node_groups


@pytest.mark.parametrize("headers", [FOR_OWNER, MODULE], ids=["for-owner", "no-on-behalf-of"])
def test_a_module_acting_for_the_owner_or_for_nobody_places_the_model(client, db_session_factory, mlmr, headers):
    """A module acting for the owner, and a module or the console with no `X-R1-On-Behalf-Of`, write the node groups as before."""
    model_id = mlmr.add_model(owner="es-client")
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    resp = _node_groups(client, model_id, headers)
    assert resp.status_code == 200 and resp.json()["clearedNodeGroups"] == ["ng1"]


@pytest.mark.parametrize("call, prepare, ok", ROUTES)
def test_the_rule_applies_to_a_module_acting_for_an_rapp_on_every_owned_route(client, db_session_factory, mlmr, call, prepare, ok):
    """On every route of the SEC-15.3c table, a module acting for another rApp is 403 and one acting for the owner, or for nobody, gets the normal answer."""
    model_id = mlmr.add_model(owner="es-client")
    prepare(db_session_factory, model_id)
    resp = call(client, model_id, FOR_OTHER)
    assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    for headers in (FOR_OWNER, MODULE):
        model_id = mlmr.add_model(owner="es-client")
        prepare(db_session_factory, model_id)
        assert call(client, model_id, headers).status_code == ok


def test_an_inference_job_a_module_requests_records_the_rapp_it_acts_for(client, db_session_factory, mlmr):
    """`requested_by` is the rApp named in `X-R1-On-Behalf-Of`, so that rApp may later resolve the job it asked for through the module, and another may not."""
    model_id = mlmr.add_model(owner="es-client", storeDiscReqs={"accessReqs": {"accessReq": "PUBLICLY_AVAILABLE"}})
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED,
                   runtime_lifecycle_state=RuntimeLifecycleState.ACTIVE)
    resp = client.post(f"/models/{model_id}/inference-jobs", headers={**MODULE, "X-R1-On-Behalf-Of": "consumer-rapp"})
    assert resp.status_code == 201, resp.text
    with db_session_factory() as db:
        assert db.query(InferenceJob).one().requested_by == "consumer-rapp"
