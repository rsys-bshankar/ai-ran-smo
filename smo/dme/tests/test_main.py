"""Tests for DME (Foundational Platform LLD sections 3.1-3.5).
Run with: pytest smo/dme/tests -q
"""

import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from smo_shared import outbox
from smo_shared.db import Base, get_session
from smo_shared.outbox import NotificationOutbox

from app.main import app
from app.models import (
    DataJob, DataOffer, DataRecord, DmeActionRecord, DMEDeliverySchema, DMEProducer, DMEProducerType, DMEType,
    DMETypeSubscription,
)


class FakeHealthResponse:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def client():
    """A `TestClient` of the DME app on one in-memory SQLite database that holds the DME tables and the outbox table. The engine is kept as `app.state.test_engine` so tests can read what the routes wrote (outbox rows included); the dependency override is removed afterwards.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[DMEProducer.__table__, DMEType.__table__, DMEProducerType.__table__, DMEDeliverySchema.__table__,
                                              DataJob.__table__, DataOffer.__table__, DMETypeSubscription.__table__, DataRecord.__table__,
                                              DmeActionRecord.__table__, NotificationOutbox.__table__])
    TestSession = sessionmaker(bind=engine)
    app.state.test_engine = engine

    def override_get_session():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _default_producer_callbacks_are_harmless(monkeypatch):
    """Every test registers producers at fake hostnames
    (http://ran-nf-oam:8000/...) that only resolve inside the real
    docker-compose network — health checks and job push/stop are all
    best-effort by design (HISTORY.md §5), so defaulting them
    to a harmless, deterministic response keeps every test that doesn't
    care about this behavior fast and stable. Tests that actually
    exercise health/push/stop behavior override this with their own
    monkeypatch.setattr call.
    """
    monkeypatch.setattr("app.main.httpx.get", lambda url, timeout=None: FakeHealthResponse(200))
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: FakeHealthResponse(200))
    monkeypatch.setattr("app.main.httpx.delete", lambda url, timeout=None: FakeHealthResponse(204))


def register_type_body(name="PMCounters", version="1.0.0", **extra):
    return {"namespace": "RAN", "name": name, "version": version, "typeName": f"RAN.{name}",
            "producerId": "ran-nf-oam", "dataProductionSchema": {"type": "object"},
            "producerHealthCallbackUrl": "http://ran-nf-oam:8000/health",
            "jobCallbackUrl": "http://ran-nf-oam:8000/dme-jobs", **extra}


def test_register_dme_type_returns_registration_id(client):
    """Registering a producer and type answers 201 with the type's registration id."""
    resp = client.post("/production-capabilities", json=register_type_body())
    assert resp.status_code == 201
    assert "registrationId" in resp.json()


def test_different_version_is_not_a_conflict(client):
    """A second version of the same type name registers as a new type, not a conflict."""
    client.post("/production-capabilities", json=register_type_body(version="1.0.0"))
    resp = client.post("/production-capabilities", json=register_type_body(version="2.0.0"))
    assert resp.status_code == 201


def test_discover_returns_dme_type_id_struct(client):
    """Foundational Platform LLD section 3.1: dmeTypeIdStruct is computed
    from our internal UUID, matching R1AP's actual wire identity.
    """
    client.post("/production-capabilities", json=register_type_body())
    resp = client.get("/dme-types")
    struct = resp.json()[0]["dmeTypeIdStruct"]
    assert struct == {"namespace": "RAN", "name": "PMCounters", "version": "1.0.0"}


def test_type_status_disabled_when_producer_health_callback_is_unreachable(client, monkeypatch):
    """The actual fix: producerHealthCallbackUrl was stored but never
    called at all, and typeStatus used to be derived from whether a
    DataJob row was ACTIVE — a dead producer with an active job still
    reported ENABLED. ICS's own typeStatus is driven entirely by real
    producer availability (ConsumerController.typeStatus /
    ProducerSupervision's health poll); this mirrors that.
    """
    import httpx as httpx_module

    def raise_error(url, timeout=None):
        raise httpx_module.ConnectError("unreachable")

    monkeypatch.setattr("app.main.httpx.get", raise_error)

    client.post("/production-capabilities", json=register_type_body())
    resp = client.get("/dme-types")
    assert resp.json()[0]["typeStatus"] == "DISABLED"


def test_type_status_disabled_on_a_non_2xx_health_response(client, monkeypatch):
    """A producer whose health callback answers 503 makes the type DISABLED."""
    monkeypatch.setattr("app.main.httpx.get", lambda url, timeout=None: FakeHealthResponse(503))
    client.post("/production-capabilities", json=register_type_body())
    resp = client.get("/dme-types")
    assert resp.json()[0]["typeStatus"] == "DISABLED"


def test_type_status_enabled_when_producer_health_callback_responds(client, monkeypatch):
    """The type is ENABLED when a producer's registered health callback answers, and that registered URL is the one called."""
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return FakeHealthResponse(200)

    monkeypatch.setattr("app.main.httpx.get", fake_get)

    client.post("/production-capabilities", json=register_type_body())
    resp = client.get("/dme-types")
    assert resp.json()[0]["typeStatus"] == "ENABLED"
    assert calls == ["http://ran-nf-oam:8000/health"]  # the registered producerHealthCallbackUrl, genuinely called


def test_type_status_stays_disabled_even_with_an_active_job_if_producer_is_unreachable(client, monkeypatch):
    """The headline regression this fix closes: an ACTIVE DataJob must no
    longer be enough on its own to report ENABLED.
    """
    import httpx as httpx_module

    def raise_error(url, timeout=None):
        raise httpx_module.ConnectError("unreachable")

    monkeypatch.setattr("app.main.httpx.get", raise_error)

    reg = client.post("/production-capabilities", json=register_type_body()).json()
    client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    })
    resp = client.get("/dme-types")
    assert resp.json()[0]["typeStatus"] == "DISABLED"


def test_data_job_rejects_unknown_delivery_method(client):
    """A delivery method that is not a known wire value is refused with 409 DELIVERY_METHOD_NOT_OFFERED."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "CARRIER_PIGEON", "consumerId": "rapp-1",
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "DELIVERY_METHOD_NOT_OFFERED"


def test_data_job_accepts_wire_exact_delivery_values(client):
    """Section 3.2: enum renamed to match R1AP's exact wire values."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    for method in ("PULL_HTTP", "PUSH_HTTP", "STREAMING_KAFKA"):
        resp = client.post("/data-jobs", json={
            "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
            "dataDeliveryMethod": method, "consumerId": "rapp-1",
        })
        assert resp.status_code == 202, method


def test_data_job_rejects_a_definition_violating_the_registered_schema(client):
    """HISTORY.md §5: ICS's own InfoJobs.validateJsonObjectAgainstSchema
    (validatePutInfoJob) — productionJobDefinition used to be accepted as an
    arbitrary dict, never checked against the DmeType's own
    dataProductionSchema.
    """
    reg = client.post("/production-capabilities", json=register_type_body(
        dataProductionSchema={"type": "object", "properties": {"cellId": {"type": "string"}}, "required": ["cellId"]},
    )).json()

    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
        "productionJobDefinition": {"cellId": 42},  # wrong type: schema requires a string
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"


