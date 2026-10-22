"""Tests of call flow 11 (DOC-CF-1.8): a producer that joins a type is sent its jobs, a producer that leaves is told to stop them and a job left with no producer
is NO_PRODUCER, a moved job callback is sent the jobs again, an rApp may not delete a type and a deleted type tells its jobs' owners and its offers, the producer
health answer is remembered, and two first registrations at once do not fail.

Fixtures and builders come from `test_main.py` (the app on SQLite with the outbox table); the SMO event table is added where a test reads events.
Run: `cd smo/dme && PYTHONPATH=.:../shared python -m pytest tests/test_producer_lifecycle.py -q`.
"""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from smo_shared import events

from app import main
from app.main import app
from app.models import DataJob, DMEType
from test_main import FakeHealthResponse, _outbox_rows, client, register_type_body  # noqa: F401  (pytest fixtures)

RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-1"}


def _type_id():
    """The one registered type's id."""
    with sessionmaker(bind=app.state.test_engine)() as db:
        return str(db.query(DMEType).one().dme_type_id)


def _job(client, type_id):
    """Creates a job on `type_id` and returns its id."""
    resp = client.post("/data-jobs", json={"dataDeliveryMode": "CONTINUOUS", "dmeTypeId": type_id, "consumerId": "consumer-rapp",
                                           "dataDeliveryMethod": "PUSH_HTTP"})
    assert resp.status_code == 202, resp.text
    return resp.json()["dataJobId"]


def _events(client):
    """The SMO events DME published, as (subject, from, to)."""
    with sessionmaker(bind=app.state.test_engine)() as db:
        return [(e.subject, e.data["from"], e.data["to"]) for e in db.query(events.SmoEvent).order_by(events.SmoEvent.seq).all()]


def _with_events(client):
    """Adds the SMO event table to the test database."""
    events.SmoEvent.__table__.create(app.state.test_engine, checkfirst=True)


def test_a_producer_that_joins_is_sent_the_open_jobs(client, monkeypatch):
    """Call flow 11a: a second producer joining a type gets one start per open job of the type, at its own job callback (before: none, until the next job)."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-1", jobCallbackUrl="http://rapp-1/jobs"))
    job_id = _job(client, _type_id())
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-2", jobCallbackUrl="http://rapp-2/jobs"))
    starts = [(r.destination, r.payload["infoJobIdentity"]) for r in _outbox_rows(client) if r.destination == "http://rapp-2/jobs"]
    assert starts == [("http://rapp-2/jobs", job_id)]


def test_registering_again_with_the_same_callback_sends_nothing_new(client, monkeypatch):
    """Call flow 11a: a producer's idempotent re-registration (same callback, already linked) sends no job again."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-1", jobCallbackUrl="http://rapp-1/jobs"))
    _job(client, _type_id())
    before = len(_outbox_rows(client))
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-1", jobCallbackUrl="http://rapp-1/jobs"))
    assert len(_outbox_rows(client)) == before


def test_a_moved_job_callback_is_sent_the_jobs_of_every_type_it_serves(client, monkeypatch):
    """Call flow 11a: a producer that re-registers one of its types with a new job callback is sent the jobs of all its types there."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    client.post("/production-capabilities", json=register_type_body(name="A", producerId="rapp-1", jobCallbackUrl="http://old/jobs"))
    client.post("/production-capabilities", json=register_type_body(name="B", producerId="rapp-1", jobCallbackUrl="http://old/jobs"))
    with sessionmaker(bind=app.state.test_engine)() as db:
        ids = [str(t.dme_type_id) for t in db.query(DMEType).order_by(DMEType.name).all()]
    jobs = {_job(client, ids[0]), _job(client, ids[1])}
    client.post("/production-capabilities", json=register_type_body(name="A", producerId="rapp-1", jobCallbackUrl="http://new/jobs"))
    assert {r.payload["infoJobIdentity"] for r in _outbox_rows(client) if r.destination == "http://new/jobs"} == jobs


def test_a_leaving_producer_is_told_to_stop_and_a_job_with_no_producer_waits(client, monkeypatch):
    """Call flow 11d: deregistering the only producer writes a stop (DELETE) per job to it and the job becomes NO_PRODUCER, with an event; a producer that
    joins later is sent the job and it is ACTIVE again."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    _with_events(client)
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-1", jobCallbackUrl="http://rapp-1/jobs"))
    job_id = _job(client, _type_id())
    assert client.delete("/production-capabilities", params={"producer_id": "rapp-1"}).status_code == 204
    stops = [r.destination for r in _outbox_rows(client) if r.method == "DELETE"]
    assert stops == [f"http://rapp-1/jobs/{job_id}"]
    assert client.get(f"/data-jobs/{job_id}/status").json()["status"] == "NO_PRODUCER"

    client.post("/production-capabilities", json=register_type_body(producerId="rapp-2", jobCallbackUrl="http://rapp-2/jobs"))
    assert client.get(f"/data-jobs/{job_id}/status").json()["status"] == "ACTIVE"
    assert [r.payload["infoJobIdentity"] for r in _outbox_rows(client) if r.destination == "http://rapp-2/jobs"] == [job_id]
    assert _events(client) == [(job_id, "ACTIVE", "NO_PRODUCER"), (job_id, "NO_PRODUCER", "ACTIVE")]


