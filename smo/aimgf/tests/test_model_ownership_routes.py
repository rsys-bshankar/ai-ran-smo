"""SEC-15.3c: the routes that start work on a model or read it apply the ownership rule: a caller with role `rapp` may act only on a model whose MLMR `owner` is its own invoker id.

Covered: `POST /training-jobs`, `/ml-training-requests`, `/validation-jobs`, `/ml-testing-requests`, `/emulation-jobs`, `/ml-update-requests`, `GET /models/{id}/lifecycle` and
`/lifecycle-history`, and the MLMF subscription routes (create, list, delete, reports, a report). For each: the owner passes, another rApp is 403 `MODEL_ACCESS_DENIED` and nothing is
written, the operator's console (role `internal`) and a call with no role pass, and a model with no owner is open to any rApp.

Fixtures and helpers come from `test_main.py` (`client`, `mlmr`, `db_session_factory`, `_set_lifecycle`); `mlmr.add_model(**attributes)` sets what MLMR answers for the model. The gateway's
headers are sent by hand. SQLite, no network. Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_model_ownership_routes.py -q`.
"""

import uuid

import pytest

from test_main import _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)
from app.models import EmulationJob, MLMFSubscription, TrainingJob, ValidationJob
from app.statemachine import ModelLifecycleState

OWNER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-client"}
OTHER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "someone-else"}
OPERATOR = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-invoker"}


def _trainable(db_session_factory, model_id):
    """A model that a training run may start on: a new model is REGISTERED, so nothing is set."""


def _trained(db_session_factory, model_id):
    """Puts the model at TRAINED with the operator's APPROVE_TRAINING recorded, the state a validation run needs."""
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)


def _validated(db_session_factory, model_id):
    """Puts the model at VALIDATED with APPROVE_VALIDATION recorded, the state an emulation run needs."""
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.VALIDATED, validation_approved=True)


# Table: route name, how to put the model in the state it needs, the call (client, model id, headers) and the success status.
def _training_job(client, model_id, headers):
    """POST /training-jobs for the model."""
    return client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}, headers=headers)


def _training_request(client, model_id, headers):
    """POST /ml-training-requests for the model."""
    return client.post("/ml-training-requests", json={"mLModelRef": str(model_id), "trainingRequestSource": "rapp-1"}, headers=headers)