def test_data_job_rejects_a_definition_missing_a_required_field(client):
    """A production definition missing a field the type's schema requires is 422 SCHEMA_VALIDATION_FAILED."""
    reg = client.post("/production-capabilities", json=register_type_body(
        dataProductionSchema={"type": "object", "properties": {"cellId": {"type": "string"}}, "required": ["cellId"]},
    )).json()

    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
        "productionJobDefinition": {},
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"


def test_data_job_accepts_a_definition_matching_the_registered_schema(client):
    """A production definition that satisfies the type's schema creates the job (202)."""
    reg = client.post("/production-capabilities", json=register_type_body(
        dataProductionSchema={"type": "object", "properties": {"cellId": {"type": "string"}}, "required": ["cellId"]},
    )).json()

    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
        "productionJobDefinition": {"cellId": "cell-42"},
    })
    assert resp.status_code == 202


def test_a_data_job_for_an_unregistered_dme_type_is_404(client):
    """SEC-15.10: a dmeTypeId nobody registered is 404 DME_TYPE_NOT_FOUND (it used to be accepted, skipping the schema check and telling no producer), and no job is stored."""
    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": "11111111-1111-1111-1111-111111111111",
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
        "productionJobDefinition": {"anything": "goes"},
    })
    assert resp.status_code == 404 and resp.json()["detail"]["title"] == "DME_TYPE_NOT_FOUND"
    assert client.get("/data-jobs").json()["total"] == 0


def test_update_data_job_rejects_a_definition_violating_the_registered_schema(client):
    """Updating a job checks the new definition against the type's schema too (422)."""
    reg = client.post("/production-capabilities", json=register_type_body(
        dataProductionSchema={"type": "object", "properties": {"cellId": {"type": "string"}}, "required": ["cellId"]},
    )).json()
    created = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1", "productionJobDefinition": {"cellId": "cell-1"},
    }).json()

    resp = client.put(f"/data-jobs/{created['dataJobId']}", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1", "productionJobDefinition": {},
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"


def test_data_offer_commits_to_first_offered_method(client):
    """Section 3.5: producer offers multiple methods, framework commits to one."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    resp = client.post("/offers", json={
        "dmeTypeId": reg["registrationId"], "dataDeliveryMode": "CONTINUOUS",
        "dataDeliveryMethods": ["PUSH_HTTP", "STREAMING_KAFKA"],
        "dataOfferTerminationNotificationUri": "http://producer/terminate",
    })
    assert resp.status_code == 201
    assert resp.json()["committedMethod"] == "PUSH_HTTP"


def test_data_job_rejects_a_method_the_offer_never_committed_to(client):
    """The actual fix: CreateDataJob previously only checked the requested
    method against the global wire-value set, never against what the
    specific DataOffer for this dmeTypeId actually committed to (section
    3.5's own "framework commits to one" decision) — a consumer could
    request STREAMING_KAFKA against a type whose offer only committed to
    PULL_HTTP, and DME accepted it without complaint.
    """
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    client.post("/offers", json={
        "dmeTypeId": reg["registrationId"], "dataDeliveryMode": "CONTINUOUS",
        "dataDeliveryMethods": ["PULL_HTTP"],
        "dataOfferTerminationNotificationUri": "http://producer/terminate",
    })

    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "STREAMING_KAFKA", "consumerId": "rapp-1",
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "DELIVERY_METHOD_NOT_OFFERED"


def test_data_job_accepts_the_offers_committed_method(client):
    """A job asking for the method an offer committed to is accepted."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    client.post("/offers", json={
        "dmeTypeId": reg["registrationId"], "dataDeliveryMode": "CONTINUOUS",
        "dataDeliveryMethods": ["PULL_HTTP"],
        "dataOfferTerminationNotificationUri": "http://producer/terminate",
    })

    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    })
    assert resp.status_code == 202


def test_data_job_unaffected_by_offer_check_when_no_offer_exists(client):
    """Not every DmeType in this build has a DataOffer — the check must
    not regress the existing PULL-only-no-offer case.
    """
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    })
    assert resp.status_code == 202


def test_deregister_producer_leaves_its_types_registered_but_disabled(client):
    """HISTORY.md §7 — DME vs. the real ICS API, Producer/Type conflation
    finding, closed: ICS's own deleteInfoProducer never touches
    info-types at all (those are only ever removed via their own
    DELETE /info-types/{id}, see the delete_dme_type tests below) — this
    used to delete every DMEType a producer registered, which was only
    "correct" because the old schema conflated a type with its one
    producer. TypeB's OTHER producer (rapp-2) keeps it ENABLED even
    after rapp-1 leaves.
    """
    client.post("/production-capabilities", json=register_type_body(name="TypeA", producerId="rapp-1"))
    client.post("/production-capabilities", json=register_type_body(name="TypeB", producerId="rapp-1"))
    client.post("/production-capabilities", json=register_type_body(name="TypeB", producerId="rapp-2"))

    resp = client.delete("/production-capabilities", params={"producer_id": "rapp-1"})
    assert resp.status_code == 204

    remaining = {t["typeName"]: t for t in client.get("/dme-types").json()}
    assert remaining.keys() == {"RAN.TypeA", "RAN.TypeB"}
    assert remaining["RAN.TypeA"]["producerIds"] == []
    assert remaining["RAN.TypeA"]["typeStatus"] == "DISABLED"  # its only producer is gone
    assert remaining["RAN.TypeB"]["producerIds"] == ["rapp-2"]
    assert remaining["RAN.TypeB"]["typeStatus"] == "ENABLED"  # rapp-2 still supports it

    assert client.get("/production-capabilities/rapp-1/status").status_code == 404


def test_deregister_unknown_producer_is_idempotent(client):
    """Deregistering a producer that was never registered is 204, not an error."""
    resp = client.delete("/production-capabilities", params={"producer_id": "never-registered"})
    assert resp.status_code == 204


def test_deregister_producer_does_not_remove_dependent_data_jobs_and_offers(client):
    """The real fix, not a cosmetic rename: a DataJob/DataOffer is
    type-scoped, not producer-scoped (ICS's own InfoJob/InfoJobs model),
    so it must survive its producer's own deregistration — an existing
    or future producer for the same type may still serve it.
    """
    reg = client.post("/production-capabilities", json=register_type_body(producerId="rapp-1")).json()
    job = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    }).json()
    offer = client.post("/offers", json={
        "dmeTypeId": reg["registrationId"], "dataDeliveryMode": "CONTINUOUS",
        "dataDeliveryMethods": ["PULL_HTTP"],
        "dataOfferTerminationNotificationUri": "http://producer/terminate",
    }).json()

    resp = client.delete("/production-capabilities", params={"producer_id": "rapp-1"})
    assert resp.status_code == 204

    assert client.get(f"/data-jobs/{job['dataJobId']}").status_code == 200
    assert client.get(f"/offers/{offer['offerId']}").status_code == 200


def test_reregistering_the_same_type_by_the_same_producer_is_idempotent(client):
    """ICS's own PUT is create-or-update, not create-only — a producer
    re-registering after a restart must not 409 the way this used to
    (DME_TYPE_VERSION_CONFLICT on the old global UniqueConstraint).
    """
    first = client.post("/production-capabilities", json=register_type_body()).json()
    second = client.post("/production-capabilities", json=register_type_body()).json()
    assert second["registrationId"] == first["registrationId"]
    types = client.get("/dme-types").json()
    assert len(types) == 1
    assert types[0]["producerIds"] == ["ran-nf-oam"]


