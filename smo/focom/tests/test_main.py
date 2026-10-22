"""Tests of the FOCOM module's own routes: inventory, resource types / pools / deployment managers, provision and deprovision, inventory subscriptions and
their notifications, alarms and performance queries, the TEIV topology export, `/health`, and the transactional outbox behind every notification (PR-MSG-1.8).

Fixtures: `db_session` builds an in-memory SQLite database with the FOCOM tables and the outbox table and returns its session factory; `client` is a
`TestClient` of `app.main.app` whose `get_session` dependency uses that database. `test_o2ims_conformance.py` imports both. Subscriber callbacks are
observed by patching `httpx.post` (`app.main.httpx` is the shared module), which is what the outbox's inline drain calls.

Run: `cd smo/focom && PYTHONPATH=.:../shared python -m pytest tests/test_main.py -q`. Needs nothing external (no Postgres, no network).
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

from app.main import app, PHASE1_CLUSTER_ID, PHASE1_DEPLOYMENT_MANAGER_ID, PHASE1_POOL_ID, PHASE1_RESOURCE_TYPE_ID
from app.models import (AlarmSubscription, DeploymentManager, InventorySubscription, Location, O2imsObject, OCloudAlarm, OCloudPerformanceMetric,
                        OCloudSite, PerformanceJob, PerformanceSubscription, Resource, ResourcePool, ResourceType)


@pytest.fixture
def db_session():
    """Builds a fresh in-memory SQLite database with every FOCOM table plus the outbox table, and returns the `sessionmaker` (not a session).

    `StaticPool` and `check_same_thread=False` make the app's request threads and the test share the one connection, so what a route commits the test can read.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        OCloudAlarm.__table__, OCloudPerformanceMetric.__table__, InventorySubscription.__table__,
        ResourceType.__table__, ResourcePool.__table__, Resource.__table__, DeploymentManager.__table__,
        Location.__table__, OCloudSite.__table__, AlarmSubscription.__table__, PerformanceJob.__table__,
        PerformanceSubscription.__table__, O2imsObject.__table__, NotificationOutbox.__table__,
    ])
    TestSession = sessionmaker(bind=engine)
    return TestSession


@pytest.fixture
def client(db_session):
    """A `TestClient` of the FOCOM app with `get_session` overridden to open sessions on `db_session`; the override is removed after the test."""
    def override_get_session():
        session = db_session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_query_inventory_returns_degenerate_cluster(client):
    """`GET /inventory` returns the Phase 1 cloud under `oCloudId` (the O2-IMS field; NFO reads it), not an invented `clusterId`."""
    resp = client.get("/inventory")
    assert resp.json()["oCloudId"] == PHASE1_CLUSTER_ID


def test_query_inventory_includes_the_real_seeded_resource_type_and_deployment_manager(client):
    """The inventory lists the three seeded resource types and the `dm-0` deployment manager, read from the database rows."""
    resp = client.get("/inventory")
    body = resp.json()
    assert {t["resourceTypeId"] for t in body["resourceTypes"]} == {PHASE1_RESOURCE_TYPE_ID, "gpu-l40", "pserver"}
    assert [d["deploymentManagerId"] for d in body["deploymentManagers"]] == [PHASE1_DEPLOYMENT_MANAGER_ID]


def test_query_inventory_carries_the_seeded_location_and_site(client):
    """SA-FOCOM-2: the spec's required locations / oCloudSites (minItems 1) are the seeded default pair."""
    body = client.get("/inventory").json()
    assert [l["globalLocationId"] for l in body["locations"]] == ["loc-0"]
    assert [s["oCloudSiteId"] for s in body["oCloudSites"]] == ["site-0"]


def test_query_inventory_filters_resource_types_by_the_requested_type(client):
    """The query parameter is `resource_type` (NFO's Instantiate sends that name; `resourceType` was a silent mismatch) and it filters `resourceTypes` to the match."""
    client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"})

    resp = client.get("/inventory", params={"resource_type": "gpu-l40"})
    assert [t["resourceTypeId"] for t in resp.json()["resourceTypes"]] == ["gpu-l40"]


def test_query_inventory_filters_to_empty_for_an_unregistered_resource_type(client):
    """A `resource_type` that is not registered gives an empty `resourceTypes`, not an error."""
    resp = client.get("/inventory", params={"resource_type": "does-not-exist"})
    assert resp.json()["resourceTypes"] == []


