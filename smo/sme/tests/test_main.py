"""Unit tests of the SME service (`app/main.py`): service publish and discovery, providers, invoker onboarding, tokens and introspection, trusted invokers, event
subscriptions and their delivery through the outbox, the registry listings and the stale-invoker purge (Foundational Platform LLD sections 2.1-2.3).

Fixtures, defined here and reused by `test_security.py`, `test_scope.py` and `test_enrollment.py` (which import them): `db_session_factory` builds an in-memory SQLite
database holding only SME's tables plus the outbox and idempotency-key tables; `client` is a FastAPI `TestClient` with `get_session` overridden to use it and the three
`KNOWN_TEST_PUBLISHERS` already enrolled as providers. `conftest.py` additionally opens enrollment for every test (`SME_ALLOW_OPEN_ENROLLMENT`).

Event delivery is observed by replacing `app.main.httpx.post` (the global `httpx.post`, which `smo_shared.webhook` calls) with a recorder, or by reading the outbox table.

Run: `cd smo/sme && PYTHONPATH=.:../shared python -m pytest tests -q` (or one file: `tests/test_main.py`). Needs no Postgres, no network and no stub server.
"""

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from smo_shared import outbox
from smo_shared.db import Base, get_session
from smo_shared.idempotency import IdempotencyKey
from smo_shared.outbox import NotificationOutbox

from app.main import app
from app.models import (InvokerRegistration, IssuedAccessToken, ProviderRegistration, ServiceAuthzPolicy, ServiceEventSubscription,
                        ServiceProfile, TrustedInvoker, UsedClientAssertion)

# Every test in this file registers services under one of these three
# identities — pre-enrolling them here (via the real POST
# /provider-registrations route, not a data shortcut) keeps every existing
# register_service call site unchanged now that it requires a registered
# publishing function (HISTORY.md §5), the same way a test suite
# logs in a fixture user once rather than re-testing login in every test.
KNOWN_TEST_PUBLISHERS = ["rapp-1", "rapp-2", "rapp-3"]


@pytest.fixture
def db_session_factory():
    """Builds an in-memory SQLite database (one shared connection, so every session sees the same data) with SME's eight tables, the notification outbox and the idempotency-key table, and returns a session factory.

    Tests may assume the tables are empty and that no other module's tables exist. Tests use the factory directly to inspect or edit rows behind the API's back.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        ServiceProfile.__table__, ServiceAuthzPolicy.__table__, ServiceEventSubscription.__table__, ProviderRegistration.__table__,
        InvokerRegistration.__table__, IssuedAccessToken.__table__, TrustedInvoker.__table__, UsedClientAssertion.__table__,
        NotificationOutbox.__table__, IdempotencyKey.__table__,
    ])
    return sessionmaker(bind=engine)


@pytest.fixture
def client(db_session_factory):
    """Returns a `TestClient` of the SME app whose database dependency uses `db_session_factory`, with `rapp-1`, `rapp-2` and `rapp-3` already enrolled as providers.

    Why the providers: `register_service` needs a prior enrolment, and enrolling here keeps the tests about something else from repeating it. The dependency override is removed
    when the test ends.
    """
    # Same contract as `smo_shared.db.get_session` (yield a session, always close it), but on the in-memory test database.
    def override_get_session():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    # `app` is a module-level object shared by all tests, so the override is set per test and cleared in the teardown below.
    app.dependency_overrides[get_session] = override_get_session
    test_client = TestClient(app)
    for apf_id in KNOWN_TEST_PUBLISHERS:
        test_client.post("/provider-registrations", json={"apfId": apf_id})
    yield test_client
    app.dependency_overrides.clear()


def register_body(service_name="training-status", producer="rapp-1", **extra):
    """Returns a minimal valid body for registering a service; `extra` overrides or adds fields (for example `allowedConsumers`, `aefProfiles`). The body's `producerId` is not read by SME; the URL's apf_id decides."""
    return {"serviceName": service_name, "producerId": producer, "endpoint": "http://rapp-1/svc",
            "version": "1.0", "moduleScope": "ai-ml-workflow", **extra}


def test_register_service_returns_service_id(client):
    """A registered publisher gets 201 and a `serviceId` back."""
    resp = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body())
    assert resp.status_code == 201
    assert "serviceId" in resp.json()


def test_duplicate_service_name_different_producer_is_conflict(client):
    """A service name already held by another producer is refused with 409 `SERVICE_NAME_CONFLICT` (LLD section 2.3: the name is unique across producers, not per producer)."""
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(producer="rapp-1"))
    resp = client.post("/published-apis/v1/rapp-2/service-apis", json=register_body(producer="rapp-2"))
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "SERVICE_NAME_CONFLICT"


def test_same_producer_can_reregister_same_service_name(client):
    """The same producer registering the same name again updates it in place: same `serviceId`, one row, new field values, not a conflict and not a duplicate."""
    first = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(version="1.0")).json()
    second = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(version="2.0")).json()
    assert first["serviceId"] == second["serviceId"]

    listing = client.get("/published-apis/v1/rapp-1/service-apis").json()
    assert len(listing) == 1
    assert listing[0]["version"] == "2.0"


def test_discovery_hides_service_from_unauthorized_consumer(client):
    """Discovery gate (LLD section 2.2): a consumer not in `allowedConsumers` gets an empty list, never learning the service exists."""
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(allowedConsumers=["rapp-2"]))
    resp = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "rapp-3"})
    assert resp.json() == []


def test_discovery_shows_service_to_authorized_consumer(client):
    """A consumer listed in `allowedConsumers` discovers the service."""
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(allowedConsumers=["rapp-2"]))
    resp = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "rapp-2"})
    assert len(resp.json()) == 1


def test_discovery_shows_service_with_no_authz_restriction(client):
    """A service with an empty `allowedConsumers` is open: any invoker id discovers it."""
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body())
    resp = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "anyone"})
    assert len(resp.json()) == 1


def test_subscribe_events_rejects_unknown_event_type(client):
    """An unknown event type is refused with 422 `SUBSCRIPTION_SCOPE_CONFLICT`, so a typo is not stored as a subscription that never fires."""
    resp = client.post("/capif-events/v1/rapp-1/subscriptions", json={
        "subscriberId": "rapp-1", "eventTypes": ["NOT_A_REAL_EVENT"], "callbackUri": "http://rapp-1/cb",
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SUBSCRIPTION_SCOPE_CONFLICT"


def test_subscribe_events_accepts_known_event_types(client):
    """Known service event types are accepted (201)."""
    resp = client.post("/capif-events/v1/rapp-1/subscriptions", json={
        "subscriberId": "rapp-1", "eventTypes": ["SERVICE_API_AVAILABLE", "SERVICE_API_UPDATE"], "callbackUri": "http://rapp-1/cb",
    })
    assert resp.status_code == 201


def test_a_repeated_event_subscription_with_the_same_key_subscribes_once(client, db_session_factory):
    """SDK-RETRY-IDEMPOTENCY: an SDK retry of POST /capif-events/v1/{subscriberId}/subscriptions gets the first subscriptionId back instead of a second
    subscription that would deliver every event twice; the same key with another body, or under another subscriber id, is 422 and stores nothing."""
    body = {"subscriberId": "rapp-1", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://rapp-1/cb"}
    first = client.post("/capif-events/v1/rapp-1/subscriptions", json=body, headers={"Idempotency-Key": "sub-1"})
    again = client.post("/capif-events/v1/rapp-1/subscriptions", json=body, headers={"Idempotency-Key": "sub-1"})
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    for path, other in (("/capif-events/v1/rapp-1/subscriptions", {**body, "eventTypes": ["SERVICE_API_UPDATE"]}),
                        ("/capif-events/v1/rapp-2/subscriptions", {**body, "subscriberId": "rapp-2"})):
        reused = client.post(path, json=other, headers={"Idempotency-Key": "sub-1"})
        assert reused.status_code == 422 and reused.json()["detail"]["title"] == "IDEMPOTENCY_KEY_REUSED"
    with db_session_factory() as session:
        assert session.query(ServiceEventSubscription).count() == 1


def test_deregister_removes_service(client):
    """Deleting a service as its producer removes it from the producer's own listing."""
    reg = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body()).json()
    client.delete(f"/published-apis/v1/rapp-1/service-apis/{reg['serviceId']}")
    resp = client.get("/published-apis/v1/rapp-1/service-apis")
    assert resp.json() == []