def test_a_second_producer_can_register_an_already_known_type(client):
    """The actual fix: ICS's own model allows several producers per type
    (consumer_information_type.no_of_producers) — this used to be
    structurally impossible, a global UniqueConstraint on
    namespace/name/version regardless of which producer registered it.
    """
    first = client.post("/production-capabilities", json=register_type_body(producerId="ran-nf-oam")).json()
    second = client.post("/production-capabilities", json=register_type_body(producerId="ran-analytics")).json()
    assert second["registrationId"] == first["registrationId"]
    types = client.get("/dme-types").json()
    assert len(types) == 1
    assert types[0]["producerIds"] == ["ran-analytics", "ran-nf-oam"]


def test_list_producers_returns_every_registered_producer(client):
    """The producer list holds every registered producer."""
    client.post("/production-capabilities", json=register_type_body(producerId="ran-nf-oam"))
    client.post("/production-capabilities", json=register_type_body(name="Other", producerId="ran-analytics"))
    ids = {p["producerId"] for p in client.get("/production-capabilities").json()}
    assert ids == {"ran-nf-oam", "ran-analytics"}


def test_get_producer_returns_its_supported_type_ids(client):
    """One producer's detail holds its callback addresses and the ids of the types it supports."""
    reg = client.post("/production-capabilities", json=register_type_body(producerId="ran-nf-oam")).json()
    p = client.get("/production-capabilities/ran-nf-oam").json()
    assert p == {
        "producerId": "ran-nf-oam", "producerHealthCallbackUrl": "http://ran-nf-oam:8000/health",
        "jobCallbackUrl": "http://ran-nf-oam:8000/dme-jobs", "supportedTypeIds": [reg["registrationId"]],
    }


def test_get_unknown_producer_is_404(client):
    """An unknown producer is 404."""
    assert client.get("/production-capabilities/never-registered").status_code == 404


def test_delete_dme_type_rejects_a_type_with_an_active_producer(client):
    """A type that a producer still supports cannot be deleted (409 DME_TYPE_HAS_ACTIVE_PRODUCERS)."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    resp = client.delete(f"/dme-types/{reg['registrationId']}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "DME_TYPE_HAS_ACTIVE_PRODUCERS"


def test_delete_dme_type_succeeds_once_its_last_producer_is_gone_and_notifies_subscribers(client, monkeypatch):
    """Once the last producer has left, the type can be deleted, and the subscribers are told it was DEREGISTERED with its schema.
    """
    notified = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: notified.append(json) or FakeHealthResponse(200))

    client.post("/type-subscriptions", json={"notificationDestination": "http://consumer/dme-type-events", "owner": "consumer-1"})
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    client.delete("/production-capabilities", params={"producer_id": "ran-nf-oam"})
    notified.clear()  # only care about the delete_dme_type notification below

    resp = client.delete(f"/dme-types/{reg['registrationId']}")
    assert resp.status_code == 204
    assert client.get("/dme-types").json() == []
    assert notified == [{"infoTypeId": reg["registrationId"], "jobDataSchema": {"type": "object"}, "status": "DEREGISTERED"}]


def test_delete_dme_type_also_removes_dependent_data_jobs_and_offers(client):
    """Deleting a type also deletes the data jobs (and offers) that depend on it."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    job = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    }).json()
    client.delete("/production-capabilities", params={"producer_id": "ran-nf-oam"})

    resp = client.delete(f"/dme-types/{reg['registrationId']}")
    assert resp.status_code == 204
    assert client.get(f"/data-jobs/{job['dataJobId']}").status_code == 404


def test_delete_unknown_dme_type_is_404(client):
    """Deleting an unknown type is 404 DME_TYPE_NOT_FOUND."""
    resp = client.delete("/dme-types/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json()["detail"]["title"] == "DME_TYPE_NOT_FOUND"


def test_terminate_data_offer_fires_termination_notification(client, monkeypatch):
    """Section 3.5: normal-direction notification on termination —
    framework -> Producer, distinct from the reversed availability
    notification the Producer sends the other way.
    """
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))

    reg = client.post("/production-capabilities", json=register_type_body()).json()
    offer = client.post("/offers", json={
        "dmeTypeId": reg["registrationId"], "dataDeliveryMode": "CONTINUOUS",
        "dataDeliveryMethods": ["PUSH_HTTP"],
        "dataOfferTerminationNotificationUri": "http://producer/terminate",
    }).json()

    client.delete(f"/offers/{offer['offerId']}")
    assert len(calls) == 1
    assert calls[0][0] == "http://producer/terminate"


def test_get_data_job_by_id_returns_its_fields(client):
    """HISTORY.md §5: no GET-by-id for DataJob existed at all."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    created = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    }).json()

    resp = client.get(f"/data-jobs/{created['dataJobId']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dataJobId"] == created["dataJobId"]
    assert body["dmeTypeId"] == reg["registrationId"]
    assert body["dataDeliveryMethod"] == "PULL_HTTP"
    assert body["consumerId"] == "rapp-1"
    assert body["status"] == "ACTIVE"


def test_get_unknown_data_job_is_404(client):
    """An unknown data job is 404."""
    resp = client.get("/data-jobs/11111111-1111-1111-1111-111111111111")
    assert resp.status_code == 404


def test_query_data_job_status_returns_status(client):
    """No job-level status endpoint existed — only the list/GET-by-id
    view, which this build didn't even have until this pass.
    """
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    created = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    }).json()

    resp = client.get(f"/data-jobs/{created['dataJobId']}/status")
    assert resp.status_code == 200
    assert resp.json() == {"dataJobId": created["dataJobId"], "status": "ACTIVE"}


def test_query_unknown_data_job_status_is_404(client):
    """The status of an unknown data job is 404."""
    resp = client.get("/data-jobs/11111111-1111-1111-1111-111111111111/status")
    assert resp.status_code == 404


def test_update_data_job_changes_its_definition(client):
    """HISTORY.md §5: DME had no update-in-place semantics at
    all — only POST-create/DELETE. ICS's own PutIndividualInfoJob.
    """
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    created = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1", "productionJobDefinition": {"v": 1},
    }).json()

    resp = client.put(f"/data-jobs/{created['dataJobId']}", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1", "productionJobDefinition": {"v": 2},
    })
    assert resp.status_code == 200
    assert resp.json()["productionJobDefinition"] == {"v": 2}

    view = client.get(f"/data-jobs/{created['dataJobId']}").json()
    assert view["productionJobDefinition"] == {"v": 2}


def test_update_data_job_rejects_changing_its_target(client):
    """ICS itself rejects changing a job's type mid-update ("Cannot
    modify job type", 409 there) — the equivalent identity fields here
    are dmeTypeId/consumerId/dataDeliveryMode, all fixed at creation.
    """
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    created = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    }).json()

    resp = client.put(f"/data-jobs/{created['dataJobId']}", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-2",
    })
    assert resp.status_code == 400
    assert resp.json()["detail"]["title"] == "DATA_JOB_TARGET_IMMUTABLE"


def test_update_data_job_revalidates_delivery_method_against_the_offer(client):
    """Changing a job's delivery method to one no offer committed to is refused on update (409), as on create."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    client.post("/offers", json={
        "dmeTypeId": reg["registrationId"], "dataDeliveryMode": "CONTINUOUS",
        "dataDeliveryMethods": ["PULL_HTTP"],
        "dataOfferTerminationNotificationUri": "http://producer/terminate",
    })
    created = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    }).json()

    resp = client.put(f"/data-jobs/{created['dataJobId']}", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "STREAMING_KAFKA", "consumerId": "rapp-1",
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "DELIVERY_METHOD_NOT_OFFERED"


def test_update_unknown_data_job_is_404(client):
    """Updating an unknown data job is 404."""
    resp = client.put("/data-jobs/11111111-1111-1111-1111-111111111111", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": "22222222-2222-2222-2222-222222222222",
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    })
    assert resp.status_code == 404