def test_subscribe_inventory_changes_returns_subscription_id(client):
    """Creating an inventory subscription answers 201 with its `subscriptionId`."""
    resp = client.post("/inventory/subscriptions", json={"callback": "http://consumer/callback"})
    assert resp.status_code == 201
    assert "subscriptionId" in resp.json()


def test_subscribe_inventory_changes_persists_and_returns_consumer_subscription_id(client):
    """The consumer's own tracking id (`consumerSubscriptionId`, an O2-IMS field) is stored and echoed in the create response."""
    resp = client.post("/inventory/subscriptions", json={"callback": "http://consumer/callback", "consumerSubscriptionId": "consumer-sub-1"})
    assert resp.status_code == 201
    assert resp.json()["consumerSubscriptionId"] == "consumer-sub-1"


def test_subscribe_inventory_changes_without_consumer_subscription_id_defaults_to_null(client):
    """The tracking id is optional: when the consumer sends none, the response carries null."""
    resp = client.post("/inventory/subscriptions", json={"callback": "http://consumer/callback"})
    assert resp.json()["consumerSubscriptionId"] is None


def test_provision_resource_notification_passes_through_consumer_subscription_id(client, monkeypatch):
    """The subscription's `consumerSubscriptionId` comes back on the notification itself, so the consumer can route it, not just in the stored row."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/inventory/subscriptions", json={
        "callback": "http://consumer/callback", "resourceTypeId": "gpu-l40", "consumerSubscriptionId": "consumer-sub-1",
    })
    client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"})

    assert len(calls) == 1
    assert calls[0][1]["consumerSubscriptionId"] == "consumer-sub-1"


def test_unsubscribe_inventory_changes_removes_subscription(client, db_session):
    """`DELETE /inventory/subscriptions/{id}` answers 204 and removes the row."""
    sub = client.post("/inventory/subscriptions", json={"callback": "http://consumer/callback"}).json()

    resp = client.delete(f"/inventory/subscriptions/{sub['subscriptionId']}")
    assert resp.status_code == 204

    with db_session() as session:
        assert session.get(InventorySubscription, uuid.UUID(sub["subscriptionId"])) is None


def test_unsubscribe_unknown_inventory_subscription_is_idempotent(client):
    """Deleting a subscription that does not exist is still 204."""
    resp = client.delete("/inventory/subscriptions/11111111-1111-1111-1111-111111111111")
    assert resp.status_code == 204


def test_provision_resource_notifies_matching_subscriber(client, monkeypatch):
    """Provisioning a resource sends a CREATE notification, with the resource id and type, to a subscriber whose type filter matches."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/inventory/subscriptions", json={"callback": "http://consumer/callback", "resourceTypeId": "gpu-l40"})

    resp = client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"})
    resource_id = resp.json()["resourceId"]

    assert len(calls) == 1
    assert calls[0][0] == "http://consumer/callback"
    assert calls[0][1]["notificationEventType"] == "CREATE"
    assert calls[0][1]["resourceId"] == resource_id
    assert calls[0][1]["resourceTypeId"] == "gpu-l40"


def test_provision_resource_does_not_notify_subscriber_filtered_out_by_type(client, monkeypatch):
    """A subscriber filtered to another resource type receives nothing."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/inventory/subscriptions", json={"callback": "http://consumer/callback", "resourceTypeId": "gpu-l40"})

    client.post("/resources/provision", json={"resourceTypeId": "generic"})

    assert calls == []


def test_deprovisioning_an_unknown_resource_notifies_nobody(client, monkeypatch):
    """Call flow 16d: deprovisioning an id that is not a stored resource (or not a UUID) is a 200 no-op that sends no DELETE (it told every subscriber
    that a resource that never existed was gone)."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    client.post("/inventory/subscriptions", json={"callback": "http://consumer/callback"})
    assert client.delete("/resources/some-resource-id").json() == {"status": "deprovisioned"}
    assert client.delete(f"/resources/{uuid.uuid4()}").status_code == 200
    assert calls == []