def test_deregister_unknown_service_is_idempotent(client):
    """Deleting an unknown service id answers 204, not 404 (idempotent delete)."""
    resp = client.delete(f"/published-apis/v1/rapp-1/service-apis/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_deregister_by_wrong_producer_is_a_silent_noop(client):
    """A producer cannot delete another producer's service: the call answers 204 like an unknown id (the `producer_id == apf_id` guard) and the service stays."""
    reg = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body()).json()

    resp = client.delete(f"/published-apis/v1/rapp-2/service-apis/{reg['serviceId']}")
    assert resp.status_code == 204  # silent no-op, not an error — same shape as an unknown id

    still_there = client.get("/published-apis/v1/rapp-1/service-apis").json()
    assert len(still_there) == 1


def test_discover_services_filters_by_api_name(client):
    """The `api_name` filter returns only the service with that name."""
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(service_name="training-status"))
    client.post("/published-apis/v1/rapp-2/service-apis", json=register_body(service_name="coverage-analysis", producer="rapp-2"))

    resp = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "anyone", "api_name": "coverage-analysis"})
    names = [s["serviceName"] for s in resp.json()]
    assert names == ["coverage-analysis"]


def test_discover_services_filters_by_api_version(client):
    """The `api_version` filter returns only services of that version."""
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(version="1.0"))
    client.post("/published-apis/v1/rapp-2/service-apis", json=register_body(service_name="other-service", producer="rapp-2", version="2.0"))

    resp = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "anyone", "api_version": "2.0"})
    versions = [s["version"] for s in resp.json()]
    assert versions == ["2.0"]


def test_register_service_stores_and_exposes_aef_profiles(client):
    """`aefProfiles`, `apiSuppFeats` and `shareableInfo` sent at registration are stored and come back unchanged in the discovery view."""
    aef_profiles = [{
        "aefId": "aef-1", "protocol": "HTTP_2", "dataFormat": "JSON",
        "versions": [{"apiVersion": "v1", "resources": [{"resourceName": "counters", "commType": "REQUEST_RESPONSE"}]}],
    }]
    resp = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(
        aefProfiles=aef_profiles, apiSuppFeats="1f", shareableInfo={"isShareable": True, "capifProvDoms": ["domain-a"]},
    ))
    service_id = resp.json()["serviceId"]

    view = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "anyone"}).json()[0]
    assert view["serviceId"] == service_id
    assert view["aefProfiles"] == aef_profiles
    assert view["apiSuppFeats"] == "1f"
    assert view["shareableInfo"] == {"isShareable": True, "capifProvDoms": ["domain-a"]}


def test_discover_services_filters_by_aef_id(client):
    """The `aef_id` filter keeps only services with an `aefProfiles` entry of that AEF."""
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(
        service_name="watched", aefProfiles=[{"aefId": "aef-1", "versions": []}],
    ))
    client.post("/published-apis/v1/rapp-2/service-apis", json=register_body(
        service_name="other", producer="rapp-2", aefProfiles=[{"aefId": "aef-2", "versions": []}],
    ))

    resp = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "anyone", "aef_id": "aef-1"})
    names = [s["serviceName"] for s in resp.json()]
    assert names == ["watched"]


def test_discover_services_filters_by_protocol_and_data_format(client):
    """The `protocol` and `data_format` filters each select the service whose AEF profile has that value."""
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(
        service_name="http-service", aefProfiles=[{"aefId": "aef-1", "protocol": "HTTP_2", "dataFormat": "JSON", "versions": []}],
    ))
    client.post("/published-apis/v1/rapp-2/service-apis", json=register_body(
        service_name="other-service", producer="rapp-2", aefProfiles=[{"aefId": "aef-2", "protocol": "HTTP_1_1", "dataFormat": "XML", "versions": []}],
    ))

    by_protocol = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "anyone", "protocol": "HTTP_2"})
    assert [s["serviceName"] for s in by_protocol.json()] == ["http-service"]

    by_data_format = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "anyone", "data_format": "XML"})
    assert [s["serviceName"] for s in by_data_format.json()] == ["other-service"]


def test_discover_services_filters_by_comm_type_across_nested_resources(client):
    """The `comm_type` filter looks inside `versions[].resources[]` of the AEF profiles, so a service is found by a resource nested two levels down."""
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(
        service_name="streaming-service", aefProfiles=[{
            "aefId": "aef-1", "versions": [{"apiVersion": "v1", "resources": [{"resourceName": "r1", "commType": "SUBSCRIBE_NOTIFY"}]}],
        }],
    ))
    client.post("/published-apis/v1/rapp-2/service-apis", json=register_body(
        service_name="request-service", producer="rapp-2", aefProfiles=[{
            "aefId": "aef-2", "versions": [{"apiVersion": "v1", "resources": [{"resourceName": "r1", "commType": "REQUEST_RESPONSE"}]}],
        }],
    ))

    resp = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "anyone", "comm_type": "SUBSCRIBE_NOTIFY"})
    assert [s["serviceName"] for s in resp.json()] == ["streaming-service"]