def test_update_data_job_re_pushes_to_the_producer(client, monkeypatch):
    """ICS re-runs startInfoSubscriptionJob on every PUT, new or
    updated — the producer is re-notified with the new job definition.
    """
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))

    reg = client.post("/production-capabilities", json=register_type_body()).json()
    created = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    }).json()
    calls.clear()  # drop the push from create_data_job itself

    client.put(f"/data-jobs/{created['dataJobId']}", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1", "productionJobDefinition": {"v": 2},
    })

    assert len(calls) == 1
    assert calls[0][1]["infoJobData"] == {"v": 2}


def test_get_data_offer_by_id_returns_its_fields(client):
    """HISTORY.md §5: no GET-by-id for DataOffer existed at all."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    created = client.post("/offers", json={
        "dmeTypeId": reg["registrationId"], "dataDeliveryMode": "CONTINUOUS",
        "dataDeliveryMethods": ["PUSH_HTTP", "STREAMING_KAFKA"],
        "dataOfferTerminationNotificationUri": "http://producer/terminate",
    }).json()

    resp = client.get(f"/offers/{created['offerId']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["offerId"] == created["offerId"]
    assert body["dmeTypeId"] == reg["registrationId"]
    assert body["committedMethod"] == "PUSH_HTTP"
    assert body["dataDeliveryMethodsOffered"] == ["PUSH_HTTP", "STREAMING_KAFKA"]


def test_get_unknown_data_offer_is_404(client):
    """An unknown data offer is 404."""
    resp = client.get("/offers/11111111-1111-1111-1111-111111111111")
    assert resp.status_code == 404


def test_discover_filters_by_data_category(client):
    """HISTORY.md §5: data_category was declared as a query
    param but silently never applied — every call returned every type
    regardless of the filter.
    """
    client.post("/production-capabilities", json=register_type_body(name="CoverageIssue"))
    client.post("/production-capabilities", json={
        "namespace": "AIML", "name": "ModelHealth", "version": "1.0.0", "typeName": "AIML.ModelHealth",
        "producerId": "ai-ml-workflow", "dataProductionSchema": {"type": "object"},
        "producerHealthCallbackUrl": "http://ai-ml-workflow:8000/health",
        "jobCallbackUrl": "http://ai-ml-workflow:8000/dme-jobs",
    })

    resp = client.get("/dme-types", params={"data_category": "AIML"})
    names = [t["typeName"] for t in resp.json()]
    assert names == ["AIML.ModelHealth"]


def test_discover_without_data_category_returns_every_type(client):
    """Without a `data_category` the type discovery lists every type."""
    client.post("/production-capabilities", json=register_type_body(name="CoverageIssue"))
    client.post("/production-capabilities", json={
        "namespace": "AIML", "name": "ModelHealth", "version": "1.0.0", "typeName": "AIML.ModelHealth",
        "producerId": "ai-ml-workflow", "dataProductionSchema": {"type": "object"},
        "producerHealthCallbackUrl": "http://ai-ml-workflow:8000/health",
        "jobCallbackUrl": "http://ai-ml-workflow:8000/dme-jobs",
    })

    resp = client.get("/dme-types")
    assert len(resp.json()) == 2


def test_create_data_job_pushes_the_job_to_the_producer(client, monkeypatch):
    """HISTORY.md §5: create_data_job/terminate_data_job only
    ever touched our own DB — ICS's own ProducerCallbacks.startInfoJob
    actually POSTs the job to the producer's jobCallbackUrl
    (ProducerJobInfo's wire shape). This is the actual fix.
    """
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))

    reg = client.post("/production-capabilities", json=register_type_body()).json()
    created = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
        "productionJobDefinition": {"kpi": "throughput"},
    }).json()

    assert len(calls) == 1
    url, body = calls[0]
    assert url == "http://ran-nf-oam:8000/dme-jobs"
    assert body["infoJobIdentity"] == created["dataJobId"]
    assert body["infoTypeIdentity"] == reg["registrationId"]
    assert body["infoJobData"] == {"kpi": "throughput"}
    assert body["owner"] == "rapp-1"
    assert "lastUpdated" in body


def test_create_data_job_succeeds_even_if_the_producer_push_fails(client, monkeypatch):
    """Best-effort, same pattern as every other DME/FOCOM
    notification in this build — an unreachable producer must not fail
    the consumer-facing CreateDataJob call.
    """
    import httpx as httpx_module

    def raise_error(url, json=None, timeout=None):
        raise httpx_module.ConnectError("unreachable")

    monkeypatch.setattr("app.main.httpx.post", raise_error)

    reg = client.post("/production-capabilities", json=register_type_body()).json()
    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    })
    assert resp.status_code == 202


def test_terminate_data_job_stops_the_job_at_the_producer(client, monkeypatch):
    """ICS's own ProducerCallbacks.stopInfoJob — DELETE to
    jobCallbackUrl/{jobId}.
    """
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    created = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    }).json()

    calls = []
    monkeypatch.setattr("app.main.httpx.delete", lambda url, timeout=None: calls.append(url))

    client.delete(f"/data-jobs/{created['dataJobId']}")
    assert calls == [f"http://ran-nf-oam:8000/dme-jobs/{created['dataJobId']}"]


def test_terminate_unknown_data_job_pushes_nothing(client, monkeypatch):
    """Terminating a job that does not exist is 204 and no producer is told."""
    calls = []
    monkeypatch.setattr("app.main.httpx.delete", lambda url, timeout=None: calls.append(url))

    resp = client.delete("/data-jobs/11111111-1111-1111-1111-111111111111")
    assert resp.status_code == 204
    assert calls == []


def test_terminate_data_job_succeeds_even_if_the_producer_stop_fails(client, monkeypatch):
    """Terminating a job answers 204 even when the producer's stop call cannot be delivered; the producers are told through the outbox, not in the request.
    """
    import httpx as httpx_module

    def raise_error(url, timeout=None):
        raise httpx_module.ConnectError("unreachable")

    reg = client.post("/production-capabilities", json=register_type_body()).json()
    created = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    }).json()

    monkeypatch.setattr("app.main.httpx.delete", raise_error)
    resp = client.delete(f"/data-jobs/{created['dataJobId']}")
    assert resp.status_code == 204


def test_terminate_data_jobs_for_consumer_deletes_every_matching_job(client, monkeypatch):
    """HISTORY.md §7's DME vs. real ICS finding: the real
    DELETE /data-consumer/v1/info-jobs?owner=X (ics-api.yaml's own
    deleteJobsForOwner) — every job one consumer owns torn down in one
    call, including the same per-job producer-stop notification
    terminate_data_job already fires, and leaving a different
    consumer's own job alone.
    """
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    job1 = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    }).json()
    job2 = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
    }).json()
    other = client.post("/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-2",
    }).json()

    calls = []
    monkeypatch.setattr("app.main.httpx.delete", lambda url, timeout=None: calls.append(url))

    resp = client.delete("/data-jobs", params={"consumer_id": "rapp-1"})
    assert resp.status_code == 204
    assert sorted(calls) == sorted([
        f"http://ran-nf-oam:8000/dme-jobs/{job1['dataJobId']}", f"http://ran-nf-oam:8000/dme-jobs/{job2['dataJobId']}",
    ])

    assert client.get(f"/data-jobs/{job1['dataJobId']}/status").status_code == 404
    assert client.get(f"/data-jobs/{job2['dataJobId']}/status").status_code == 404
    assert client.get(f"/data-jobs/{other['dataJobId']}/status").status_code == 200


def test_register_dme_type_exposes_job_callback_url_on_its_producer(client):
    """jobCallbackUrl is now a producer-level field (ICS's own
    producer_registration_info), not a type-level one — a type can have
    several producers, each with its own callback URLs.
    """
    client.post("/production-capabilities", json=register_type_body())
    resp = client.get("/production-capabilities/ran-nf-oam")
    assert resp.json()["jobCallbackUrl"] == "http://ran-nf-oam:8000/dme-jobs"


def test_query_producer_status_enabled_when_healthy(client, monkeypatch):
    """HISTORY.md §5: no producer-status endpoint existed at
    all — ICS's own GET .../info-producers/{id}/status.
    """
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return FakeHealthResponse(200)

    monkeypatch.setattr("app.main.httpx.get", fake_get)
    client.post("/production-capabilities", json=register_type_body(producerId="ran-nf-oam"))

    resp = client.get("/production-capabilities/ran-nf-oam/status")
    assert resp.status_code == 200
    assert resp.json() == {"producerId": "ran-nf-oam", "operationalState": "ENABLED"}
    assert calls == ["http://ran-nf-oam:8000/health"]


def test_query_producer_status_disabled_when_unreachable(client, monkeypatch):
    """A producer whose health callback cannot be reached is reported DISABLED (200, not an error)."""
    import httpx as httpx_module

    def raise_error(url, timeout=None):
        raise httpx_module.ConnectError("unreachable")

    monkeypatch.setattr("app.main.httpx.get", raise_error)
    client.post("/production-capabilities", json=register_type_body(producerId="ran-nf-oam"))

    resp = client.get("/production-capabilities/ran-nf-oam/status")
    assert resp.status_code == 200
    assert resp.json()["operationalState"] == "DISABLED"


def test_query_producer_status_for_unknown_producer_is_404(client):
    """The status of a producer that was never registered is 404."""
    resp = client.get("/production-capabilities/never-registered/status")
    assert resp.status_code == 404


def test_query_producer_status_after_deregistration_is_404(client):
    """After deregistration the producer's status is 404."""
    client.post("/production-capabilities", json=register_type_body(producerId="ran-nf-oam"))
    client.delete("/production-capabilities", params={"producer_id": "ran-nf-oam"})

    resp = client.get("/production-capabilities/ran-nf-oam/status")
    assert resp.status_code == 404


def test_subscribe_and_unsubscribe_type_changes(client):
    """HISTORY.md §5: ICS's own `/info-type-subscription` — a
    consumer notified whenever any DmeType is registered or removed.
    Entirely absent from this build until this pass.
    """
    sub = client.post("/type-subscriptions", json={"notificationDestination": "http://consumer/type-changes", "owner": "sa-smos"}).json()
    assert "subscriptionId" in sub

    resp = client.delete(f"/type-subscriptions/{sub['subscriptionId']}")
    assert resp.status_code == 204


def test_unsubscribe_unknown_type_subscription_is_idempotent(client):
    """Unsubscribing an unknown subscription is 204."""
    resp = client.delete("/type-subscriptions/11111111-1111-1111-1111-111111111111")
    assert resp.status_code == 204


def test_get_type_subscription_by_id_returns_its_fields(client):
    """A subscription is returned with its destination and owner."""
    created = client.post("/type-subscriptions", json={"notificationDestination": "http://consumer/type-changes", "owner": "sa-smos"}).json()

    resp = client.get(f"/type-subscriptions/{created['subscriptionId']}")
    assert resp.status_code == 200
    assert resp.json() == {"subscriptionId": created["subscriptionId"], "notificationDestination": "http://consumer/type-changes", "owner": "sa-smos"}


def test_get_unknown_type_subscription_is_404(client):
    """An unknown subscription is 404."""
    resp = client.get("/type-subscriptions/11111111-1111-1111-1111-111111111111")
    assert resp.status_code == 404


def test_list_type_subscriptions_filters_by_owner(client):
    """The subscription list can be limited to one owner."""
    client.post("/type-subscriptions", json={"notificationDestination": "http://sa-smos/type-changes", "owner": "sa-smos"})
    client.post("/type-subscriptions", json={"notificationDestination": "http://nfo/type-changes", "owner": "nfo"})

    resp = client.get("/type-subscriptions", params={"owner": "nfo"})
    assert [s["owner"] for s in resp.json()["items"]] == ["nfo"]


def test_register_dme_type_notifies_subscribers(client, monkeypatch):
    """The headline fix — ICS's own ConsumerCallbacks.notifyTypeRegistered.
    Unfiltered: every subscriber hears about every type registration,
    matching the reference's own lack of per-type scoping.
    """
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))
    client.post("/type-subscriptions", json={"notificationDestination": "http://consumer/type-changes", "owner": "sa-smos"})

    reg = client.post("/production-capabilities", json=register_type_body()).json()

    assert len(calls) == 1
    assert calls[0][0] == "http://consumer/type-changes"
    assert calls[0][1]["infoTypeId"] == reg["registrationId"]
    assert calls[0][1]["jobDataSchema"] == {"type": "object"}
    assert calls[0][1]["status"] == "REGISTERED"