def test_deprovision_known_resource_notifies_with_its_real_type(client, monkeypatch):
    """Deprovisioning a stored resource sends DELETE with the resource's real type, which the filter is matched against."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/inventory/subscriptions", json={"callback": "http://consumer/callback", "resourceTypeId": "gpu-l40"})
    provisioned = client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"}).json()
    calls.clear()  # discard the provision-time CREATE notification

    client.delete(f"/resources/{provisioned['resourceId']}")

    assert len(calls) == 1
    assert calls[0][1]["notificationEventType"] == "DELETE"
    assert calls[0][1]["resourceTypeId"] == "gpu-l40"


def test_inventory_notification_delivery_survives_unreachable_subscriber(client, monkeypatch):
    """A subscriber that cannot be reached does not fail the provision: the request still answers 200."""
    import httpx as httpx_module

    def raise_error(url, json=None, timeout=None, **kw):
        raise httpx_module.ConnectError("unreachable")

    monkeypatch.setattr("app.main.httpx.post", raise_error)

    client.post("/inventory/subscriptions", json={"callback": "http://consumer/callback"})

    resp = client.post("/resources/provision", json={"resourceTypeId": "generic"})  # must not raise
    assert resp.status_code == 200


def test_provision_and_deprovision_resource(client):
    """Provision answers with `resourceId` and the Phase 1 `clusterId` (extra spec keys are ignored); deprovision answers `deprovisioned`."""
    provisioned = client.post("/resources/provision", json={"cpu": 4, "memory": "16Gi"})
    assert provisioned.status_code == 200
    body = provisioned.json()
    assert body["clusterId"] == PHASE1_CLUSTER_ID
    assert "resourceId" in body

    deprovisioned = client.delete(f"/resources/{body['resourceId']}")
    assert deprovisioned.json()["status"] == "deprovisioned"


def test_resource_status_follows_its_active_alarms_and_an_unknown_one_is_404(client):
    """Call flow 16c: a resource is `healthy` with no active alarm and `degraded` with one, counted in `activeAlarms`; a cleared alarm no longer counts; an
    unknown id is 404 `RESOURCE_NOT_FOUND` (it answered `healthy` for any id)."""
    rid = client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"}).json()["resourceId"]
    assert client.get(f"/resources/{rid}/status").json() == {"resourceId": rid, "status": "healthy", "activeAlarms": 0}
    alarm = client.post("/alarms/ingest", params={"resource_ref": rid, "severity": "major"}).json()
    assert client.get(f"/resources/{rid}/status").json() == {"resourceId": rid, "status": "degraded", "activeAlarms": 1}
    alarm_id = alarm.get("alarmId") or alarm.get("alarmEventRecordId")
    client.patch(f"/alarms/{alarm_id}/clear")
    assert client.get(f"/resources/{rid}/status").json()["status"] == "healthy"
    missing = client.get("/resources/some-resource-id/status")
    assert missing.status_code == 404 and missing.json()["detail"]["title"] == "RESOURCE_NOT_FOUND"
    assert client.get(f"/resources/{rid}").json()["resourceTypeId"] == "gpu-l40" and client.get(f"/resources/{uuid.uuid4()}").status_code == 404


def test_ingested_alarm_is_queryable(client):
    """An ingested infrastructure alarm is listed by `GET /alarms` with its `resourceRef` (FOCOM's own domain, distinct from RAN NF OAM's alarms)."""
    client.post("/alarms/ingest", params={"resource_ref": "host-1", "severity": "critical"})
    resp = client.get("/alarms")
    assert len(resp.json()["items"]) == 1
    assert resp.json()["items"][0]["resourceRef"] == "host-1"


def test_performance_metrics_filterable_by_resource(client, db_session):
    """`GET /performance?resource_ref=` returns only that resource's records. The rows are inserted directly because the test needs records with no job."""
    session = db_session()
    session.add(OCloudPerformanceMetric(resource_ref="host-1", metric_name="cpu", value=0.5))
    session.add(OCloudPerformanceMetric(resource_ref="host-2", metric_name="cpu", value=0.9))
    session.commit()
    session.close()

    resp = client.get("/performance", params={"resource_ref": "host-2"})
    assert len(resp.json()["items"]) == 1
    assert resp.json()["items"][0]["value"] == 0.9


def test_performance_metrics_without_filter_returns_all(client, db_session):
    """Without a filter every record is returned (the unfiltered branch of the query)."""
    session = db_session()
    session.add(OCloudPerformanceMetric(resource_ref="host-1", metric_name="cpu", value=0.5))
    session.add(OCloudPerformanceMetric(resource_ref="host-2", metric_name="cpu", value=0.9))
    session.commit()
    session.close()

    resp = client.get("/performance")
    assert len(resp.json()["items"]) == 2


