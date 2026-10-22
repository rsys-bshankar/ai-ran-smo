"""Tests of call flow 18 (DOC-CF-1.8): an rApp acts at SME only as itself. The apfId it registers, publishes and deregisters under, the invoker it discovers
for and the subscriber it subscribes as must be its own invoker id; the console and SMO modules act for others. A provider's services go with it, and an
offboarded invoker's subscriptions and provider go with it.

Fixtures come from `test_main.py` (the app on SQLite, `rapp-1` to `rapp-3` enrolled as providers). Run: `cd smo/sme && PYTHONPATH=.:../shared python -m pytest
tests/test_identity.py -q`.
"""

from test_main import client, db_session_factory, register_body  # noqa: F401  (pytest fixtures and builder)


def rapp(invoker):
    """The headers R1 Termination stamps on an rApp's call."""
    return {"X-R1-Role": "rapp", "X-R1-Invoker-Id": invoker}


def _title(resp):
    """The error code of a refusal."""
    return resp.json()["detail"]["title"]


def test_an_rapp_registers_publishes_and_deregisters_only_as_its_own_provider(client):
    """Call flow 18a: under another apfId every provider and service change is 403 `PROVIDER_IDENTITY_MISMATCH` and nothing changes; under its own id it
    may; the console (no rApp role) may act for any apfId."""
    published = client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(service_name="owned-by-1"))
    service = published.json()["serviceId"]
    for method, path, body in (("POST", "/provider-registrations", {"apfId": "rapp-1"}), ("DELETE", "/provider-registrations/rapp-1", None),
                               ("POST", "/published-apis/v1/rapp-1/service-apis", register_body(service_name="owned-by-1", endpoint="http://evil/")),
                               ("DELETE", f"/published-apis/v1/rapp-1/service-apis/{service}", None)):
        refused = client.request(method, path, json=body, headers=rapp("rapp-2"))
        assert refused.status_code == 403 and _title(refused) == "PROVIDER_IDENTITY_MISMATCH", path
    assert [s["endpoint"] for s in client.get("/published-apis/v1/rapp-1/service-apis").json()] == ["http://rapp-1/svc"]
    assert client.post("/provider-registrations", json={"apfId": "rapp-2"}, headers=rapp("rapp-2")).status_code == 201
    assert client.post("/published-apis/v1/rapp-2/service-apis", json=register_body(service_name="owned-by-2"), headers=rapp("rapp-2")).status_code == 201
    assert client.delete(f"/published-apis/v1/rapp-1/service-apis/{service}").status_code == 204       # the console
    assert client.get("/published-apis/v1/rapp-1/service-apis").json() == []


def test_an_rapp_discovers_only_as_itself(client):
    """Call flow 18e: discovery naming another invoker is 403 `INVOKER_IDENTITY_MISMATCH` for an rApp, so it cannot see a service allowed only to another;
    for itself it sees what it may; the console may ask for anyone."""
    client.post("/published-apis/v1/rapp-1/service-apis", json=register_body(service_name="private", allowedConsumers=["consumer-a"]))
    refused = client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "consumer-a"}, headers=rapp("consumer-b"))
    assert refused.status_code == 403 and _title(refused) == "INVOKER_IDENTITY_MISMATCH"
    assert client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "consumer-b"}, headers=rapp("consumer-b")).json() == []
    assert [s["serviceName"] for s in client.get("/service-apis/v1/allServiceAPIs", params={"api_invoker_id": "consumer-a"}).json()] == ["private"]


def test_an_rapp_subscribes_lists_and_unsubscribes_only_as_itself_and_a_refused_callback_is_422(client):
    """Call flow 18e: CAPIF event subscriptions under another subscriber id are 403 `INVOKER_IDENTITY_MISMATCH` for an rApp; a callback the SSRF guard refuses
    is 422 for anyone."""
    body = {"subscriberId": "consumer-a", "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://consumer-a/cb"}
    for method, path in (("POST", "/capif-events/v1/consumer-a/subscriptions"), ("GET", "/capif-events/v1/consumer-a/subscriptions"),
                         ("DELETE", "/capif-events/v1/consumer-a/subscriptions/00000000-0000-0000-0000-000000000000")):
        refused = client.request(method, path, json=body if method == "POST" else None, headers=rapp("consumer-b"))
        assert refused.status_code == 403 and _title(refused) == "INVOKER_IDENTITY_MISMATCH", method
    assert client.post("/capif-events/v1/consumer-b/subscriptions", json=body, headers=rapp("consumer-b")).status_code == 201
    bad = client.post("/capif-events/v1/consumer-b/subscriptions", json={**body, "callbackUri": "http://169.254.169.254/x"})
    assert bad.status_code == 422 and _title(bad) == "SCHEMA_VALIDATION_FAILED"


def test_offboarding_an_invoker_removes_its_subscriptions_and_its_provider_with_its_services(client):
    """Call flow 18b: an offboarded invoker leaves no CAPIF subscription, no provider registered under its id and none of that provider's services."""
    inv = client.post("/invoker-registrations", json={"apiInvokerPublicKey": "pk"}).json()["apiInvokerId"]
    client.post("/provider-registrations", json={"apfId": inv}, headers=rapp(inv))
    client.post(f"/published-apis/v1/{inv}/service-apis", json=register_body(service_name="theirs"), headers=rapp(inv))
    client.post(f"/capif-events/v1/{inv}/subscriptions", json={"subscriberId": inv, "eventTypes": ["SERVICE_API_AVAILABLE"], "callbackUri": "http://x/cb"}, headers=rapp(inv))
    assert client.delete(f"/invoker-registrations/{inv}").status_code == 204
    assert client.get(f"/published-apis/v1/{inv}/service-apis").status_code == 404
    assert client.get(f"/capif-events/v1/{inv}/subscriptions").json()["items"] == []
    assert inv not in {p["apfId"] for p in client.get("/provider-registrations").json()["items"]}