def test_deleting_a_types_last_producer_notifies_subscribers_per_type(client, monkeypatch):
    """ICS's own ConsumerCallbacks.notifyTypeRemoved fires from
    deleteInfoType, not from a producer's own deregistration (which
    never touches info-types at all) — fired once per DmeType actually
    deleted, here via delete_dme_type after rapp-1 is its last producer.
    """
    reg_a = client.post("/production-capabilities", json=register_type_body(name="TypeA", producerId="rapp-1")).json()
    reg_b = client.post("/production-capabilities", json=register_type_body(name="TypeB", producerId="rapp-1")).json()

    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))
    client.post("/type-subscriptions", json={"notificationDestination": "http://consumer/type-changes", "owner": "sa-smos"})

    client.delete("/production-capabilities", params={"producer_id": "rapp-1"})
    client.delete(f"/dme-types/{reg_a['registrationId']}")
    client.delete(f"/dme-types/{reg_b['registrationId']}")

    assert len(calls) == 2
    assert {c[1]["status"] for c in calls} == {"DEREGISTERED"}


def test_register_dme_type_does_not_notify_when_no_subscribers(client, monkeypatch):
    """With no subscribers, registering a type sends no notification."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))

    client.post("/production-capabilities", json=register_type_body())

    assert calls == []


def test_register_dme_type_succeeds_even_if_a_subscriber_is_unreachable(client, monkeypatch):
    """Registration answers 201 even when a subscriber's address cannot be reached, because the notification is only queued in the same transaction.
    """
    import httpx as httpx_module

    def raise_error(url, json=None, timeout=None):
        raise httpx_module.ConnectError("unreachable")

    monkeypatch.setattr("app.main.httpx.post", raise_error)
    client.post("/type-subscriptions", json={"notificationDestination": "http://consumer/type-changes", "owner": "sa-smos"})

    resp = client.post("/production-capabilities", json=register_type_body())
    assert resp.status_code == 201  # must not raise despite the unreachable subscriber


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """GUI pass: the BFF's /modules/status probes /<module>/health on every module."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