def _validation_job(client, model_id, headers):
    """POST /validation-jobs for the model."""
    return client.post("/validation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}, headers=headers)


def _testing_request(client, model_id, headers):
    """POST /ml-testing-requests for the model."""
    return client.post("/ml-testing-requests", json={"mLModelRef": str(model_id), "requestSource": "rapp-1"}, headers=headers)


def _emulation_job(client, model_id, headers):
    """POST /emulation-jobs for the model."""
    return client.post("/emulation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}, headers=headers)


def _update_request(client, model_id, headers):
    """POST /ml-update-requests naming the model."""
    return client.post("/ml-update-requests", json={"mLModelRefList": [str(model_id)]}, headers=headers)


def _lifecycle(client, model_id, headers):
    """GET /models/{id}/lifecycle."""
    return client.get(f"/models/{model_id}/lifecycle", headers=headers)


def _history(client, model_id, headers):
    """GET /models/{id}/lifecycle-history."""
    return client.get(f"/models/{model_id}/lifecycle-history", headers=headers)


def _subscribe(client, model_id, headers):
    """POST /mlmf/subscriptions for the model."""
    return client.post("/mlmf/subscriptions", params={"model_id": str(model_id), "dme_type_id": str(uuid.uuid4())}, json={"metric_types": ["accuracy"]}, headers=headers)


def _list_subscriptions(client, model_id, headers):
    """GET /mlmf/subscriptions filtered to the model."""
    return client.get("/mlmf/subscriptions", params={"model_id": str(model_id)}, headers=headers)


ROUTES = [
    pytest.param(_training_job, _trainable, 201, id="training-jobs"),
    pytest.param(_training_request, _trainable, 201, id="ml-training-requests"),
    pytest.param(_validation_job, _trained, 201, id="validation-jobs"),
    pytest.param(_testing_request, _trained, 201, id="ml-testing-requests"),
    pytest.param(_emulation_job, _validated, 201, id="emulation-jobs"),
    pytest.param(_update_request, _trainable, 201, id="ml-update-requests"),
    pytest.param(_lifecycle, _trainable, 200, id="lifecycle"),
    pytest.param(_history, _trainable, 200, id="lifecycle-history"),
    pytest.param(_subscribe, _trainable, 201, id="mlmf-subscribe"),
    pytest.param(_list_subscriptions, _trainable, 200, id="mlmf-list"),
]


def _rows(db_session_factory) -> int:
    """Counts the training, validation, emulation and subscription rows, the things these routes create."""
    with db_session_factory() as db:
        return sum(db.query(t).count() for t in (TrainingJob, ValidationJob, EmulationJob, MLMFSubscription))


@pytest.mark.parametrize("call, prepare, ok", ROUTES)
def test_another_rapp_is_refused_and_nothing_is_written(client, db_session_factory, mlmr, call, prepare, ok):
    """An rApp that is not the model's owner is 403 `MODEL_ACCESS_DENIED` on every route, before the lifecycle gate (the model is in the right state, so only ownership can refuse),
    and no job, subscription or NFO runtime is created.
    """
    model_id = mlmr.add_model(owner="es-client")
    prepare(db_session_factory, model_id)
    resp = call(client, model_id, OTHER)
    # a listing filtered to a model of another rApp is refused too, not answered with an empty page
    assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    assert _rows(db_session_factory) == 0 and mlmr.nfo.descriptors == {}


@pytest.mark.parametrize("call, prepare, ok", ROUTES)
def test_the_owner_the_operator_and_a_call_with_no_role_pass(client, db_session_factory, mlmr, call, prepare, ok):
    """The owner rApp (its invoker id equals `owner`), the operator's console and an SMO module (role `internal`) and a call with no role get the route's normal answer."""
    for headers in (OWNER, OPERATOR, None):
        model_id = mlmr.add_model(owner="es-client")
        prepare(db_session_factory, model_id)
        assert call(client, model_id, headers).status_code == ok


@pytest.mark.parametrize("call, prepare, ok", ROUTES)
def test_a_model_with_no_owner_is_open_to_any_rapp(client, db_session_factory, mlmr, call, prepare, ok):
    """A model MLMR holds without an owner (registered before SEC-15.3b, or by an SMO module that named none) passes for any rApp: the open state is unchanged by SEC-15.3c."""
    model_id = mlmr.add_model()
    prepare(db_session_factory, model_id)
    assert call(client, model_id, OTHER).status_code == ok


@pytest.mark.parametrize("call", [_training_job, _training_request, _validation_job, _testing_request, _emulation_job, _update_request, _lifecycle, _subscribe])
def test_an_unknown_model_is_404_for_an_rapp(client, db_session_factory, mlmr, call):
    """A model id MLMR does not know is 404 `MODEL_NOT_FOUND` for an rApp on the routes that always looked the model up, and on the subscription create and the history, which looked nothing up before."""
    resp = call(client, uuid.uuid4(), OTHER)
    assert resp.status_code == 404 and "MODEL_NOT_FOUND" in resp.text


def test_the_history_of_an_unknown_model_is_still_open_but_a_subscription_to_it_is_404(client, mlmr):
    """The console, an SMO module and a call with no role are not asked about the model on the history, so the history of a model MLMR has since deleted stays
    readable; subscribing to a model MLMR does not know is 404 `MODEL_NOT_FOUND` for every caller (call flow 13a)."""
    unknown = uuid.uuid4()
    for headers in (OPERATOR, None):
        assert _history(client, unknown, headers).json()["items"] == []
        resp = _subscribe(client, unknown, headers)
        assert resp.status_code == 404 and "MODEL_NOT_FOUND" in resp.text


def test_a_refused_update_request_starts_no_run_for_the_models_of_the_caller(client, db_session_factory, mlmr):
    """An update request naming one model of the caller and one of another rApp is refused whole: the caller's own model starts no run."""
    mine, theirs = mlmr.add_model(owner="someone-else"), mlmr.add_model(owner="es-client")
    resp = client.post("/ml-update-requests", json={"mLModelRefList": [str(mine), str(theirs)]}, headers=OTHER)
    assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    assert _rows(db_session_factory) == 0 and mlmr.nfo.descriptors == {}


def test_group_retrain_propagation_is_not_asked_about_ownership(client, db_session_factory, mlmr):
    """The runs a breached subscription starts for a coordination group's members are the platform's own: they go through `_start_training` without a caller, so a member of another owner
    is retrained (SEC-15.3c did not change the propagation; only the report filing is the owner's).
    """
    from app.main import _start_training
    model_id = mlmr.add_model(owner="es-client")
    with db_session_factory() as db:
        job = _start_training(db, model_id=model_id, group_id=None, producer_id="aimgf:group-retrain")
        db.commit()
        assert job.model_id == model_id


# ---------------------------------------------------------------- the subscription routes that take a subscription id

def _subscription_of(client, model_id) -> str:
    """Creates a subscription on the model as the operator and returns its id."""
    return _subscribe(client, model_id, OPERATOR).json()["subscriptionId"]


def test_another_rapp_cannot_read_report_on_or_delete_a_subscription_of_a_model(client, db_session_factory, mlmr):
    """On a subscription of a model it does not own, an rApp is 403 `MODEL_ACCESS_DENIED` for its reports, for filing a report (which can start a group retrain) and for deleting it; the
    subscription is still there afterwards. The owner reads, reports on and deletes it.
    """
    model_id = mlmr.add_model(owner="es-client")
    sub_id = _subscription_of(client, model_id)
    for resp in (client.get(f"/mlmf/subscriptions/{sub_id}/reports", headers=OTHER),
                 client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.9}, headers=OTHER),
                 client.delete(f"/mlmf/subscriptions/{sub_id}", headers=OTHER)):
        assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    assert _rows(db_session_factory) == 1
    assert client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.9}, headers=OWNER).status_code == 200
    assert client.get(f"/mlmf/subscriptions/{sub_id}/reports", headers=OWNER).json()["total"] == 1
    assert client.delete(f"/mlmf/subscriptions/{sub_id}", headers=OWNER).status_code == 204
    assert _rows(db_session_factory) == 0