def test_discover_services_with_no_aef_filters_returns_services_without_aef_profiles(client):
    """A service registered without any `aefProfiles` is still discoverable when no AEF-level filter is given (the filters must not hide profile-less services)."""
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body())

    resp = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "anyone"})
    assert len(resp.json()) == 1


def test_unsubscribe_events_removes_subscription(client, db_session_factory):
    """Unsubscribing as the owner answers 204 and deletes the subscription row."""
    sub = client.post("/capif-events/v1/rapp-1/subscriptions", json={
        "subscriberId": "rapp-1", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://rapp-1/cb",
    }).json()

    resp = client.delete(f"/capif-events/v1/rapp-1/subscriptions/{sub['subscriptionId']}")
    assert resp.status_code == 204

    with db_session_factory() as session:
        assert session.get(ServiceEventSubscription, uuid.UUID(sub["subscriptionId"])) is None


def test_unsubscribe_unknown_subscription_is_idempotent(client):
    """Unsubscribing an unknown id answers 204."""
    resp = client.delete(f"/capif-events/v1/rapp-1/subscriptions/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_unsubscribe_by_wrong_subscriber_is_a_silent_noop(client, db_session_factory):
    """A subscription cannot be deleted through another subscriber's URL: 204, and the row remains."""
    sub = client.post("/capif-events/v1/rapp-1/subscriptions", json={
        "subscriberId": "rapp-1", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://rapp-1/cb",
    }).json()

    resp = client.delete(f"/capif-events/v1/rapp-2/subscriptions/{sub['subscriptionId']}")
    assert resp.status_code == 204  # silent no-op, not an error

    with db_session_factory() as session:
        assert session.get(ServiceEventSubscription, uuid.UUID(sub["subscriptionId"])) is not None


def test_register_service_notifies_only_matching_subscribers(client, monkeypatch):
    """Registering a service delivers SERVICE_API_AVAILABLE to a subscriber of that event type and not to one that subscribed only to SERVICE_API_UPDATE."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/capif-events/v1/rapp-2/subscriptions", json={
        "subscriberId": "rapp-2", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://rapp-2/cb",
    })
    client.post("/capif-events/v1/rapp-3/subscriptions", json={
        "subscriberId": "rapp-3", "eventTypes": ["SERVICE_API_UPDATE"], "callbackUri": "http://rapp-3/cb",
    })
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body())

    assert len(calls) == 1
    assert calls[0][0] == "http://rapp-2/cb"
    assert calls[0][1]["eventType"] == "SERVICE_API_AVAILABLE"


def test_reregister_service_notifies_with_update_event_type(client, monkeypatch):
    """Re-registering an existing service sends SERVICE_API_UPDATE, not AVAILABLE."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/capif-events/v1/rapp-2/subscriptions", json={
        "subscriberId": "rapp-2", "eventTypes": ["SERVICE_API_AVAILABLE", "SERVICE_API_UPDATE"], "callbackUri": "http://rapp-2/cb",
    })
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(version="1.0"))
    calls.clear()  # discard the create-time SERVICE_API_AVAILABLE notification

    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(version="2.0"))

    assert len(calls) == 1
    assert calls[0][1]["eventType"] == "SERVICE_API_UPDATE"


def test_deregister_service_notifies_with_unavailable_event_type(client, monkeypatch):
    """Deleting a service sends SERVICE_API_UNAVAILABLE (the row is still live when the event is built)."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/capif-events/v1/rapp-2/subscriptions", json={
        "subscriberId": "rapp-2", "eventTypes": ["SERVICE_API_UNAVAILABLE"], "callbackUri": "http://rapp-2/cb",
    })
    reg = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body()).json()
    calls.clear()  # discard the create-time notification (different event type anyway)

    client.delete(f"/published-apis/v1/rapp-1/service-apis/{reg['serviceId']}")

    assert len(calls) == 1
    assert calls[0][1]["eventType"] == "SERVICE_API_UNAVAILABLE"


def test_deregister_by_wrong_producer_does_not_notify(client, monkeypatch):
    """A refused (wrong-producer) delete notifies nobody: nothing changed, so nothing is announced."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/capif-events/v1/rapp-2/subscriptions", json={
        "subscriberId": "rapp-2", "eventTypes": ["SERVICE_API_UNAVAILABLE"], "callbackUri": "http://rapp-2/cb",
    })
    reg = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body()).json()
    calls.clear()

    client.delete(f"/published-apis/v1/rapp-2/service-apis/{reg['serviceId']}")  # wrong producer — silent no-op

    assert calls == []


def test_subscription_with_api_ids_filter_only_notifies_for_a_matching_service(client, monkeypatch):
    """An `apiIds` filter limits notifications to that service's events: another service's events (any type) and the watched service's other event types do not reach the subscriber."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    watched = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(service_name="watched-service")).json()
    client.post("/capif-events/v1/rapp-2/subscriptions", json={
        "subscriberId": "rapp-2", "eventTypes": ["SERVICE_API_UPDATE"], "callbackUri": "http://rapp-2/cb",
        "apiIds": [watched["serviceId"]],
    })

    client.post("/published-apis/v1/rapp-3/service-apis", json=register_body(service_name="other-service"))  # AVAILABLE, wrong event type anyway
    client.post("/published-apis/v1/rapp-3/service-apis", json=register_body(service_name="other-service", version="2.0"))  # UPDATE, wrong apiId
    assert calls == []

    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(service_name="watched-service", version="2.0"))  # UPDATE, matching apiId
    assert len(calls) == 1
    assert calls[0][1]["serviceId"] == watched["serviceId"]


def test_subscription_without_api_ids_filter_notifies_for_every_matching_service(client, monkeypatch):
    """A subscription with no filter is notified for every service of the subscribed event type."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/capif-events/v1/rapp-2/subscriptions", json={
        "subscriberId": "rapp-2", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://rapp-2/cb",
    })

    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(service_name="service-a"))
    client.post("/published-apis/v1/rapp-3/service-apis", json=register_body(service_name="service-b"))

    assert len(calls) == 2