# ---------------------------------------------------------------- list reads (GUI pass 2)

def test_list_data_jobs_filters_by_type_and_consumer(client):
    """The job list can be limited by type and by consumer."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    other = client.post("/production-capabilities", json=register_type_body(name="Other")).json()
    job = client.post("/data-jobs", json={"dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
                                          "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1"}).json()
    client.post("/data-jobs", json={"dataDeliveryMode": "ONE_TIME", "dmeTypeId": other["registrationId"],
                                    "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-2"})

    assert client.get("/data-jobs").json()["total"] == 2
    by_type = client.get("/data-jobs", params={"dme_type_id": reg["registrationId"]}).json()["items"]
    assert [j["dataJobId"] for j in by_type] == [job["dataJobId"]]
    assert [j["consumerId"] for j in client.get("/data-jobs", params={"consumer_id": "rapp-2"}).json()["items"]] == ["rapp-2"]


def test_list_data_offers_filters_by_type(client):
    """The offer list can be limited by type."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    offer = client.post("/offers", json={"dmeTypeId": reg["registrationId"], "dataDeliveryMode": "CONTINUOUS",
                                          "dataDeliveryMethods": ["PUSH_HTTP"],
                                          "dataOfferTerminationNotificationUri": "http://producer/terminate"}).json()
    listed = client.get("/offers").json()["items"]
    assert [o["offerId"] for o in listed] == [offer["offerId"]]
    assert client.get("/offers", params={"dme_type_id": "00000000-0000-0000-0000-000000000000"}).json()["items"] == []


# ---------------------------------------------------------------- Wave 3: source provenance + lifecycle eligibility

def test_register_dme_type_rejects_unknown_source_domain(client):
    """A source domain other than LIVE_RAN or DIGITAL_TWIN is 422 SCHEMA_VALIDATION_FAILED."""
    resp = client.post("/production-capabilities", json=register_type_body(sourceDomain="SOMEWHERE_ELSE"))
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"


def test_register_dme_type_round_trips_source_provenance(client):
    """The source domain and source context given at registration are shown in the type view."""
    reg = client.post("/production-capabilities", json=register_type_body(
        sourceDomain="DIGITAL_TWIN", sourceContext={"vendor": "acme", "instance": "dt-1"},
    )).json()
    view = client.get("/dme-types").json()[0]
    assert view["dmeTypeId"] == reg["registrationId"]
    assert view["sourceDomain"] == "DIGITAL_TWIN"
    assert view["sourceContext"] == {"vendor": "acme", "instance": "dt-1"}


def test_data_job_rejects_unknown_lifecycle_stage(client):
    """A lifecycle stage that is not a known stage is 422 SCHEMA_VALIDATION_FAILED."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1", "lifecycleStage": "REHEARSAL",
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"


def test_data_job_rejects_digital_twin_source_for_inference(client):
    """A job for INFERENCE on a Digital Twin type is 422 DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE."""
    reg = client.post("/production-capabilities", json=register_type_body(sourceDomain="DIGITAL_TWIN")).json()
    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1", "lifecycleStage": "INFERENCE",
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE"


def test_data_job_accepts_digital_twin_source_for_training_and_emulation(client):
    """A Digital Twin type may feed TRAINING and EMULATION jobs."""
    reg = client.post("/production-capabilities", json=register_type_body(sourceDomain="DIGITAL_TWIN")).json()
    for stage in ("TRAINING", "EMULATION"):
        resp = client.post("/data-jobs", json={
            "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
            "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1", "lifecycleStage": stage,
        })
        assert resp.status_code == 202, stage


def test_data_job_accepts_live_ran_source_for_inference(client):
    """A live-RAN type may feed INFERENCE jobs."""
    reg = client.post("/production-capabilities", json=register_type_body(sourceDomain="LIVE_RAN")).json()
    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1", "lifecycleStage": "INFERENCE",
    })
    assert resp.status_code == 202


def test_data_job_unaffected_by_eligibility_check_for_a_type_with_no_declared_source_domain(client):
    """A type that declared no source domain is not subject to the Digital Twin rule, even for INFERENCE."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    resp = client.post("/data-jobs", json={
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1", "lifecycleStage": "INFERENCE",
    })
    assert resp.status_code == 202


