"""Tests of call flow 16 (DOC-CF-1.8): FOCOM's inventory resource lifecycle. A subscription is checked when it is made; a provision takes a typed body and a
pool; a notification is O2-IMS's `InfrastructureInventoryEvent` and the change is on the SMO event feed; a resource still named elsewhere is not
deprovisioned; a duplicate resource type is a 409.

Fixtures come from `test_main.py` (the app on SQLite with the FOCOM and outbox tables). Run: `cd smo/focom && PYTHONPATH=.:../shared python -m pytest
tests/test_inventory_lifecycle.py -q`.
"""

import uuid

from smo_shared import events

from app.main import PHASE1_POOL_ID
from app.models import O2imsObject, Resource
from test_main import client, db_session  # noqa: F401  (pytest fixtures)


def _capture(monkeypatch) -> list:
    """Records every notification POSTed, as (url, json)."""
    calls: list = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    return calls


def _title(resp):
    """The error code of a refusal."""
    return resp.json()["detail"]["title"]


def test_a_subscription_with_a_refused_callback_or_an_unknown_type_is_refused(client):
    """Call flow 16a: a callback the SSRF guard refuses is 422 and an unregistered filter type 404 `RESOURCE_TYPE_NOT_FOUND`; nothing is stored."""
    refused = client.post("/inventory/subscriptions", json={"callback": "http://127.0.0.1/cb"})
    assert refused.status_code == 422 and _title(refused) == "SCHEMA_VALIDATION_FAILED"
    unknown = client.post("/inventory/subscriptions", json={"callback": "http://consumer/cb", "resourceTypeId": "no-such-type"})
    assert unknown.status_code == 404 and _title(unknown) == "RESOURCE_TYPE_NOT_FOUND"
    assert client.get("/inventory/subscriptions").json()["items"] == []


def test_a_notification_is_an_o2ims_inventory_event_and_the_change_is_on_the_event_feed(client, monkeypatch):
    """Call flow 16b and 16d: CREATE carries `postObjectState`, DELETE `priorObjectState`, both `objectRef` and a `notificationId`, beside the earlier
    fields; each change is published as `o-cloud-resource.created` / `.deleted`."""
    monkeypatch.setenv("SMO_EVENT_BUS", "memory")
    events.MEMORY.events.clear()
    calls = _capture(monkeypatch)
    client.post("/inventory/subscriptions", json={"callback": "http://consumer/cb", "consumerSubscriptionId": "c-1"})
    rid = client.post("/resources/provision", json={"resourceTypeId": "gpu-l40", "description": "node", "tags": ["edge"]}).json()["resourceId"]
    client.delete(f"/resources/{rid}")
    created, deleted = calls[0][1], calls[1][1]
    assert created["notificationEventType"] == "CREATE" and created["objectRef"] == f"/focom/resources/{rid}" and created["consumerSubscriptionId"] == "c-1"
    assert created["postObjectState"]["description"] == "node" and created["postObjectState"]["tags"] == ["edge"] and "priorObjectState" not in created
    assert deleted["notificationEventType"] == "DELETE" and deleted["priorObjectState"]["resourceId"] == rid and "postObjectState" not in deleted
    assert created["resourceId"] == rid and created["resourceTypeId"] == "gpu-l40" and created["notificationId"] != deleted["notificationId"]
    assert [(e["type"], e["subject"]) for e in events.MEMORY.events] == [("org.oran.smo.o-cloud-resource.created", rid),
                                                                          ("org.oran.smo.o-cloud-resource.deleted", rid)]


def test_a_provision_takes_a_typed_body_and_a_pool(client, db_session):
    """Call flow 16b: a field of the wrong type is 422; an unknown pool is 404 `RESOURCE_POOL_NOT_FOUND`; a known pool is used; unknown keys are ignored."""
    assert client.post("/resources/provision", json={"tags": {"not": "a list"}}).status_code == 422
    assert client.post("/resources/provision", json={"description": 7}).status_code == 422
    missing = client.post("/resources/provision", json={"resourcePoolId": "pool-x"})
    assert missing.status_code == 404 and _title(missing) == "RESOURCE_POOL_NOT_FOUND"
    client.get("/resource-pools")      # seeds the topology, site included
    site = client.get("/o-cloud-sites").json()["items"][0]["oCloudSiteId"]
    assert client.post("/resource-pools", json={"resourcePoolId": "pool-b", "name": "b", "oCloudSiteId": site}).status_code == 201
    rid = client.post("/resources/provision", json={"resourcePoolId": "pool-b", "cpu": 4}).json()["resourceId"]
    assert client.get(f"/resources/{rid}").json()["resourcePoolId"] == "pool-b"
    assert client.post("/resources/provision", json={}).status_code == 200
    with db_session() as db:
        assert {r.resource_pool_id for r in db.query(Resource).all()} == {"pool-b", PHASE1_POOL_ID}


def test_a_resource_still_named_elsewhere_is_not_deprovisioned(client, db_session, monkeypatch):
    """Call flow 16d: a resource named by a cluster resource, an infrastructure resource or a child resource is 409 `RESOURCE_IN_USE`, listing them, and
    stays, with nothing notified; once they are gone it is deprovisioned."""
    calls = _capture(monkeypatch)
    rid = client.post("/resources/provision", json={}).json()["resourceId"]
    client.post("/inventory/subscriptions", json={"callback": "http://consumer/cb"})
    with db_session() as db:
        db.add(O2imsObject(kind="ClusterResource", object_id="cr-1", attributes={"resourceId": rid}))
        db.add(O2imsObject(kind="InfrastructureResource", object_id="ir-1", attributes={"inventoryResourceIds": [rid]}))
        db.add(Resource(resource_type_id="generic", resource_pool_id=PHASE1_POOL_ID, parent_id=uuid.UUID(rid)))
        db.commit()
    refused = client.delete(f"/resources/{rid}")
    assert refused.status_code == 409 and _title(refused) == "RESOURCE_IN_USE"
    assert "ClusterResource cr-1" in refused.json()["detail"]["detail"] and "InfrastructureResource ir-1" in refused.json()["detail"]["detail"]
    assert client.get(f"/resources/{rid}").status_code == 200 and calls == []
    with db_session() as db:
        db.query(O2imsObject).delete()
        db.query(Resource).filter(Resource.parent_id.is_not(None)).delete()
        db.commit()
    assert client.delete(f"/resources/{rid}").json() == {"status": "deprovisioned"}
    assert client.get(f"/resources/{rid}").status_code == 404 and [c[1]["notificationEventType"] for c in calls] == ["DELETE"]