def test_the_operator_and_an_unowned_model_pass_on_a_subscription_id(client, db_session_factory, mlmr):
    """The operator (and MDAF, an SMO module) files reports and deletes; an rApp does the same on a subscription of an owner-less model, or of a model MLMR has since deleted."""
    owned = _subscription_of(client, mlmr.add_model(owner="es-client"))
    assert client.post(f"/mlmf/subscriptions/{owned}/reports", json={"accuracy": 0.9}, headers=OPERATOR).status_code == 200
    assert client.get(f"/mlmf/subscriptions/{owned}/reports", headers=None).status_code == 200
    assert client.delete(f"/mlmf/subscriptions/{owned}", headers=OPERATOR).status_code == 204
    unowned = _subscription_of(client, mlmr.add_model())
    assert client.post(f"/mlmf/subscriptions/{unowned}/reports", json={"accuracy": 0.9}, headers=OTHER).status_code == 200
    gone_model = mlmr.add_model(owner="es-client")
    gone = _subscription_of(client, gone_model)
    del mlmr.models[str(gone_model)]                          # MLMR has since deleted the model
    assert client.delete(f"/mlmf/subscriptions/{gone}", headers=OTHER).status_code == 204


def test_an_rapps_subscription_listings_show_only_the_models_it_may_see(client, db_session_factory, mlmr):
    """Without `model_id`, `GET /mlmf/subscriptions` and `GET /mlmf/reports` give an rApp the subscriptions (and reports) of its own models, of owner-less ones and of models MLMR no longer
    knows, and leave out another rApp's; the operator and a call with no role see all.
    """
    mine, theirs, open_model = (mlmr.add_model(owner="es-client"), mlmr.add_model(owner="someone-else"), mlmr.add_model())
    subs = {name: _subscription_of(client, m) for name, m in (("mine", mine), ("theirs", theirs), ("open", open_model))}
    for sub_id in subs.values():
        client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.9}, headers=OPERATOR)

    def seen(path, headers, key):
        """The ids a listing returns under `key`."""
        return {item[key] for item in client.get(path, headers=headers).json()["items"]}

    assert seen("/mlmf/subscriptions", OWNER, "subscriptionId") == {subs["mine"], subs["open"]}
    assert seen("/mlmf/subscriptions", OPERATOR, "subscriptionId") == set(subs.values())
    assert seen("/mlmf/subscriptions", None, "subscriptionId") == set(subs.values())
    assert len(client.get("/mlmf/reports", headers=OWNER).json()["items"]) == 2
    assert len(client.get("/mlmf/reports", headers=OPERATOR).json()["items"]) == 3
    assert client.get("/mlmf/subscriptions", headers=OWNER).json()["total"] == 2
