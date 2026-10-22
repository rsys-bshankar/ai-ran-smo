"""Tests of call flow 12 (DOC-CF-1.8): a data job, its records and an offer are their owners'. An rApp changes and deletes only the jobs it created, sees
only the jobs it created, consumes or produces for, delivers records only to a job of a type it produces, terminates only its own offers; a NO_PRODUCER job
takes no record; records page by a cursor; a type with INFERENCE jobs cannot become a Digital Twin; a deleted job is announced; a bulk delete is all or
nothing.

Fixtures and builders come from `test_main.py` (the app on SQLite with the outbox table); the SMO event table is added where a test reads events.
Run: `cd smo/dme && PYTHONPATH=.:../shared python -m pytest tests/test_data_ownership.py -q`.
"""

import uuid

from sqlalchemy.orm import sessionmaker

from smo_shared import events

from app.main import app
from app.models import DataRecord
from test_main import client, register_type_body  # noqa: F401  (pytest fixtures)


def rapp(invoker):
    """The headers R1 Termination stamps on an rApp's call."""
    return {"X-R1-Role": "rapp", "X-R1-Invoker-Id": invoker}


CONSOLE = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-bff"}


def _type(client, producer="producer-rapp", **extra):
    """Registers `producer` (an rApp, under its own id) for a type and returns the type id."""
    resp = client.post("/production-capabilities", json=register_type_body(producerId=producer, **extra), headers=rapp(producer))
    assert resp.status_code == 201, resp.text
    return resp.json()["registrationId"]


def _job(client, type_id, owner="consumer-rapp", stage=None):
    """Creates a job as the rApp `owner`, naming itself as consumer; returns its id."""
    resp = client.post("/data-jobs", headers=rapp(owner), json={"dataDeliveryMode": "CONTINUOUS", "dmeTypeId": type_id, "consumerId": owner,
                                                                "dataDeliveryMethod": "PULL_HTTP", "lifecycleStage": stage})
    assert resp.status_code == 202, resp.text
    return resp.json()["dataJobId"]


def _title(resp):
    """The error code of a refusal."""
    return resp.json()["detail"]["title"]


# ---------------------------------------------------------------- 12a: a job is its creator's

def test_another_rapp_cannot_change_or_delete_a_job(client):
    """Call flow 12a: another rApp's PUT is 404 (it cannot see the job); the producer, which sees it, is 403 DATA_JOB_NOT_OWNER; the creator changes and
    deletes it."""
    job = _job(client, _type(client))
    body = {"dataDeliveryMode": "CONTINUOUS", "dmeTypeId": None, "consumerId": "consumer-rapp", "dataDeliveryMethod": "PULL_HTTP",
            "deliveryDetails": {"targetUri": "http://elsewhere/"}}
    body["dmeTypeId"] = client.get(f"/data-jobs/{job}").json()["dmeTypeId"]
    assert client.put(f"/data-jobs/{job}", json=body, headers=rapp("rogue-rapp")).status_code == 404
    refused = client.put(f"/data-jobs/{job}", json=body, headers=rapp("producer-rapp"))
    assert refused.status_code == 403 and _title(refused) == "DATA_JOB_NOT_OWNER"
    assert client.delete(f"/data-jobs/{job}", headers=rapp("producer-rapp")).status_code == 403
    assert client.get(f"/data-jobs/{job}").json()["deliveryDetails"] == {}
    assert client.put(f"/data-jobs/{job}", json=body, headers=rapp("consumer-rapp")).status_code == 200
    assert client.delete(f"/data-jobs/{job}", headers=rapp("consumer-rapp")).status_code == 204
    assert client.get(f"/data-jobs/{job}").status_code == 404


def test_an_rapp_sees_only_the_jobs_it_creates_consumes_or_produces_for(client):
    """Call flow 12a: the list, the job and its status are scoped for an rApp; the console sees every job; an unseen job is a 404, not a 403."""
    type_id = _type(client)
    mine, theirs = _job(client, type_id, owner="consumer-rapp"), _job(client, type_id, owner="other-rapp")
    ids = lambda headers: {j["dataJobId"] for j in client.get("/data-jobs", headers=headers).json()["items"]}  # noqa: E731
    assert ids(rapp("consumer-rapp")) == {mine}
    assert ids(rapp("producer-rapp")) == {mine, theirs}                  # the producer delivers to both
    assert ids(rapp("rogue-rapp")) == set()
    assert ids(CONSOLE) == {mine, theirs}
    for path in (f"/data-jobs/{theirs}", f"/data-jobs/{theirs}/status", f"/data-jobs/{theirs}/records"):
        assert client.get(path, headers=rapp("consumer-rapp")).status_code == 404