def test_query_performance_returns_empty_list_when_none_seeded(client):
    """With no records the list is empty."""
    resp = client.get("/performance")
    assert resp.json()["items"] == []


def test_query_inventory_defaults_to_every_registered_resource_type(client):
    """With no `resource_type`, `resourceTypes` is every registered type: the three seeded ones."""
    resp = client.get("/inventory")
    assert {t["resourceTypeId"] for t in resp.json()["resourceTypes"]} == {PHASE1_RESOURCE_TYPE_ID, "gpu-l40", "pserver"}


def test_query_inventory_reflects_the_real_seeded_deployment_manager_row(client, db_session):
    """`/inventory` reads the `dm-0` row: after the row is changed in the database the response shows the new `oCloudId` and `name`, so the values are not constants."""
    client.get("/deployment-managers")  # triggers _ensure_phase1_topology's lazy seed

    db = db_session()
    dm = db.get(DeploymentManager, PHASE1_DEPLOYMENT_MANAGER_ID)
    dm.name = "renamed-cluster"
    dm.o_cloud_id = "renamed-ocloud"
    db.commit()
    db.close()

    resp = client.get("/inventory")
    assert resp.json()["oCloudId"] == "renamed-ocloud"
    assert resp.json()["name"] == "renamed-cluster"
    assert resp.json()["deploymentManagers"][0]["name"] == "renamed-cluster"


def test_query_alarms_returns_empty_list_when_none_ingested(client):
    """With no alarms the list is empty."""
    resp = client.get("/alarms")
    assert resp.json()["items"] == []


def test_multiple_alarms_are_all_returned(client):
    """A second alarm does not overwrite or drop the first."""
    client.post("/alarms/ingest", params={"resource_ref": "host-1", "severity": "critical"})
    client.post("/alarms/ingest", params={"resource_ref": "host-2", "severity": "minor"})

    resp = client.get("/alarms")
    assert len(resp.json()["items"]) == 2
    refs = {a["resourceRef"] for a in resp.json()["items"]}
    assert refs == {"host-1", "host-2"}


def test_deprovision_arbitrary_unprovisioned_resource_succeeds(client):
    """Deprovisioning an id that was never provisioned is a 200 no-op, not an error."""
    resp = client.delete("/resources/never-provisioned-id")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deprovisioned"


def test_list_resource_types_returns_seeded_phase1_type(client):
    """The topology is seeded on first read: `GET /resource-types` lists the three seeded types."""
    resp = client.get("/resource-types")
    ids = {t["resourceTypeId"] for t in resp.json()["items"]}
    assert ids == {PHASE1_RESOURCE_TYPE_ID, "gpu-l40", "pserver"}


def test_get_resource_type_by_id(client):
    """A stored resource type is returned by id."""
    resp = client.get(f"/resource-types/{PHASE1_RESOURCE_TYPE_ID}")
    assert resp.status_code == 200
    assert resp.json()["resourceTypeId"] == PHASE1_RESOURCE_TYPE_ID


def test_get_unknown_resource_type_is_404(client):
    """An unknown resource type id answers 404."""
    resp = client.get("/resource-types/does-not-exist")
    assert resp.status_code == 404


def test_resource_type_view_exposes_the_new_spec_fields(client):
    """The resource type view carries the spec's dictionary ids, kind, class and extensions; they are null on a seeded type because nothing sets them."""
    resp = client.get(f"/resource-types/{PHASE1_RESOURCE_TYPE_ID}")
    body = resp.json()
    for field in ("alarmDictionaryId", "performanceDictionaryId", "resourceKind", "resourceClass", "extensions"):
        assert field in body
        assert body[field] is None


def test_provision_with_unrecognized_type_is_refused(client):
    """SA-FOCOM-9: an unregistered `resourceTypeId` is 404 and no type is created as a side effect."""
    resp = client.post("/resources/provision", json={"resourceTypeId": "tpu-v5"})
    assert resp.status_code == 404
    ids = {t["resourceTypeId"] for t in client.get("/resource-types").json()["items"]}
    assert "tpu-v5" not in ids