def test_notify_service_change_does_not_notify_a_subscriber_the_service_is_not_visible_to(client, monkeypatch):
    """Notifications use the discovery gate: a subscriber outside `allowedConsumers` is not told about the service, since it could not discover it."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/capif-events/v1/rapp-3/subscriptions", json={
        "subscriberId": "rapp-3", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://rapp-3/cb",
    })
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(allowedConsumers=["rapp-2"]))

    assert calls == []


def test_notify_service_change_survives_unreachable_subscriber(client, monkeypatch):
    """A subscriber whose callback cannot be reached never fails the registration: the publish still answers 201."""
    import httpx as httpx_module

    # Stands in for `httpx.post` with the signature `smo_shared.webhook` calls it with; ConnectError is what an unreachable callback raises.
    def raise_error(url, json=None, timeout=None, **kw):
        raise httpx_module.ConnectError("unreachable")

    monkeypatch.setattr("app.main.httpx.post", raise_error)

    client.post("/capif-events/v1/rapp-2/subscriptions", json={
        "subscriberId": "rapp-2", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://rapp-2/cb",
    })
    resp = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body())  # must not raise
    assert resp.status_code == 201


def test_register_service_without_enrollment_is_forbidden(client):
    """Registering a service for an apf_id that was never enrolled as a provider is refused with 403 `APF_NOT_REGISTERED`."""
    resp = client.post("/published-apis/v1/rapp-unregistered/service-apis", json=register_body(producer="rapp-unregistered"))
    assert resp.status_code == 403
    assert resp.json()["detail"]["title"] == "APF_NOT_REGISTERED"


def test_register_provider_then_register_service_succeeds(client):
    """Enrolling a provider answers 201 with its apfId and unlocks service registration for it."""
    resp = client.post("/provider-registrations", json={"apfId": "rapp-99", "providerDomainInfo": "test rApp"})
    assert resp.status_code == 201
    assert resp.json() == {"apfId": "rapp-99"}

    resp = client.post("/published-apis/v1/rapp-99/service-apis", json=register_body(producer="rapp-99", service_name="rapp-99-service"))
    assert resp.status_code == 201


def test_register_provider_is_idempotent_update_in_place(client):
    """Enrolling the same apfId twice is an update (201 again), not a conflict, and the provider stays usable."""
    client.post("/provider-registrations", json={"apfId": "rapp-99", "providerDomainInfo": "first"})
    resp = client.post("/provider-registrations", json={"apfId": "rapp-99", "providerDomainInfo": "second"})
    assert resp.status_code == 201
    # still registered, still usable — a re-registration is an update, not a conflict
    resp = client.post("/published-apis/v1/rapp-99/service-apis", json=register_body(producer="rapp-99", service_name="rapp-99-service"))
    assert resp.status_code == 201


def test_deregister_provider_removes_it(client):
    """Removing a provider enrolment answers 204 and blocks it from registering services again (403)."""
    client.post("/provider-registrations", json={"apfId": "rapp-99"})
    resp = client.delete("/provider-registrations/rapp-99")
    assert resp.status_code == 204

    resp = client.post("/published-apis/v1/rapp-99/service-apis", json=register_body(producer="rapp-99", service_name="rapp-99-service"))
    assert resp.status_code == 403


def test_deregister_unknown_provider_is_idempotent(client):
    """Removing an unknown provider answers 204."""
    resp = client.delete("/provider-registrations/does-not-exist")
    assert resp.status_code == 204


def test_query_own_services_without_enrollment_is_404(client):
    """An apf_id that has no services and no enrolment is "not a publisher": 404, not an empty list (GetApfIdServiceApis, publishservice.go)."""
    resp = client.get("/published-apis/v1/rapp-unregistered/service-apis")
    assert resp.status_code == 404


def test_query_own_services_with_enrollment_and_no_services_is_empty_list(client):
    """An enrolled provider with no services yet gets 200 and an empty list."""
    client.post("/provider-registrations", json={"apfId": "rapp-99"})
    resp = client.get("/published-apis/v1/rapp-99/service-apis")
    assert resp.status_code == 200
    assert resp.json() == []


def test_deregistering_a_provider_takes_its_services_with_it(client):
    """Call flow 18a: deleting a provider deletes the services it published (they stayed discoverable before), so its apfId is then not a publisher at all."""
    client.post("/provider-registrations", json={"apfId": "rapp-99"})
    client.post("/published-apis/v1/rapp-99/service-apis", json=register_body(producer="rapp-99", service_name="rapp-99-service"))
    client.delete("/provider-registrations/rapp-99")

    assert client.get("/published-apis/v1/rapp-99/service-apis").status_code == 404
    assert client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "anyone", "api_name": "rapp-99-service"}).json() == []


def _register_invoker(client, public_key="pk-1"):
    """Onboards an invoker through the real route and returns the response body (`apiInvokerId`, `onboardingSecret`, ...); asserts 201.

    Tests get an identity the way a client does, from the server, instead of inserting rows with chosen ids and secrets.
    """
    resp = client.post("/invoker-registrations", json={"apiInvokerPublicKey": public_key})
    assert resp.status_code == 201
    return resp.json()


def test_register_invoker_generates_id_and_secret_server_side(client):
    """The server mints the invoker id (`api-invoker-...`) and the onboarding secret; the client supplies only a public key."""
    body = _register_invoker(client, public_key="pk-1")
    assert body["apiInvokerId"].startswith("api-invoker-")
    assert body["onboardingSecret"]


def test_register_invoker_generates_a_distinct_id_and_secret_each_call(client):
    """Onboarding twice with the same public key creates two invokers with different ids and secrets (every onboarding is new)."""
    first = _register_invoker(client, public_key="pk-1")
    second = _register_invoker(client, public_key="pk-1")  # same public key, still a genuinely new onboarding
    assert first["apiInvokerId"] != second["apiInvokerId"]
    assert first["onboardingSecret"] != second["onboardingSecret"]


def test_register_invoker_persists_the_submitted_public_key(client, db_session_factory):
    """The public key sent at onboarding is stored as given."""
    body = _register_invoker(client, public_key="the-real-public-key")
    with db_session_factory() as session:
        stored = session.get(InvokerRegistration, body["apiInvokerId"])
    assert stored.public_key == "the-real-public-key"


def test_onboarding_secret_is_never_stored_in_cleartext(client, db_session_factory):
    """The stored `onboarding_secret_hash` is a `salt:digest` value that does not contain the secret, so a database leak does not hand out credentials."""
    body = _register_invoker(client)
    with db_session_factory() as session:
        stored = session.get(InvokerRegistration, body["apiInvokerId"]).onboarding_secret_hash
    assert body["onboardingSecret"] not in stored
    assert ":" in stored  # salt_hex:digest_hex


def test_access_token_is_never_stored_in_cleartext(client, db_session_factory):
    """Only a 64-hex-character SHA-256 of the access token is stored; the raw token is not recoverable from the database."""
    inv = _register_invoker(client)
    token = client.post("/oauth2/token", json={
        "grant_type": "client_credentials", "client_id": inv["apiInvokerId"], "client_secret": inv["onboardingSecret"],
    }).json()["access_token"]
    with db_session_factory() as session:
        stored_hashes = [row.access_token_hash for row in session.query(IssuedAccessToken).all()]
    assert token not in stored_hashes
    assert len(stored_hashes) == 1
    assert len(stored_hashes[0]) == 64  # hex-encoded SHA-256


def test_issue_token_succeeds_for_a_registered_invoker(client):
    """A correct client_id and secret get a Bearer token with a 3600 s lifetime."""
    inv = _register_invoker(client)
    resp = client.post("/oauth2/token", json={
        "grant_type": "client_credentials", "client_id": inv["apiInvokerId"], "client_secret": inv["onboardingSecret"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["access_token"]


def test_issue_token_rejects_unregistered_invoker(client):
    """An unknown client_id is refused with 400 `invalid_client`."""
    resp = client.post("/oauth2/token", json={"grant_type": "client_credentials", "client_id": "does-not-exist", "client_secret": "whatever"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client"


def test_issue_token_rejects_wrong_secret(client):
    """A wrong secret is refused with 400 `unauthorized_client`."""
    inv = _register_invoker(client)
    resp = client.post("/oauth2/token", json={"grant_type": "client_credentials", "client_id": inv["apiInvokerId"], "client_secret": "wrong"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unauthorized_client"


def test_issue_token_rejects_unsupported_grant_type(client):
    """Any grant type other than `client_credentials` is refused with 400 `unsupported_grant_type`, even with valid credentials."""
    inv = _register_invoker(client)
    resp = client.post("/oauth2/token", json={
        "grant_type": "authorization_code", "client_id": inv["apiInvokerId"], "client_secret": inv["onboardingSecret"],
    })
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


def test_introspect_active_token_reports_active_with_client_id(client):
    """Introspection of a live token answers `active: true` with the invoker's id as `client_id`."""
    inv = _register_invoker(client)
    token = client.post("/oauth2/token", json={
        "grant_type": "client_credentials", "client_id": inv["apiInvokerId"], "client_secret": inv["onboardingSecret"],
    }).json()["access_token"]

    resp = client.post("/oauth2/introspect", json={"token": token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["client_id"] == inv["apiInvokerId"]


def test_introspect_unknown_token_is_inactive(client):
    """An unknown token is `{active: false}` with HTTP 200 (RFC 7662), not an error."""
    resp = client.post("/oauth2/introspect", json={"token": "not-a-real-token"})
    assert resp.status_code == 200
    assert resp.json() == {"active": False}


def test_introspect_expired_token_is_inactive(client, db_session_factory):
    """A token past its `expires_at` is inactive; the test back-dates the stored row by one second."""
    inv = _register_invoker(client)
    token = client.post("/oauth2/token", json={
        "grant_type": "client_credentials", "client_id": inv["apiInvokerId"], "client_secret": inv["onboardingSecret"],
    }).json()["access_token"]

    from app.main import _hash_token

    with db_session_factory() as session:
        rec = session.get(IssuedAccessToken, _hash_token(token))
        rec.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)
        session.commit()

    resp = client.post("/oauth2/introspect", json={"token": token})
    assert resp.json() == {"active": False}


def _register_trusted_invoker(client, api_invoker_id, aef_id="aef-1", api_id="api-1", pref_methods=None):
    """PUTs a one-entry trusted-invoker context (`auth-info` / `authz-info` strings, the given AEF, API and preferred methods) for `api_invoker_id` and returns the response."""
    return client.put(f"/trusted-invokers/{api_invoker_id}", json={
        "notificationDestination": "http://consumer/security-notify",
        "securityInfo": [{"aefId": aef_id, "apiId": api_id, "authenticationInfo": "auth-info", "authorizationInfo": "authz-info",
                           "prefSecurityMethods": pref_methods or ["OAUTH"]}],
    })


def test_register_trusted_invoker_rejects_unregistered_invoker(client):
    """A trusted-invoker context can only be created for an onboarded invoker: 400 `INVOKER_NOT_REGISTERED`."""
    resp = _register_trusted_invoker(client, "does-not-exist")
    assert resp.status_code == 400
    assert resp.json()["detail"]["title"] == "INVOKER_NOT_REGISTERED"


def test_register_trusted_invoker_succeeds_for_registered_invoker(client):
    """A registered invoker gets 201, and the first preferred method becomes `selSecurityMethod`."""
    inv = _register_invoker(client)
    resp = _register_trusted_invoker(client, inv["apiInvokerId"])
    assert resp.status_code == 201
    body = resp.json()
    assert body["apiInvokerId"] == inv["apiInvokerId"]
    assert body["notificationDestination"] == "http://consumer/security-notify"
    assert body["securityInfo"][0]["selSecurityMethod"] == "OAUTH"


def test_register_trusted_invoker_rejects_missing_notification_destination(client):
    """A blank `notificationDestination` is refused with 422 `SECURITY_CONTEXT_INVALID`."""
    inv = _register_invoker(client)
    resp = client.put(f"/trusted-invokers/{inv['apiInvokerId']}", json={
        "notificationDestination": "", "securityInfo": [{"prefSecurityMethods": ["OAUTH"]}],
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SECURITY_CONTEXT_INVALID"


def test_register_trusted_invoker_rejects_empty_security_info(client):
    """An empty `securityInfo` list is refused with 422 `SECURITY_CONTEXT_INVALID`."""
    inv = _register_invoker(client)
    resp = client.put(f"/trusted-invokers/{inv['apiInvokerId']}", json={"notificationDestination": "http://consumer/notify", "securityInfo": []})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SECURITY_CONTEXT_INVALID"


def test_register_trusted_invoker_rejects_missing_pref_security_methods(client):
    """An entry without any `prefSecurityMethods` is refused with 422 `SECURITY_CONTEXT_INVALID`."""
    inv = _register_invoker(client)
    resp = client.put(f"/trusted-invokers/{inv['apiInvokerId']}", json={
        "notificationDestination": "http://consumer/notify", "securityInfo": [{"aefId": "aef-1", "prefSecurityMethods": []}],
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SECURITY_CONTEXT_INVALID"


def test_register_trusted_invoker_is_idempotent_replace_on_re_put(client):
    """A second PUT replaces the context (one entry, new selected method) and still answers 201."""
    inv = _register_invoker(client)
    _register_trusted_invoker(client, inv["apiInvokerId"], pref_methods=["OAUTH"])
    resp = _register_trusted_invoker(client, inv["apiInvokerId"], pref_methods=["PSK"])
    assert resp.status_code == 201
    assert resp.json()["securityInfo"][0]["selSecurityMethod"] == "PSK"
    assert len(resp.json()["securityInfo"]) == 1


def test_get_trusted_invoker_returns_404_for_unregistered(client):
    """Reading a missing context is 404 `TRUSTED_INVOKER_NOT_FOUND`."""
    resp = client.get("/trusted-invokers/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["title"] == "TRUSTED_INVOKER_NOT_FOUND"


def test_get_trusted_invoker_redacts_authentication_and_authorization_info_by_default(client):
    """The GET blanks `authenticationInfo` and `authorizationInfo` unless asked, so the secrets are not returned to every reader."""
    inv = _register_invoker(client)
    _register_trusted_invoker(client, inv["apiInvokerId"])
    resp = client.get(f"/trusted-invokers/{inv['apiInvokerId']}")
    assert resp.status_code == 200
    info = resp.json()["securityInfo"][0]
    assert info["authenticationInfo"] == ""
    assert info["authorizationInfo"] == ""


def test_get_trusted_invoker_reveals_authentication_and_authorization_info_when_requested(client):
    """Passing the two query flags returns the stored `authenticationInfo` and `authorizationInfo`."""
    inv = _register_invoker(client)
    _register_trusted_invoker(client, inv["apiInvokerId"])
    resp = client.get(f"/trusted-invokers/{inv['apiInvokerId']}", params={"authentication_info": True, "authorization_info": True})
    info = resp.json()["securityInfo"][0]
    assert info["authenticationInfo"] == "auth-info"
    assert info["authorizationInfo"] == "authz-info"


def test_deregister_trusted_invoker_removes_it(client, db_session_factory):
    """Deleting a context answers 204 and removes the row."""
    inv = _register_invoker(client)
    _register_trusted_invoker(client, inv["apiInvokerId"])
    resp = client.delete(f"/trusted-invokers/{inv['apiInvokerId']}")
    assert resp.status_code == 204
    with db_session_factory() as session:
        assert session.get(TrustedInvoker, inv["apiInvokerId"]) is None


def test_deregister_unknown_trusted_invoker_is_idempotent(client):
    """Deleting an unknown context answers 204."""
    resp = client.delete("/trusted-invokers/does-not-exist")
    assert resp.status_code == 204


def test_update_trusted_invoker_requires_existing_context(client):
    """The update route needs an existing context: 404 `TRUSTED_INVOKER_NOT_FOUND` even for a registered invoker."""
    inv = _register_invoker(client)
    resp = client.post(f"/trusted-invokers/{inv['apiInvokerId']}/update", json={
        "notificationDestination": "http://consumer/notify", "securityInfo": [{"prefSecurityMethods": ["OAUTH"]}],
    })
    assert resp.status_code == 404
    assert resp.json()["detail"]["title"] == "TRUSTED_INVOKER_NOT_FOUND"


def test_update_trusted_invoker_does_not_re_check_invoker_registration(client, db_session_factory):
    """The update route, unlike the PUT, does not re-check that the invoker is still onboarded: it only needs the context to exist (as in the reference). The test deletes the registration row behind the API."""
    inv = _register_invoker(client)
    _register_trusted_invoker(client, inv["apiInvokerId"])
    with db_session_factory() as session:
        session.query(InvokerRegistration).filter_by(api_invoker_id=inv["apiInvokerId"]).delete()
        session.commit()

    resp = client.post(f"/trusted-invokers/{inv['apiInvokerId']}/update", json={
        "notificationDestination": "http://consumer/notify-v2", "securityInfo": [{"prefSecurityMethods": ["PSK"]}],
    })
    assert resp.status_code == 200
    assert resp.json()["notificationDestination"] == "http://consumer/notify-v2"


def test_revoke_trusted_invoker_removes_matching_entry_by_aef_id(client):
    """Revocation removes the entries matching the notified `aefId` (here also with an unrelated apiId) and keeps the others."""
    inv = _register_invoker(client)
    client.put(f"/trusted-invokers/{inv['apiInvokerId']}", json={
        "notificationDestination": "http://consumer/notify",
        "securityInfo": [
            {"aefId": "aef-1", "apiId": "api-1", "prefSecurityMethods": ["OAUTH"]},
            {"aefId": "aef-2", "apiId": "api-2", "prefSecurityMethods": ["OAUTH"]},
        ],
    })
    resp = client.post(f"/trusted-invokers/{inv['apiInvokerId']}/delete", json={
        "aefId": "aef-1", "apiIds": ["nonexistent"], "apiInvokerId": inv["apiInvokerId"], "cause": "UNEXPECTED_REASON",
    })
    assert resp.status_code == 204

    remaining = client.get(f"/trusted-invokers/{inv['apiInvokerId']}")
    assert len(remaining.json()["securityInfo"]) == 1
    assert remaining.json()["securityInfo"][0]["aefId"] == "aef-2"


def test_revoke_trusted_invoker_deletes_whole_record_when_no_entries_remain(client, db_session_factory):
    """Revoking the last remaining entry drops the whole trusted-invoker record."""
    inv = _register_invoker(client)
    _register_trusted_invoker(client, inv["apiInvokerId"], aef_id="aef-1", api_id="api-1")
    resp = client.post(f"/trusted-invokers/{inv['apiInvokerId']}/delete", json={
        "aefId": "aef-1", "apiIds": ["api-1"], "apiInvokerId": inv["apiInvokerId"], "cause": "OVERLIMIT_USAGE",
    })
    assert resp.status_code == 204
    with db_session_factory() as session:
        assert session.get(TrustedInvoker, inv["apiInvokerId"]) is None


def test_revoke_trusted_invoker_returns_404_for_unregistered(client):
    """Revoking for an unknown invoker is 404 `TRUSTED_INVOKER_NOT_FOUND`."""
    resp = client.post("/trusted-invokers/does-not-exist/delete", json={
        "apiIds": ["api-1"], "apiInvokerId": "does-not-exist", "cause": "OVERLIMIT_USAGE",
    })
    assert resp.status_code == 404
    assert resp.json()["detail"]["title"] == "TRUSTED_INVOKER_NOT_FOUND"


def test_revoke_trusted_invoker_rejects_empty_api_ids(client):
    """A revocation with an empty `apiIds` is refused with 422 `SECURITY_CONTEXT_INVALID`."""
    inv = _register_invoker(client)
    _register_trusted_invoker(client, inv["apiInvokerId"])
    resp = client.post(f"/trusted-invokers/{inv['apiInvokerId']}/delete", json={
        "apiIds": [], "apiInvokerId": inv["apiInvokerId"], "cause": "OVERLIMIT_USAGE",
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SECURITY_CONTEXT_INVALID"


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """`/health` answers 200 `{status: healthy}`: the GUI BFF probes `/<module>/health` on every module."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


# ---------------------------------------------------------------- registry reads (GUI pass 2)

def test_list_providers_counts_their_published_services(client):
    """The provider listing reports `serviceCount` per provider (1 for the publisher, 0 for the pre-enrolled others)."""
    client.post("/provider-registrations", json={"apfId": "rapp-1", "providerDomainInfo": "demo"})
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body())
    by_apf = {p["apfId"]: p for p in client.get("/provider-registrations").json()["items"]}
    assert by_apf["rapp-1"] == {"apfId": "rapp-1", "providerDomainInfo": "demo", "serviceCount": 1}
    assert by_apf["rapp-2"]["serviceCount"] == 0   # KNOWN_TEST_PUBLISHERS, pre-registered by the fixture


def test_list_invokers_never_exposes_the_secret(client):
    """The invoker listing shows id, key, `keyAuthentication`, `trusted` and scope but neither the secret nor its hash."""
    inv = _register_invoker(client, public_key="pk-9")
    _register_trusted_invoker(client, inv["apiInvokerId"])
    listed = client.get("/invoker-registrations").json()["items"]
    assert listed == [{"apiInvokerId": inv["apiInvokerId"], "apiInvokerPublicKey": "pk-9", "keyAuthentication": False,
                       "trusted": True, "authzScope": None}]
    assert inv["onboardingSecret"] not in str(listed)


def test_list_trusted_invokers(client):
    """The trusted-invoker listing returns the registered contexts."""
    inv = _register_invoker(client)
    _register_trusted_invoker(client, inv["apiInvokerId"])
    assert [t["apiInvokerId"] for t in client.get("/trusted-invokers").json()["items"]] == [inv["apiInvokerId"]]


def test_list_event_subscriptions_per_subscriber(client):
    """A subscriber's listing shows only its own subscriptions."""
    client.post("/capif-events/v1/rapp-1/subscriptions", json={"subscriberId": "rapp-1", "eventTypes": ["SERVICE_API_AVAILABLE"],
                                                               "callbackUri": "http://rapp-1/cb"})
    listed = client.get("/capif-events/v1/rapp-1/subscriptions").json()["items"]
    assert [(s["subscriberId"], s["eventTypes"]) for s in listed] == [("rapp-1", ["SERVICE_API_AVAILABLE"])]
    assert client.get("/capif-events/v1/rapp-2/subscriptions").json()["items"] == []


# ---------------------------------------------------------------- PR-ST-4: stale-invoker housekeeping

def _age_invoker(db_session_factory, invoker_id, days, *, last_token=False):
    """Back-dates an invoker in the database as if it was onboarded `days` ago, or (`last_token=True`) as if it last got a token `days` ago.

    With `last_token=False` the last-token time is also cleared, so the age is measured from onboarding.
    """
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
    with db_session_factory() as session:
        inv = session.get(InvokerRegistration, invoker_id)
        if last_token:
            inv.last_token_issued_at = when
        else:
            inv.created_at, inv.last_token_issued_at = when, None
        session.commit()


def test_an_invoker_records_when_it_was_onboarded_and_when_it_last_got_a_token(client, db_session_factory):
    """Onboarding sets `created_at` (and leaves `last_token_issued_at` empty); issuing a token sets `last_token_issued_at`. The purge relies on both."""
    inv = _register_invoker(client)
    with db_session_factory() as session:
        row = session.get(InvokerRegistration, inv["apiInvokerId"])
        assert row.created_at is not None and row.last_token_issued_at is None
    token = client.post("/oauth2/token", json={"grant_type": "client_credentials", "client_id": inv["apiInvokerId"],
                                                "client_secret": inv["onboardingSecret"]})
    assert token.status_code == 200
    with db_session_factory() as session:
        assert session.get(InvokerRegistration, inv["apiInvokerId"]).last_token_issued_at is not None


def test_purge_stale_is_a_dry_run_by_default_and_lists_only_the_stale(client, db_session_factory):
    """Without `dry_run=false` the purge deletes nothing; it lists only invokers older than the threshold."""
    stale, recent = _register_invoker(client, "pk-stale"), _register_invoker(client, "pk-recent")
    _age_invoker(db_session_factory, stale["apiInvokerId"], days=10)
    resp = client.post("/invoker-registrations/purge-stale", params={"unused_for_days": 7})
    assert resp.status_code == 200
    assert resp.json() == {"unusedForDays": 7, "dryRun": True, "count": 1, "invokerIds": [stale["apiInvokerId"]]}
    ids = {i["apiInvokerId"] for i in client.get("/invoker-registrations").json()["items"]}
    assert ids == {stale["apiInvokerId"], recent["apiInvokerId"]}  # nothing deleted


def test_purge_stale_offboards_only_the_stale_with_their_tokens_and_trust(client, db_session_factory):
    """A real purge removes the stale invoker together with its tokens and leaves the recent invoker and its token alone."""
    stale, recent = _register_invoker(client, "pk-stale"), _register_invoker(client, "pk-recent")
    for inv in (stale, recent):
        assert client.post("/oauth2/token", json={"grant_type": "client_credentials", "client_id": inv["apiInvokerId"],
                                                   "client_secret": inv["onboardingSecret"]}).status_code == 200
    _age_invoker(db_session_factory, stale["apiInvokerId"], days=10, last_token=True)

    resp = client.post("/invoker-registrations/purge-stale", params={"unused_for_days": 7, "dry_run": False})
    assert resp.json()["invokerIds"] == [stale["apiInvokerId"]]
    ids = {i["apiInvokerId"] for i in client.get("/invoker-registrations").json()["items"]}
    assert ids == {recent["apiInvokerId"]}
    with db_session_factory() as session:
        assert session.query(IssuedAccessToken).filter_by(api_invoker_id=stale["apiInvokerId"]).count() == 0
        assert session.query(IssuedAccessToken).filter_by(api_invoker_id=recent["apiInvokerId"]).count() == 1


def test_an_invoker_that_keeps_getting_tokens_is_never_stale(client, db_session_factory):
    """Staleness is measured from the last token, not from onboarding: an old invoker in daily use is not purged."""
    inv = _register_invoker(client)
    _age_invoker(db_session_factory, inv["apiInvokerId"], days=30)           # onboarded long ago ...
    client.post("/oauth2/token", json={"grant_type": "client_credentials", "client_id": inv["apiInvokerId"],
                                        "client_secret": inv["onboardingSecret"]})  # ... but in use today
    assert client.post("/invoker-registrations/purge-stale", params={"unused_for_days": 7}).json()["count"] == 0


def test_purge_stale_needs_a_positive_number_of_days(client):
    """`unused_for_days` is required and must be positive (422 for 0 or missing), so a call cannot purge everyone by accident."""
    assert client.post("/invoker-registrations/purge-stale", params={"unused_for_days": 0}).status_code == 422
    assert client.post("/invoker-registrations/purge-stale").status_code == 422


# ---------------------------------------------------------------- notifications through the outbox (PR-MSG-1.6)

def _outbox_rows(db_session_factory):
    """Returns all notification-outbox rows in creation order, read through a fresh session."""
    with db_session_factory() as db:
        return db.query(NotificationOutbox).order_by(NotificationOutbox.created_at).all()


def test_a_service_event_survives_a_crash_between_commit_and_send(client, db_session_factory, monkeypatch):
    """Outbox guarantee: an event is a committed row before anything is sent. With the inline drain off (simulating a crash after the commit) nothing is sent but the row is PENDING, and a later `drain` delivers it."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    monkeypatch.setenv("MODULE", "sme")
    calls = []
    # The stub returns an object with `status_code` 200 so the outbox counts the row as sent when the later drain runs.
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)) or type("R", (), {"status_code": 200})())
    client.post("/capif-events/v1/rapp-2/subscriptions", json={
        "subscriberId": "rapp-2", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://rapp-2/cb"})

    reg = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body()).json()

    assert calls == []
    rows = _outbox_rows(db_session_factory)
    assert [(r.module, r.status, r.destination) for r in rows] == [("sme", "PENDING", "http://rapp-2/cb")]
    assert rows[0].payload["eventType"] == "SERVICE_API_AVAILABLE" and rows[0].payload["serviceId"] == reg["serviceId"]

    monkeypatch.delenv("SMO_OUTBOX_INLINE_DRAIN")
    assert outbox.drain(db_session_factory().get_bind())["sent"] == 1
    assert [(u, b["eventType"]) for u, b in calls] == [("http://rapp-2/cb", "SERVICE_API_AVAILABLE")]


def test_the_visibility_gate_still_sees_the_policy_written_in_the_same_request(client, db_session_factory, monkeypatch):
    """The event is enqueued before the commit, so the visibility check must read the policy this same request wrote: a subscriber outside `allowedConsumers` gets no outbox row."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    client.post("/capif-events/v1/rapp-3/subscriptions", json={
        "subscriberId": "rapp-3", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://rapp-3/cb"})
    client.post("/capif-events/v1/rapp-2/subscriptions", json={
        "subscriberId": "rapp-2", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://rapp-2/cb"})
    resp = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(
        allowedConsumers=["rapp-2"], gatesDiscoveryVisibility=True))
    assert resp.status_code == 201, resp.text
    assert {r.destination for r in _outbox_rows(db_session_factory)} == {"http://rapp-2/cb"}


def test_nothing_is_announced_when_the_registration_does_not_commit(client, db_session_factory, monkeypatch):
    """If the commit fails, nothing is announced: no callback is made, no outbox row survives and the service row is not stored (a consumer is never told about something that did not happen)."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    client.post("/capif-events/v1/rapp-2/subscriptions", json={
        "subscriberId": "rapp-2", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://rapp-2/cb"})

    from sqlalchemy.orm import Session as OrmSession
    real_commit = OrmSession.commit

    # Fails only a commit that carries pending outbox rows (the registration's), after rolling back like a real failed commit; other commits (fixtures, later reads) pass through.
    def failing_commit(self):
        if self.info.get("outbox_pending_ids"):
            self.rollback()
            raise RuntimeError("the database refused the commit")
        return real_commit(self)

    monkeypatch.setattr(OrmSession, "commit", failing_commit)
    # raise_server_exceptions=False so the RuntimeError becomes the 500 response the assertions look at instead of being re-raised into the test.
    resp = TestClient(app, raise_server_exceptions=False).post("/published-apis/v1/rapp-1/service-apis", json=register_body())
    # Restore the real commit before the assertions below open sessions of their own.
    monkeypatch.setattr(OrmSession, "commit", real_commit)

    assert resp.status_code == 500
    assert calls == [] and _outbox_rows(db_session_factory) == []
    with db_session_factory() as db:
        assert db.query(ServiceProfile).count() == 0


def test_a_renewed_secret_keeps_the_invoker_and_ends_the_old_secret_and_tokens(client):
    """Call flow 01d: a new secret for the same invoker id; the old secret is refused, the tokens issued before are revoked, the new secret works,
    and an unknown invoker is 400 INVOKER_NOT_REGISTERED."""
    inv = _register_invoker(client)
    grant = {"grant_type": "client_credentials", "client_id": inv["apiInvokerId"]}
    old_token = client.post("/oauth2/token", json={**grant, "client_secret": inv["onboardingSecret"]}).json()["access_token"]
    renewed = client.post(f"/invoker-registrations/{inv['apiInvokerId']}/secret")
    assert renewed.status_code == 200 and renewed.json()["apiInvokerId"] == inv["apiInvokerId"]
    assert renewed.json()["onboardingSecret"] != inv["onboardingSecret"]
    assert client.post("/oauth2/token", json={**grant, "client_secret": inv["onboardingSecret"]}).status_code == 400
    assert client.post("/oauth2/token", json={**grant, "client_secret": renewed.json()["onboardingSecret"]}).status_code == 200
    assert client.post("/oauth2/introspect", json={"token": old_token}).json()["active"] is False
    assert client.post("/invoker-registrations/unknown/secret").status_code == 400