def test_a_bulk_delete_by_consumer_is_all_or_nothing_for_an_rapp(client):
    """Call flow 12a: an rApp deleting a consumer's jobs is refused (403, none deleted) when any of them is another's; its own go in one transaction."""
    type_id = _type(client)
    _job(client, type_id, owner="consumer-rapp")
    client.post("/data-jobs", headers=rapp("other-rapp"), json={"dataDeliveryMode": "CONTINUOUS", "dmeTypeId": type_id, "consumerId": "consumer-rapp",
                                                                "dataDeliveryMethod": "PULL_HTTP"})
    refused = client.delete("/data-jobs", params={"consumer_id": "consumer-rapp"}, headers=rapp("consumer-rapp"))
    assert refused.status_code == 403 and len(client.get("/data-jobs").json()["items"]) == 2
    assert client.delete("/data-jobs", params={"consumer_id": "consumer-rapp"}, headers=CONSOLE).status_code == 204
    assert client.get("/data-jobs").json()["items"] == []


def test_a_deleted_job_is_announced(client):
    """Call flow 12a: deleting a job publishes `data-job.state-changed` to DELETED (before: only a deleted type's jobs were)."""
    events.SmoEvent.__table__.create(app.state.test_engine, checkfirst=True)
    job = _job(client, _type(client))
    client.delete(f"/data-jobs/{job}", headers=rapp("consumer-rapp"))
    with sessionmaker(bind=app.state.test_engine)() as db:
        assert [(e.subject, e.data["to"], e.data["reason"]) for e in db.query(events.SmoEvent).all()] == [(job, "DELETED", "deleted by its owner")]


# ---------------------------------------------------------------- 12b: records

def test_only_a_producer_of_the_type_delivers_and_not_to_a_job_without_one(client):
    """Call flow 12b: an rApp that is not a producer of the job's type is 403 DME_NOT_A_PRODUCER (a consumer cannot forge its own data); the producer and
    the console deliver; a job whose type lost its producer is 409 DATA_JOB_NO_PRODUCER."""
    job = _job(client, _type(client))
    refused = client.post(f"/data-jobs/{job}/records", json={"payload": {"v": 1}}, headers=rapp("consumer-rapp"))
    assert refused.status_code == 403 and _title(refused) == "DME_NOT_A_PRODUCER"
    assert client.post(f"/data-jobs/{job}/records", json={"payload": {"v": 1}}, headers=rapp("producer-rapp")).status_code == 201
    assert client.post(f"/data-jobs/{job}/records", json={"payload": {"v": 2}}, headers=CONSOLE).status_code == 201
    client.delete("/production-capabilities", params={"producer_id": "producer-rapp"}, headers=rapp("producer-rapp"))
    waiting = client.post(f"/data-jobs/{job}/records", json={"payload": {"v": 3}}, headers=CONSOLE)
    assert waiting.status_code == 409 and _title(waiting) == "DATA_JOB_NO_PRODUCER"


def test_records_page_by_a_cursor_that_does_not_drift(client):
    """Call flow 12b: `before` pages newest first and `after` oldest first; a record that arrives between two pages does not repeat one; a cursor of
    another job is 404; both cursors at once are 422."""
    job = _job(client, _type(client))
    ids = [client.post(f"/data-jobs/{job}/records", json={"payload": {"n": n}}, headers=CONSOLE).json()["recordId"] for n in range(5)]
    first = client.get(f"/data-jobs/{job}/records", params={"limit": 2}).json()["items"]
    assert [r["payload"]["n"] for r in first] == [4, 3]
    client.post(f"/data-jobs/{job}/records", json={"payload": {"n": 5}}, headers=CONSOLE)       # arrives between the pages
    second = client.get(f"/data-jobs/{job}/records", params={"limit": 2, "before": first[-1]["recordId"]}).json()["items"]
    assert [r["payload"]["n"] for r in second] == [2, 1]
    tail = client.get(f"/data-jobs/{job}/records", params={"after": ids[2]}).json()["items"]
    assert [r["payload"]["n"] for r in tail] == [3, 4, 5]
    assert client.get(f"/data-jobs/{job}/records", params={"after": str(uuid.uuid4())}).status_code == 404
    assert client.get(f"/data-jobs/{job}/records", params={"after": ids[0], "before": ids[1]}).status_code == 422