def test_provision_with_unrecognized_type_auto_registers_it_behind_the_flag(client, monkeypatch):
    """With `FOCOM_AUTO_REGISTER_RESOURCE_TYPES=true` an unknown type is registered and provisioned (the flag is read per call, so `monkeypatch.setenv` is enough)."""
    monkeypatch.setenv("FOCOM_AUTO_REGISTER_RESOURCE_TYPES", "true")
    assert client.post("/resources/provision", json={"resourceTypeId": "tpu-v5"}).status_code == 200
    ids = {t["resourceTypeId"] for t in client.get("/resource-types").json()["items"]}
    assert "tpu-v5" in ids


def test_list_resource_pools_returns_seeded_phase1_pool(client):
    """The seeded `pool-0` is the only pool listed on a fresh database."""
    resp = client.get("/resource-pools")
    ids = [p["resourcePoolId"] for p in resp.json()["items"]]
    assert ids == [PHASE1_POOL_ID]


def test_get_resource_pool_by_id(client):
    """A pool is returned by id with the Phase 1 `oCloudId`."""
    resp = client.get(f"/resource-pools/{PHASE1_POOL_ID}")
    assert resp.status_code == 200
    assert resp.json()["oCloudId"] == PHASE1_CLUSTER_ID


def test_get_unknown_resource_pool_is_404(client):
    """An unknown pool id answers 404."""
    resp = client.get("/resource-pools/does-not-exist")
    assert resp.status_code == 404


def test_list_pool_resources_is_empty_before_any_provisioning(client):
    """A pool with no provisioned resources lists an empty page (200, not 404)."""
    resp = client.get(f"/resource-pools/{PHASE1_POOL_ID}/resources")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_list_pool_resources_reflects_provisioned_resource(client):
    """A provisioned resource is persisted and shows up, with its type, in its pool's resource list."""
    provisioned = client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"}).json()

    resp = client.get(f"/resource-pools/{PHASE1_POOL_ID}/resources")
    resources = resp.json()["items"]
    assert len(resources) == 1
    assert resources[0]["resourceId"] == provisioned["resourceId"]
    assert resources[0]["resourceTypeId"] == "gpu-l40"


def test_provision_resource_persists_global_asset_id_tags_and_groups(client):
    """`globalAssetId`, `tags` and `groups` from the provision body are stored and returned in the resource view."""
    provisioned = client.post("/resources/provision", json={
        "resourceTypeId": "gpu-l40", "globalAssetId": "SN-12345", "tags": ["gpu", "edge"], "groups": ["site-a"],
    }).json()

    resp = client.get(f"/resource-pools/{PHASE1_POOL_ID}/resources")
    resource = resp.json()["items"][0]
    assert resource["resourceId"] == provisioned["resourceId"]
    assert resource["globalAssetId"] == "SN-12345"
    assert resource["tags"] == ["gpu", "edge"]
    assert resource["groups"] == ["site-a"]


def test_provision_resource_without_optional_spec_fields_leaves_them_null(client):
    """The optional fields are null, not empty, when the provision body omits them."""
    client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"})
    resource = client.get(f"/resource-pools/{PHASE1_POOL_ID}/resources").json()["items"][0]
    assert resource["globalAssetId"] is None
    assert resource["tags"] is None
    assert resource["groups"] is None


def test_deprovisioned_resource_no_longer_listed(client):
    """A deprovisioned resource is removed from its pool's list."""
    provisioned = client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"}).json()
    client.delete(f"/resources/{provisioned['resourceId']}")

    resp = client.get(f"/resource-pools/{PHASE1_POOL_ID}/resources")
    assert resp.json()["items"] == []


def test_list_resources_for_unknown_pool_is_404(client):
    """Listing the resources of an unknown pool answers 404, unlike an existing empty pool."""
    resp = client.get("/resource-pools/does-not-exist/resources")
    assert resp.status_code == 404


def test_list_deployment_managers_returns_seeded_phase1_manager(client):
    """The seeded `dm-0` is the only deployment manager on a fresh database."""
    resp = client.get("/deployment-managers")
    ids = [d["deploymentManagerId"] for d in resp.json()["items"]]
    assert ids == [PHASE1_DEPLOYMENT_MANAGER_ID]


def test_get_deployment_manager_by_id(client):
    """A deployment manager is returned by id with the Phase 1 `oCloudId`."""
    resp = client.get(f"/deployment-managers/{PHASE1_DEPLOYMENT_MANAGER_ID}")
    assert resp.status_code == 200
    assert resp.json()["oCloudId"] == PHASE1_CLUSTER_ID