def test_a_job_on_a_type_another_producer_still_serves_stays_active(client):
    """Call flow 11d: one of two producers leaving leaves the job ACTIVE."""
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-1", jobCallbackUrl="http://rapp-1/jobs"))
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-2", jobCallbackUrl="http://rapp-2/jobs"))
    job_id = _job(client, _type_id())
    client.delete("/production-capabilities", params={"producer_id": "rapp-1"})
    assert client.get(f"/data-jobs/{job_id}/status").json()["status"] == "ACTIVE"


def test_a_job_on_a_type_nobody_serves_starts_as_no_producer(client):
    """Call flow 11b: a job created while the type has no producer is NO_PRODUCER, not ACTIVE."""
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-1"))
    type_id = _type_id()
    client.delete("/production-capabilities", params={"producer_id": "rapp-1"})
    assert client.get(f"/data-jobs/{_job(client, type_id)}/status").json()["status"] == "NO_PRODUCER"


def test_an_rapp_may_not_delete_a_type(client):
    """Call flow 11d: an rApp deleting a type, even one with no producer left, is 403 ROLE_NOT_PERMITTED and the type stays."""
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-1"))
    type_id = _type_id()
    client.delete("/production-capabilities", params={"producer_id": "rapp-1"})
    resp = client.delete(f"/dme-types/{type_id}", headers=RAPP)
    assert resp.status_code == 403 and resp.json()["detail"]["title"] == "ROLE_NOT_PERMITTED"
    assert client.get(f"/dme-types/{type_id}").status_code == 200


def test_a_deleted_type_announces_its_jobs_and_tells_its_offers(client, monkeypatch):
    """Call flow 11d: deleting a type publishes a DELETED event per job and writes the termination notice of each offer (before: both deleted silently)."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    _with_events(client)
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-1"))
    type_id = _type_id()
    offer = client.post("/offers", json={"dmeTypeId": type_id, "dataDeliveryMode": "CONTINUOUS", "dataDeliveryMethods": ["PUSH_HTTP"],
                                         "dataOfferTerminationNotificationUri": "http://producer/offer-ended"}).json()["offerId"]
    job_id = _job(client, type_id)
    client.delete("/production-capabilities", params={"producer_id": "rapp-1"})
    assert client.delete(f"/dme-types/{type_id}", headers={"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-bff"}).status_code == 204
    assert (job_id, "NO_PRODUCER", "DELETED") in _events(client)
    assert [r.payload for r in _outbox_rows(client) if r.destination == "http://producer/offer-ended"] == [{"dataOfferId": offer}]
    assert client.get(f"/data-jobs/{job_id}").status_code == 404


def test_the_health_answer_is_remembered_and_forgotten_on_reregistration(client, monkeypatch):
    """Call flow 11c: with the cache on, a list of two types served by one producer asks it once, and a second read asks nothing; a re-registration forgets it."""
    monkeypatch.setenv("DME_PRODUCER_HEALTH_CACHE_SECONDS", "30")
    main._health_cache.clear()
    asked = []
    monkeypatch.setattr("app.main.httpx.get", lambda url, timeout=None, **kw: asked.append(url) or FakeHealthResponse(200))
    client.post("/production-capabilities", json=register_type_body(name="A", producerId="rapp-1", producerHealthCallbackUrl="http://rapp-1/health"))
    client.post("/production-capabilities", json=register_type_body(name="B", producerId="rapp-1", producerHealthCallbackUrl="http://rapp-1/health"))
    assert [t["typeStatus"] for t in client.get("/dme-types").json()] == ["ENABLED", "ENABLED"]
    client.get("/dme-types")
    assert asked == ["http://rapp-1/health"]
    client.post("/production-capabilities", json=register_type_body(name="A", producerId="rapp-1", producerHealthCallbackUrl="http://rapp-1/health"))
    client.get("/production-capabilities/rapp-1/status")
    assert asked == ["http://rapp-1/health", "http://rapp-1/health"]
    main._health_cache.clear()


def test_a_bad_cache_setting_falls_back_to_thirty_seconds(monkeypatch):
    """Call flow 11c: a value that is not a number is 30 s, a negative one 0."""
    monkeypatch.setenv("DME_PRODUCER_HEALTH_CACHE_SECONDS", "soon")
    assert main._health_cache_seconds() == 30.0
    monkeypatch.setenv("DME_PRODUCER_HEALTH_CACHE_SECONDS", "-5")
    assert main._health_cache_seconds() == 0.0


def test_a_registration_that_loses_a_race_is_retried(client, monkeypatch):
    """Call flow 11a: when a concurrent first registration commits first (an IntegrityError), the registration is retried once and answers 201 (a 500 before)."""
    real = main._register
    calls = []

    def racing(db, request, body):
        """Fails like a lost race the first time."""
        calls.append(1)
        if len(calls) == 1:
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        return real(db, request, body)
    monkeypatch.setattr("app.main._register", racing)
    resp = client.post("/production-capabilities", json=register_type_body())
    assert resp.status_code == 201 and len(calls) == 2
    with sessionmaker(bind=app.state.test_engine)() as db:
        assert db.query(DataJob).count() == 0 and uuid.UUID(resp.json()["registrationId"])