def test_update_data_job_also_enforces_digital_twin_inference_eligibility(client):
    """Updating a job to INFERENCE on a Digital Twin type is refused, so the rule cannot be bypassed after creation."""
    reg = client.post("/production-capabilities", json=register_type_body(sourceDomain="DIGITAL_TWIN")).json()
    job = client.post("/data-jobs", json={"dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
                                           "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1",
                                           "lifecycleStage": "TRAINING"}).json()
    resp = client.put(f"/data-jobs/{job['dataJobId']}", json={
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": reg["registrationId"],
        "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1", "lifecycleStage": "INFERENCE",
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE"


# ---------------------------------------------------------------- Wave 3: real data-plane store

def test_ingest_and_fetch_data_records(client):
    """A payload ingested for a job is returned by the records read with its job id."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    job = client.post("/data-jobs", json={"dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
                                           "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1"}).json()
    ingest = client.post(f"/data-jobs/{job['dataJobId']}/records", json={"payload": {"kpi": 12.5}})
    assert ingest.status_code == 201
    assert "recordId" in ingest.json()

    records = client.get(f"/data-jobs/{job['dataJobId']}/records").json()["items"]
    assert len(records) == 1
    assert records[0]["payload"] == {"kpi": 12.5}
    assert records[0]["dataJobId"] == job["dataJobId"]


def test_ingest_data_record_for_unknown_job_is_404(client):
    """Ingesting a record for an unknown job is 404."""
    resp = client.post("/data-jobs/00000000-0000-0000-0000-000000000000/records", json={"payload": {}})
    assert resp.status_code == 404


def test_fetch_data_records_for_unknown_job_is_404(client):
    """Reading the records of an unknown job is 404."""
    resp = client.get("/data-jobs/00000000-0000-0000-0000-000000000000/records")
    assert resp.status_code == 404


def test_fetch_data_records_respects_limit(client):
    """The records read honours `limit`."""
    reg = client.post("/production-capabilities", json=register_type_body()).json()
    job = client.post("/data-jobs", json={"dataDeliveryMode": "CONTINUOUS", "dmeTypeId": reg["registrationId"],
                                           "dataDeliveryMethod": "PULL_HTTP", "consumerId": "rapp-1"}).json()
    for i in range(3):
        client.post(f"/data-jobs/{job['dataJobId']}/records", json={"payload": {"i": i}})
    assert len(client.get(f"/data-jobs/{job['dataJobId']}/records", params={"limit": 2}).json()["items"]) == 2


# ---------------------------------------------------------------- Wave 3: O1 action mediation

class FakeRanNfOam:
    """A minimal double for ran-nf-oam's own POST /config-jobs — enough
    to prove DME's /actions route mediates and forwards, without
    duplicating ran-nf-oam's own test coverage of NETCONF dispatch
    (ran-nf-oam/tests/test_main.py already covers that in depth).
    """

    def __init__(self):
        self.received: list[dict] = []
        self.refuse_with: tuple[int, dict] | None = None

    def post(self, path, json=None, **kw):
        assert path == "/ran-nf-oam/config-jobs"
        self.received.append(json)
        if self.refuse_with:
            return FakeConfigJobResponse(self.refuse_with[1], self.refuse_with[0])
        return FakeConfigJobResponse({"jobId": "11111111-1111-1111-1111-111111111111", "status": "PROCESSING"})


class FakeConfigJobResponse:
    def __init__(self, payload, status_code=202):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture
def ran_nf_oam(monkeypatch):
    """Replaces the R1 client's POST with `FakeRanNfOam`, which records the configuration jobs DME forwards and can be told to refuse them.
    """
    fake = FakeRanNfOam()
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: fake.post(path, json=json, **kw))
    return fake


def test_mediate_action_forwards_to_ran_nf_oam_and_records_provenance(client, ran_nf_oam):
    """An action is forwarded to RAN NF OAM as a configuration job with its changes (class name included) and the default scope, and is recorded with its managed element, class, source context and the job id.
    """
    resp = client.post("/actions", json={
        "requestedBy": "energy-optimizer",
        "changes": [{"managedElementRef": "me-1", "className": "GNBDUFunction", "attributeChanges": {"txPower": 10}}],
        "sourceContext": {"vendor": "acme"},
    })
    assert resp.status_code == 202
    body = resp.json()
    assert body["forwardedJobId"] == "11111111-1111-1111-1111-111111111111"
    assert body["status"] == "PROCESSING"

    forwarded = ran_nf_oam.received[0]
    assert forwarded["scope"] == "single-ME"
    # Wave 9: className is forwarded, for RAN NF OAM's schema pre-check
    assert forwarded["changes"] == [{"managedElementRef": "me-1", "className": "GNBDUFunction", "attributeChanges": {"txPower": 10}}]

    action = client.get(f"/actions/{body['actionId']}").json()
    assert action["managedElementRef"] == "me-1"
    assert action["className"] == "GNBDUFunction"
    assert action["sourceContext"] == {"vendor": "acme"}
    assert action["forwardedJobId"] == "11111111-1111-1111-1111-111111111111"


def test_mediate_action_records_a_change_whose_managed_element_is_null(client, ran_nf_oam):
    """A change whose managed element is null does not crash the route; the action is recorded with an empty element reference.
    """
    resp = client.post("/actions", json={"requestedBy": "r", "changes": [{"managedElementRef": None}]})
    assert resp.status_code != 500
    assert client.get(f"/actions/{resp.json()['actionId']}").json()["managedElementRef"] == ""


def test_mediate_action_surfaces_a_ran_nf_oam_precheck_refusal(client, ran_nf_oam):
    """Wave 9 (W9-02): a write RAN NF OAM's pre-check refuses (here a
    schema violation) reaches the rApp as that same 4xx; the action is
    recorded REJECTED, never forwarded."""
    problem = {"title": "SCHEMA_VALIDATION_FAILED", "status": 422, "detail": "attribute txPower is not defined"}
    ran_nf_oam.refuse_with = (422, {"detail": problem})
    resp = client.post("/actions", json={"requestedBy": "rapp", "changes": [
        {"managedElementRef": "me-1", "className": "GNBDUFunction", "attributeChanges": {"txPower": 10}}]})
    assert resp.status_code == 422 and resp.json()["detail"] == problem
    action = client.get("/actions").json()["items"][0]
    assert (action["status"], action["forwardedJobId"]) == ("REJECTED", None)


def test_mediate_action_answers_502_when_ran_nf_oam_fails_without_a_usable_body(client, ran_nf_oam):
    """Found by the contract test: RAN NF OAM answering a bare 500 made DME fail on `resp.json()` and answer 500 itself."""
    ran_nf_oam.refuse_with = (500, {})
    resp = client.post("/actions", json={"requestedBy": "rapp", "changes": [
        {"managedElementRef": "me-1", "className": "GNBDUFunction", "attributeChanges": {"txPower": 10}}]})
    assert resp.status_code == 502 and resp.json()["detail"]["title"] == "UPSTREAM_FAILED"
    action = client.get("/actions").json()["items"][0]
    assert (action["status"], action["forwardedJobId"]) == ("REJECTED", None)


def test_mediate_action_rejects_empty_changes(client, ran_nf_oam):
    """An action with no changes is 422 and nothing is forwarded."""
    resp = client.post("/actions", json={"requestedBy": "energy-optimizer", "changes": []})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"
    assert ran_nf_oam.received == []


def test_get_unknown_action_is_404(client):
    """An unknown action is 404."""
    resp = client.get("/actions/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_the_decision_context_is_forwarded_with_the_action_id_and_absent_when_not_given(client, ran_nf_oam):
    """PR-AI-13: why the rApp acts goes to RAN NF OAM, which keeps it as the decision record of the job."""
    action_id = "22222222-2222-2222-2222-222222222222"
    client.post("/actions", json={"requestedBy": "r", "actionId": action_id, "changes": [{"managedElementRef": "me-1"}],
                                  "decision": {"inputsRef": "dme://jobs/1", "modelVersion": "m 1.0", "rationale": "low load"}})
    client.post("/actions", json={"requestedBy": "r", "changes": [{"managedElementRef": "me-2"}]})
    assert ran_nf_oam.received[0]["decision"] == {"inputsRef": "dme://jobs/1", "modelVersion": "m 1.0", "rationale": "low load", "actionId": action_id}
    assert "decision" not in ran_nf_oam.received[1]


def test_an_action_held_for_approval_is_recorded_as_waiting_with_no_job(client, monkeypatch):
    """PR-AI-11: RAN NF OAM answers PENDING_APPROVAL with no jobId; the action is recorded as waiting, and the rApp follows `approvalId`."""
    fake = FakeRanNfOam()
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeConfigJobResponse(
        {"status": "PENDING_APPROVAL", "jobId": None, "approvalId": "33333333-3333-3333-3333-333333333333"}))
    resp = client.post("/actions", json={"requestedBy": "r", "changes": [{"managedElementRef": "me-1"}]})
    assert resp.status_code == 202
    assert resp.json() == {"actionId": resp.json()["actionId"], "forwardedJobId": None, "status": "PENDING_APPROVAL",
                           "approvalId": "33333333-3333-3333-3333-333333333333"}
    action = client.get(f"/actions/{resp.json()['actionId']}").json()
    assert action["status"] == "PENDING_APPROVAL" and action["forwardedJobId"] is None
    assert fake.received == []


def test_list_actions_filters_by_managed_element_ref(client, ran_nf_oam):
    """The action list can be limited to one managed element."""
    client.post("/actions", json={"requestedBy": "rapp-1", "changes": [{"managedElementRef": "me-1"}]})
    client.post("/actions", json={"requestedBy": "rapp-2", "changes": [{"managedElementRef": "me-2"}]})
    listed = client.get("/actions", params={"managed_element_ref": "me-1"}).json()["items"]
    assert [a["requestedBy"] for a in listed] == ["rapp-1"]


def test_a_replayed_action_id_is_ignored_not_forwarded_twice(client, ran_nf_oam):
    """Wave 10.1 (W10-18, TC29): a caller-supplied actionId is an
    idempotency key; the correlation id of the causing request is kept."""
    action_id = str(uuid.uuid4())
    body = {"requestedBy": "es-rapp", "actionId": action_id,
            "changes": [{"managedElementRef": "me-1", "attributeChanges": {"administrativeState": "LOCKED"}}]}
    first = client.post("/actions", json=body, headers={"X-Correlation-ID": "exec-42"})
    assert first.status_code == 202 and first.json()["actionId"] == action_id
    replay = client.post("/actions", json=body)
    assert replay.status_code == 200
    assert replay.json() == {"actionId": action_id, "status": "IGNORED", "originalStatus": "PROCESSING",
                             "forwardedJobId": "11111111-1111-1111-1111-111111111111"}
    assert len(ran_nf_oam.received) == 1
    assert client.get(f"/actions/{action_id}").json()["correlationId"] == "exec-42"


# ---------------------------------------------------------------- notifications through the outbox (PR-MSG-1.5)

def _outbox_rows(client):
    with sessionmaker(bind=app.state.test_engine)() as db:
        return db.query(NotificationOutbox).order_by(NotificationOutbox.created_at).all()


def test_a_type_registration_notification_survives_a_crash_between_commit_and_send(client, monkeypatch):
    """The crash test of MSG-1.5: the subscriber's notification is a committed row, sent after the commit; if the process dies
    before sending, a later drain delivers it, so a registered type is never silently un-announced."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")           # the process dies right after the commit
    monkeypatch.setenv("MODULE", "dme")                              # what the container sets
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)) or FakeHealthResponse(200))
    client.post("/type-subscriptions", json={"notificationDestination": "http://consumer/type-changes", "owner": "sa-smos"})

    reg = client.post("/production-capabilities", json=register_type_body()).json()

    assert calls == []
    pending = _outbox_rows(client)
    assert [(r.module, r.status, r.destination) for r in pending] == [("dme", "PENDING", "http://consumer/type-changes")]
    assert pending[0].payload["infoTypeId"] == reg["registrationId"] and pending[0].payload["status"] == "REGISTERED"

    monkeypatch.delenv("SMO_OUTBOX_INLINE_DRAIN")                    # the restarted process sweeps
    assert outbox.drain(app.state.test_engine)["sent"] == 1
    assert [(u, j["status"]) for u, j in calls] == [("http://consumer/type-changes", "REGISTERED")]
    assert _outbox_rows(client)[0].status == "SENT"