def test_get_unknown_deployment_manager_is_404(client):
    """An unknown deployment manager id answers 404."""
    resp = client.get("/deployment-managers/does-not-exist")
    assert resp.status_code == 404


def test_deployment_manager_view_exposes_the_new_spec_fields(client):
    """The view carries `supportedLocations`, `capabilities` and `capacity`; they are null on the seeded manager because nothing introspects the cluster."""
    resp = client.get(f"/deployment-managers/{PHASE1_DEPLOYMENT_MANAGER_ID}")
    body = resp.json()
    for field in ("supportedLocations", "capabilities", "capacity"):
        assert field in body
        assert body[field] is None


def test_topology_export_includes_phase1_seeded_entities(client):
    """Before any resource exists the export still lists the seeded types, pool and deployment manager as entities, and has no relationships."""
    resp = client.get("/topology")
    assert resp.status_code == 200
    body = resp.json()

    entity_types = {key for group in body["entities"] for key in group}
    assert entity_types == {
        "o-ran-smo-teiv-cloud:ResourceType",
        "o-ran-smo-teiv-cloud:ResourcePool",
        "o-ran-smo-teiv-cloud:DeploymentManager",
    }
    # No resources provisioned yet, so no Resource entities and no relationships at all.
    assert body["relationships"] == []

    resource_types = next(g["o-ran-smo-teiv-cloud:ResourceType"] for g in body["entities"] if "o-ran-smo-teiv-cloud:ResourceType" in g)
    assert resource_types[0]["id"] == f"urn:oran:smo:teiv:ResourceType:{PHASE1_RESOURCE_TYPE_ID}"
    assert resource_types[0]["attributes"]["name"] == "generic"


def test_topology_export_includes_provisioned_resource_and_its_relationships(client):
    """A provisioned resource is a typed entity with is-of-type and contained-in-pool relationships in the shape of the reference focom-to-teiv-adapter."""
    provisioned = client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"}).json()
    resource_id = provisioned["resourceId"]

    body = client.get("/topology").json()

    resource_entities = next(g["o-ran-smo-teiv-cloud:Resource"] for g in body["entities"] if "o-ran-smo-teiv-cloud:Resource" in g)
    assert len(resource_entities) == 1
    assert resource_entities[0]["id"] == f"urn:oran:smo:teiv:Resource:{resource_id}"
    assert resource_entities[0]["attributes"]["resourceTypeId"] == "gpu-l40"
    assert resource_entities[0]["attributes"]["resourcePoolId"] == PHASE1_POOL_ID

    rel_types = {key for group in body["relationships"] for key in group}
    assert rel_types == {
        "o-ran-smo-teiv-cloud:RESOURCE_IS_OF_TYPE_RESOURCETYPE",
        "o-ran-smo-teiv-cloud:RESOURCE_CONTAINED_IN_RESOURCEPOOL",
    }

    is_of_type = next(g["o-ran-smo-teiv-cloud:RESOURCE_IS_OF_TYPE_RESOURCETYPE"] for g in body["relationships"] if "o-ran-smo-teiv-cloud:RESOURCE_IS_OF_TYPE_RESOURCETYPE" in g)
    assert is_of_type[0]["aSide"] == f"urn:oran:smo:teiv:Resource:{resource_id}"
    assert is_of_type[0]["bSide"] == "urn:oran:smo:teiv:ResourceType:gpu-l40"
    assert is_of_type[0]["sourceIds"] == [resource_id, "gpu-l40"]