def test_records_of_one_instant_keep_one_order(client):
    """Call flow 12b: records with the same `produced_at` are ordered by id, so a cursor between them neither skips nor repeats one."""
    job = _job(client, _type(client))
    ids = [client.post(f"/data-jobs/{job}/records", json={"payload": {"n": n}}, headers=CONSOLE).json()["recordId"] for n in range(3)]
    with sessionmaker(bind=app.state.test_engine)() as db:
        same = db.get(DataRecord, uuid.UUID(ids[0])).produced_at
        for r in db.query(DataRecord).all():
            r.produced_at = same
        db.commit()
    newest_first = [r["recordId"] for r in client.get(f"/data-jobs/{job}/records").json()["items"]]
    rest = [r["recordId"] for r in client.get(f"/data-jobs/{job}/records", params={"before": newest_first[0]}).json()["items"]]
    assert rest == newest_first[1:] and sorted(newest_first) == sorted(ids)


# ---------------------------------------------------------------- 12c: offers

def _offer(client, headers):
    """Creates an offer on a fresh type as the caller in `headers`; returns its id."""
    return client.post("/offers", headers=headers, json={"dmeTypeId": _type(client), "dataDeliveryMode": "CONTINUOUS", "dataDeliveryMethods": ["PULL_HTTP"],
                                                         "dataOfferTerminationNotificationUri": "http://producer/offer-ended"}).json()["offerId"]


def test_an_rapp_terminates_only_its_own_offer_and_notify_of_an_unknown_offer_is_404(client):
    """Call flow 12c: another rApp's termination is 403 DATA_OFFER_NOT_OWNER and the offer stays; its creator ends it; notify of an unknown offer is 404
    DATA_OFFER_NOT_FOUND (a 409 DME_TYPE_VERSION_CONFLICT before)."""
    offer = _offer(client, rapp("producer-rapp"))
    refused = client.delete(f"/offers/{offer}", headers=rapp("rogue-rapp"))
    assert refused.status_code == 403 and _title(refused) == "DATA_OFFER_NOT_OWNER"
    assert client.get(f"/offers/{offer}").status_code == 200
    assert client.delete(f"/offers/{offer}", headers=rapp("producer-rapp")).status_code == 204
    unknown = client.post(f"/offers/{uuid.uuid4()}/notify", json={})
    assert unknown.status_code == 404 and _title(unknown) == "DATA_OFFER_NOT_FOUND"


# ---------------------------------------------------------------- 12d: eligibility

def test_a_type_with_inference_jobs_cannot_become_a_digital_twin(client):
    """Call flow 12d: redefining a LIVE_RAN type as DIGITAL_TWIN while it has an INFERENCE job is 409 DME_TYPE_HAS_INFERENCE_JOBS and nothing changes;
    once the job is gone, it may."""
    type_id = _type(client, sourceDomain="LIVE_RAN")
    job = _job(client, type_id, stage="INFERENCE")
    redefine = register_type_body(producerId="producer-rapp", sourceDomain="DIGITAL_TWIN")
    refused = client.post("/production-capabilities", json=redefine, headers=rapp("producer-rapp"))
    assert refused.status_code == 409 and _title(refused) == "DME_TYPE_HAS_INFERENCE_JOBS"
    assert client.get(f"/dme-types/{type_id}").json()["sourceDomain"] == "LIVE_RAN"
    client.delete(f"/data-jobs/{job}", headers=rapp("consumer-rapp"))
    assert client.post("/production-capabilities", json=redefine, headers=rapp("producer-rapp")).status_code == 201