def test_a_job_push_to_producers_is_one_row_per_producer_and_sent_after_commit(client, monkeypatch):
    """Creating a job writes one outbox row per producer of the type, to that producer's job callback, and sends nothing until the outbox is drained after the commit.
    """
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-1"))
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-2", jobCallbackUrl="http://rapp-2:8000/jobs"))
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)) or FakeHealthResponse(200))
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    with sessionmaker(bind=app.state.test_engine)() as db:
        type_id = db.query(DMEType).one().dme_type_id
    resp = client.post("/data-jobs", json={"dataDeliveryMode": "ONE_TIME", "dmeTypeId": str(type_id), "consumerId": "rapp-x",
                                           "productionJobDefinition": {}, "dataDeliveryMethod": "PUSH_HTTP", "deliveryDetails": {}})
    assert resp.status_code == 202, resp.text
    assert calls == []                                               # nothing goes out before the drain
    rows = _outbox_rows(client)
    assert sorted(r.destination for r in rows) == ["http://ran-nf-oam:8000/dme-jobs", "http://rapp-2:8000/jobs"]
    assert {r.payload["infoJobIdentity"] for r in rows} == {resp.json()["dataJobId"]}


def test_nothing_is_announced_when_the_change_does_not_commit(client, monkeypatch):
    """The other half: a change that rolls back used to have told its subscribers already; now it leaves no row and sends nothing."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)) or FakeHealthResponse(200))
    client.post("/type-subscriptions", json={"notificationDestination": "http://consumer/type-changes", "owner": "sa-smos"})

    from sqlalchemy.orm import Session as OrmSession
    real_commit = OrmSession.commit

    def failing_commit(self):
        if self.info.get("outbox_pending_ids"):
            self.rollback()
            raise RuntimeError("the database refused the commit")
        return real_commit(self)

    monkeypatch.setattr(OrmSession, "commit", failing_commit)
    resp = TestClient(app, raise_server_exceptions=False).post("/production-capabilities", json=register_type_body())
    monkeypatch.setattr(OrmSession, "commit", real_commit)

    assert resp.status_code == 500
    assert calls == [] and _outbox_rows(client) == []
    with sessionmaker(bind=app.state.test_engine)() as db:
        assert db.query(DMEType).count() == 0                        # the registration itself rolled back too


def test_stopping_a_job_at_the_producers_is_a_delete_row_in_the_same_transaction_and_survives_a_crash(client, monkeypatch):
    """MSG-1.10: the producer's stop-job DELETE was an inline call after the commit, lost if the process died in between."""
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-1"))
    client.post("/production-capabilities", json=register_type_body(producerId="rapp-2", jobCallbackUrl="http://rapp-2:8000/jobs"))
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: FakeHealthResponse(200))
    with sessionmaker(bind=app.state.test_engine)() as db:
        type_id = db.query(DMEType).one().dme_type_id
    job = client.post("/data-jobs", json={"dataDeliveryMode": "ONE_TIME", "dmeTypeId": str(type_id), "consumerId": "rapp-x",
                                          "productionJobDefinition": {}, "dataDeliveryMethod": "PUSH_HTTP", "deliveryDetails": {}}).json()
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")           # the process dies right after the commit
    monkeypatch.setenv("MODULE", "dme")
    deleted = []
    monkeypatch.setattr("smo_shared.webhook.delete_webhook", lambda destination, timeout=5.0: deleted.append(destination) or FakeHealthResponse(204))
    before = len(_outbox_rows(client))

    assert client.delete(f"/data-jobs/{job['dataJobId']}").status_code == 204

    assert deleted == []                                             # nothing went out before the drain
    stops = [r for r in _outbox_rows(client)[before:] if r.method == "DELETE"]
    assert sorted(r.destination for r in stops) == sorted([f"http://ran-nf-oam:8000/dme-jobs/{job['dataJobId']}",
                                                           f"http://rapp-2:8000/jobs/{job['dataJobId']}"])
    assert {r.status for r in stops} == {"PENDING"} and {r.payload == {} for r in stops} == {True}

    monkeypatch.delenv("SMO_OUTBOX_INLINE_DRAIN")                    # the restarted process sweeps
    outbox.drain(app.state.test_engine)
    assert sorted(deleted) == sorted(r.destination for r in stops)
    assert {r.status for r in _outbox_rows(client) if r.method == "DELETE"} == {"SENT"}


def test_an_offer_with_no_delivery_method_is_refused(client):
    """An offer with an empty delivery method list is 409 DELIVERY_METHOD_NOT_OFFERED."""
    type_id = client.post("/production-capabilities", json=register_type_body()).json()["registrationId"]
    resp = client.post("/offers", json={"dmeTypeId": type_id, "dataDeliveryMode": "CONTINUOUS",
                                        "dataDeliveryMethods": [], "dataOfferTerminationNotificationUri": "http://producer/terminate"})
    assert resp.status_code == 409
