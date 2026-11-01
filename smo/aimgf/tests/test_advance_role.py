"""SEC-15.1: `POST /models/{id}/advance` is not for an rApp.

Governance decisions and the end-of-life events are an operator's; the gateway stamps the caller's role in `X-R1-Role` and this route refuses `rapp`. Fixtures (`client`, `mlmr`,
`db_session_factory`) and `_set_lifecycle` come from `test_main.py`. Run with `PYTHONPATH=.:../shared python -m pytest tests/test_advance_role.py -q`.
"""

import pytest

from test_main import _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (fixtures)
from app.models import CertificationRecord, ModelLifecycle
from app.governance.statemachine import ModelLifecycleState

RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-client"}
OPERATOR = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-invoker"}


# Table: a governance decision, the two end-of-life events and an unknown event name. The role is checked before the event, so an rApp learns nothing from the answer.
@pytest.mark.parametrize("event", ["APPROVE_TRAINING", "CERTIFY", "PROMOTE", "DEPRECATE", "RETIRE", "NOT_AN_EVENT"])
def test_an_rapp_cannot_advance_a_model(client, mlmr, db_session_factory, event):
    """An rApp token gets 403 ROLE_NOT_PERMITTED for any event, and the lifecycle and the governance history are unchanged."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED)
    resp = client.post(f"/models/{model_id}/advance", params={"event": event, "decided_by": "me"}, headers=RAPP)
    assert resp.status_code == 403 and "ROLE_NOT_PERMITTED" in resp.text
    with db_session_factory() as db:
        assert db.query(CertificationRecord).count() == 0
        assert db.query(ModelLifecycle).one().model_lifecycle_state == ModelLifecycleState.TRAINED


def test_an_operator_and_a_call_without_a_role_still_advance_a_model(client, mlmr, db_session_factory):
    """The operator's console (role internal) and a call that did not come through the gateway (no role) advance as before."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED)
    params = {"event": "APPROVE_TRAINING", "decided_by": "alice"}
    assert client.post(f"/models/{model_id}/advance", params=params, headers=OPERATOR).status_code == 200
    assert client.post(f"/models/{model_id}/advance", params=params).status_code == 200


def test_the_refusals_say_why(client, mlmr, db_session_factory):
    """The 403 for an rApp names the reason, and the 422s for an unknown and for a job-driven event name the event, the accepted events and the job route to use."""
    model_id = mlmr.add_model()
    refused = client.post(f"/models/{model_id}/advance", params={"event": "CERTIFY"}, headers=RAPP).json()["detail"]["detail"]
    assert refused == "an rApp cannot advance a model's lifecycle: governance and end-of-life decisions are an operator's"
    unknown = client.post(f"/models/{model_id}/advance", params={"event": "NOPE"}).json()["detail"]["detail"]
    assert unknown.startswith("unknown model lifecycle event 'NOPE'; advance accepts [") and "CERTIFY" in unknown
    job_driven = client.post(f"/models/{model_id}/advance", params={"event": "CREATE_TRAINING"}).json()["detail"]["detail"]
    assert "is job-driven and cannot be advanced directly; use " in job_driven and "CREATE_TRAINING" in job_driven