def test_topology_export_reflects_child_resource_relationship(client, db_session):
    """A resource's `parent_id` is exported as a RESOURCE_CHILD_OF_RESOURCE relationship (set directly in the database, since no route sets it)."""
    parent_id = client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"}).json()["resourceId"]
    child_id = client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"}).json()["resourceId"]

    with db_session() as session:
        child = session.get(Resource, uuid.UUID(child_id))
        child.parent_id = uuid.UUID(parent_id)
        session.commit()

    body = client.get("/topology").json()
    child_of = next(g["o-ran-smo-teiv-cloud:RESOURCE_CHILD_OF_RESOURCE"] for g in body["relationships"] if "o-ran-smo-teiv-cloud:RESOURCE_CHILD_OF_RESOURCE" in g)
    assert len(child_of) == 1
    assert child_of[0]["aSide"] == f"urn:oran:smo:teiv:Resource:{child_id}"
    assert child_of[0]["bSide"] == f"urn:oran:smo:teiv:Resource:{parent_id}"


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """`GET /health` answers 200 `{"status": "healthy"}`, which is what the GUI BFF's module-status probe calls on every module."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_list_inventory_subscriptions(client):
    """Inventory subscriptions can be listed (with their consumer ids), not only created and deleted."""
    sub = client.post("/inventory/subscriptions", json={"callback": "http://consumer/cb", "consumerSubscriptionId": "c-1"}).json()
    listed = client.get("/inventory/subscriptions").json()["items"]
    assert [(s["subscriptionId"], s["consumerSubscriptionId"]) for s in listed] == [(sub["subscriptionId"], "c-1")]


# ---------------------------------------------------------------- notifications through the outbox (PR-MSG-1.8)

def _outbox_rows(db_session):
    """Returns every outbox row, oldest first, read through a new session."""
    with db_session() as db:
        return db.query(NotificationOutbox).order_by(NotificationOutbox.created_at).all()


def test_an_inventory_notification_survives_a_crash_between_commit_and_send(client, db_session, monkeypatch):
    """The crash test of MSG-1.8: with the inline send off (as if the process died after the commit) the notification is a committed PENDING row and nothing
    went out; a later `outbox.drain` then delivers it.
    """
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    monkeypatch.setenv("MODULE", "focom")
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)) or type("R", (), {"status_code": 200})())
    client.post("/inventory/subscriptions", json={"callback": "http://consumer/callback", "resourceTypeId": "gpu-l40", "consumerSubscriptionId": "sub-1"})

    resource_id = client.post("/resources/provision", json={"resourceTypeId": "gpu-l40"}).json()["resourceId"]

    assert calls == []
    rows = _outbox_rows(db_session)
    assert [(r.module, r.status, r.destination) for r in rows] == [("focom", "PENDING", "http://consumer/callback")]
    assert rows[0].payload["resourceId"] == resource_id and rows[0].payload["notificationEventType"] == "CREATE"

    monkeypatch.delenv("SMO_OUTBOX_INLINE_DRAIN")
    assert outbox.drain(db_session().get_bind())["sent"] == 1
    assert [u for u, _ in calls] == ["http://consumer/callback"]


def test_an_alarm_and_its_notification_commit_together(client, db_session, monkeypatch):
    """Each alarm change enqueues its outbox row in the same transaction: ingest then clear leave exactly a NEW and a CLEAR row for the same alarm."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    client.post("/alarm-subscriptions", json={"callback": "http://consumer/alarms", "consumerSubscriptionId": "a-1"})
    alarm_id = client.post("/alarms/ingest", params={"resource_ref": "host-1", "severity": "critical"}).json()["alarmId"]
    client.patch(f"/alarms/{alarm_id}/clear")

    rows = _outbox_rows(db_session)
    assert [r.payload["alarmNotificationType"] for r in rows] == ["NEW", "CLEAR"]
    assert {r.payload["alarmEventRecord"]["alarmId"] for r in rows} == {alarm_id}


def test_nothing_is_announced_when_the_provision_does_not_commit(client, db_session, monkeypatch):
    """When the commit fails, no outbox row and no callback exist and the resource is not stored: a subscriber is never told about something that did not happen."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    client.post("/inventory/subscriptions", json={"callback": "http://consumer/callback", "resourceTypeId": "gpu-l40"})

    from sqlalchemy.orm import Session as OrmSession
    real_commit = OrmSession.commit

    # A commit that rolls back and raises, but only for a session that has outbox rows pending, so the earlier subscription create commits normally.
    def failing_commit(self):
        if self.info.get("outbox_pending_ids"):
            self.rollback()
            raise RuntimeError("the database refused the commit")
        return real_commit(self)

    monkeypatch.setattr(OrmSession, "commit", failing_commit)
    resp = TestClient(app, raise_server_exceptions=False).post("/resources/provision", json={"resourceTypeId": "gpu-l40"})
    monkeypatch.setattr(OrmSession, "commit", real_commit)

    assert resp.status_code == 500
    assert calls == [] and _outbox_rows(db_session) == []
    with db_session() as db:
        assert db.query(Resource).count() == 0
